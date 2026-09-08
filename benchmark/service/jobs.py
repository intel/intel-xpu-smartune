# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# One-at-a-time background job execution for the benchmark pipeline.
#
# Both jobs this package runs -- environment setup (setup_env.sh) and a pipeline
# run (the rendered run_template.sh) -- are long, chatty subprocesses whose output
# the dashboard tails incrementally. They also share a hard constraint: only ONE
# may run at a time. A benchmark run saturates the GPU/NPU and the numbers it
# produces are meaningless if anything else is competing for the device, and a
# setup that reinstalls the venv underneath a running pipeline breaks it outright.
# So the manager below holds a single slot rather than a pool.
#
# Each job is a process GROUP (start_new_session=True) so cancelling reaches the
# whole tree -- optimum-cli, ovms and pip all spawn children that would otherwise
# survive as orphans holding the GPU.

import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from utils.logger import logger

# Terminated jobs kept for status/log lookup after they finish. Logs stay on disk
# regardless; this only bounds the in-memory index.
_HISTORY_LIMIT = 20

# Grace period between SIGTERM and SIGKILL when cancelling.
_KILL_GRACE_SEC = 5.0

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


class BenchBusy(RuntimeError):
    """Raised when a job is requested while another one holds the slot."""

    def __init__(self, current: "Job"):
        super().__init__(
            f"a benchmark {current.kind} job is already running (id={current.id})"
        )
        self.current = current


class Job:
    """A single background subprocess plus the log file it streams into."""

    def __init__(self, kind: str, log_path: Path, meta: Optional[dict] = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                      # "setup" | "run"
        self.log_path = log_path
        self.meta = meta or {}
        self.status = STATUS_RUNNING
        self.returncode: Optional[int] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._on_finish: Optional[Callable[["Job"], None]] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": (self.finished_at or time.time()) - self.started_at,
            "log_path": str(self.log_path),
            "meta": self.meta,
        }


class JobManager:
    """Holds at most one live job; keeps a bounded history of finished ones."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current: Optional[Job] = None
        self._history: deque = deque(maxlen=_HISTORY_LIMIT)
        self._listeners: List[Callable[[Job], None]] = []

    # --- observers --------------------------------------------------------
    def add_listener(self, listener: Callable[[Job], None]) -> None:
        """Call ``listener(job)`` whenever a job starts or reaches a terminal status.

        Deliberately generic: this module knows nothing about who is listening, so
        the event broker (events.py) can register itself without jobs.py importing
        it back. Listeners run on the caller's thread -- the requesting thread for
        a start, the reaper thread for a finish -- so they must not block.
        """
        with self._lock:
            self._listeners.append(listener)

    def _notify(self, job: Job) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(job)
            except Exception:
                # A broken observer must not take down the job that triggered it,
                # nor stop the others from hearing about it.
                logger.exception(f"Benchmark job listener failed for job {job.id}")

    # --- queries ----------------------------------------------------------
    def current(self) -> Optional[Job]:
        with self._lock:
            return self._current

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if self._current is not None and self._current.id == job_id:
                return self._current
            for job in self._history:
                if job.id == job_id:
                    return job
        return None

    def latest(self, kind: Optional[str] = None) -> Optional[Job]:
        """Most recent job of ``kind`` (running or finished), or None."""
        with self._lock:
            if self._current is not None and (kind is None or self._current.kind == kind):
                return self._current
            for job in reversed(self._history):
                if kind is None or job.kind == kind:
                    return job
        return None

    def recent(self, limit: int = 10) -> List[dict]:
        with self._lock:
            jobs = list(self._history)
            if self._current is not None:
                jobs.append(self._current)
        jobs.sort(key=lambda j: j.started_at, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    # --- execution --------------------------------------------------------
    def start(
        self,
        kind: str,
        argv: Sequence[str],
        cwd: str,
        env: Dict[str, str],
        log_path: Path,
        header: str = "",
        meta: Optional[dict] = None,
        on_finish: Optional[Callable[[Job], None]] = None,
    ) -> Job:
        """Spawn ``argv`` in the background. Raises BenchBusy if the slot is taken.

        ``on_finish`` runs on the reaper thread once the job has reached a terminal
        status, however it got there -- exit, failure or cancel. It is the hook that
        releases whatever was set up alongside the process (the metrics sampler and
        the perf/sysfs descriptors it holds).
        """
        with self._lock:
            if self._current is not None:
                raise BenchBusy(self._current)
            job = Job(kind, log_path, meta)
            job._on_finish = on_finish
            self._current = job

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Line buffering keeps the dashboard's incremental tail close to live
            # even while the child is mid-run.
            log_f = open(log_path, "w", buffering=1, encoding="utf-8", errors="replace")
            if header:
                log_f.write(header if header.endswith("\n") else header + "\n")
                log_f.flush()
            job._proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Own process group: cancel() signals the whole tree, not just bash.
                start_new_session=True,
            )
        except Exception:
            with self._lock:
                self._current = None
            logger.exception(f"Failed to start benchmark {kind} job")
            raise

        logger.info(f"Benchmark {kind} job {job.id} started (pid={job._proc.pid}), "
                    f"log={log_path}")
        # Announce the start BEFORE the reaper exists: a job that fails in its
        # first millisecond would otherwise be reported as finished by the reaper
        # thread before anyone had heard it started, and an observer tracking
        # "what is running" would be left believing it still is.
        self._notify(job)
        threading.Thread(
            target=self._reap, args=(job, log_f), daemon=True,
            name=f"bench-{kind}-{job.id}",
        ).start()
        return job

    def _reap(self, job: Job, log_f) -> None:
        rc = job._proc.wait()
        try:
            log_f.write(f"\n=== finished (exit {rc}) ===\n")
            log_f.flush()
            log_f.close()
        except Exception:
            pass
        with self._lock:
            job.returncode = rc
            job.finished_at = time.time()
            if job._cancelled:
                job.status = STATUS_CANCELLED
            else:
                job.status = STATUS_DONE if rc == 0 else STATUS_FAILED
            if self._current is job:
                self._current = None
            self._history.append(job)
        logger.info(f"Benchmark {job.kind} job {job.id} {job.status} (exit {rc})")
        self._notify(job)
        if job._on_finish is not None:
            try:
                job._on_finish(job)
            except Exception:
                # The job is already accounted for; a failing teardown must not
                # leave the slot looking occupied.
                logger.exception(f"Benchmark {job.kind} job {job.id}: on_finish failed")

    def cancel(self, job_id: str) -> bool:
        """SIGTERM the job's process group, escalating to SIGKILL after a grace
        period. Returns False if the job is unknown or already finished."""
        job = self.get(job_id)
        if job is None or job.status != STATUS_RUNNING or job._proc is None:
            return False
        job._cancelled = True
        pgid = os.getpgid(job._proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        logger.info(f"Benchmark {job.kind} job {job.id}: SIGTERM sent to pgid {pgid}")

        def _escalate():
            try:
                job._proc.wait(timeout=_KILL_GRACE_SEC)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    logger.warning(f"Benchmark job {job.id} ignored SIGTERM; SIGKILLed "
                                   f"process group {pgid}")
                except ProcessLookupError:
                    pass

        threading.Thread(target=_escalate, daemon=True).start()
        return True


def tail_offset(log_path: Path, max_bytes: int) -> int:
    """Offset from which ``read_log`` returns at most the last ``max_bytes``.

    A client joining a job that has been running for an hour wants the end of the
    log, not the beginning; read_log only ever reads forwards, so the caller needs
    somewhere to start. Byte-exact, so the first line may be a partial one -- for
    a tail that is the normal, expected cost.
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0
    return max(0, size - max_bytes)


def read_log(log_path: Path, offset: int = 0, max_bytes: int = 256 * 1024) -> dict:
    """Read up to ``max_bytes`` of a job log starting at ``offset``.

    The cap bounds a single response when a caller reconnects against a log that
    grew for hours; the returned offset lets it keep pulling until it catches up.

    Read in binary and decoded here, so ``offset`` is a plain byte count both ways.
    A text-mode handle would return an opaque cookie from tell() whenever a read
    happened to stop mid-character, and events.py feeds this offset straight back
    in every half second: one such cookie would compare greater than the file size
    below and rewind the whole tail to 0. The cost is that a chunk boundary
    splitting a multi-byte character shows one replacement character -- which
    ``errors="replace"`` was already accepting anyway.
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return {"chunk": "", "offset": offset, "size": 0}
    # A truncated/recreated log (fresh job, stale client offset) rewinds to 0
    # rather than seeking past the end and looking permanently empty.
    if offset > size:
        offset = 0
    try:
        with open(log_path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read(max_bytes)
            new_offset = fh.tell()
    except OSError:
        return {"chunk": "", "offset": offset, "size": size}
    return {"chunk": raw.decode("utf-8", errors="replace"),
            "offset": new_offset, "size": size}


# Single manager shared by env.py (setup) and runner.py (pipeline runs) -- that
# sharing is what makes setup and run mutually exclusive.
manager = JobManager()
