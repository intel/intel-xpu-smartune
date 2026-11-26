# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Background event writer: runs diagnostics' storage work on one daemon thread.

Jobs run in submission order, each in a copy of the submitter's contextvars. A full
queue drops the job and counts it (``wait`` gives it a bounded wait for room first);
the queue is drained for a bounded time at exit. Until start() is called, work runs
inline on the caller's thread.
"""

import atexit
import contextvars
import copy
import queue
import threading
import time
from datetime import datetime, timezone

from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CAPACITY = 1000
# How long exit waits for queued work; must fit in the unit's 20 s TimeoutStopSec.
EXIT_DRAIN_SECONDS = 5.0
_DROP_LOG_INTERVAL_SECONDS = 60.0

_lock = threading.Lock()
_queue = None
_thread = None
_atexit_registered = False
_counts = {"submitted": 0, "completed": 0, "failed": 0, "dropped": 0}
_last_drop_at = None
_last_drop_logged = 0.0


_stamp_lock = threading.Lock()
_last_stamp_ms = 0
# A backward clock step larger than this is accepted instead of keeping stamps increasing.
_MAX_STAMP_REGRESSION_MS = 1000


def now_iso():
    """Current UTC time as ISO 8601 (ms), strictly increasing so same-ms events keep their order."""
    global _last_stamp_ms
    with _stamp_lock:
        now_ms = int(time.time() * 1000)
        if 0 <= _last_stamp_ms - now_ms < _MAX_STAMP_REGRESSION_MS:
            now_ms = _last_stamp_ms + 1
        _last_stamp_ms = now_ms
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def settled(value):
    """Deep copy of ``value``, so the caller's later changes cannot reach queued work."""
    try:
        return copy.deepcopy(value)
    except Exception:  # an uncopyable fact: keep the top level at least
        if isinstance(value, dict):
            return dict(value)
        return list(value) if isinstance(value, (list, tuple, set)) else value


def _count(key):
    with _lock:
        _counts[key] += 1


def _store_failures():
    """The calling thread's count of failed event stores (0 if the store cannot load)."""
    try:
        from diagnostics import event_store
        return event_store.write_failures()
    except Exception:
        return 0


def _run_job(job):
    context, fn, args, kwargs = job
    failures_before = _store_failures()
    try:
        context.run(fn, *args, **kwargs)
        # The seams swallow a failed insert, so count by the store's failure counter.
        _count("completed" if _store_failures() == failures_before else "failed")
    except Exception as exc:  # the seams are best-effort; never kill the writer
        _count("failed")
        logger.warning("Diagnostics write %s failed: %s", getattr(fn, "__name__", fn), exc)


def _drain(work):
    while True:
        job = work.get()
        try:
            _run_job(job)
        finally:
            work.task_done()


def start(capacity=DEFAULT_CAPACITY):
    """Start the writer thread and queue work from now on; returns whether this call started it."""
    global _queue, _thread, _atexit_registered
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _queue = queue.Queue(maxsize=max(1, int(capacity)))
        _thread = threading.Thread(target=_drain, args=(_queue,), daemon=True, name="diag-writer")
        _thread.start()
        if not _atexit_registered:
            atexit.register(_drain_at_exit)
            _atexit_registered = True
    logger.info("Diagnostics writer started (capacity %d)", _queue.maxsize)
    return True


def running():
    """Whether submitted work is queued (True) or runs inline (False)."""
    thread = _thread
    return thread is not None and thread.is_alive()


def submit(fn, *args, wait=0.0, **kwargs):
    """Run ``fn(*args, **kwargs)`` on the writer thread (inline if not running).

    Returns False if dropped because the queue stayed full for ``wait`` seconds."""
    global _last_drop_at, _last_drop_logged
    job = (contextvars.copy_context(), fn, args, kwargs)
    _count("submitted")
    work = _queue if running() else None
    if work is None:
        _run_job(job)
        return True
    try:
        if wait and wait > 0:
            work.put(job, timeout=wait)
        else:
            work.put_nowait(job)
        return True
    except queue.Full:
        pass
    now = time.monotonic()
    with _lock:
        _counts["dropped"] += 1
        dropped = _counts["dropped"]
        _last_drop_at = now_iso()
        should_log = now - _last_drop_logged >= _DROP_LOG_INTERVAL_SECONDS
        if should_log:
            _last_drop_logged = now
    if should_log:
        logger.warning("Diagnostics writer queue full (%d); dropped %s (%d dropped so far)",
                       work.maxsize, getattr(fn, "__name__", fn), dropped)
    return False


def flush(timeout=EXIT_DRAIN_SECONDS):
    """Wait for submitted work to finish; returns whether it did within ``timeout`` seconds."""
    work = _queue if running() else None
    if work is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    with work.all_tasks_done:
        while work.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            work.all_tasks_done.wait(remaining)
    return True


def _drain_at_exit():
    work = _queue
    if work is None or not running():
        return
    if not flush(EXIT_DRAIN_SECONDS):
        logger.warning("Diagnostics writer: %d event write(s) not stored before exit", work.qsize())


def stats():
    """Writer mode, queue depth and job counters."""
    work = _queue if running() else None
    with _lock:
        counts = dict(_counts)
        last_drop_at = _last_drop_at
    return {
        "mode": "background" if work is not None else "inline",
        "queued": work.qsize() if work is not None else 0,
        "capacity": work.maxsize if work is not None else None,
        **counts,
        "last_drop_at": last_drop_at,
    }
