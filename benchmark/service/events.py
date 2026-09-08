# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Server-sent-event broker for the Benchmark tab.
#
# The tab used to poll: GET /bench/env plus GET /bench/run/<id>?offset= every two
# seconds for as long as a job ran, and a setup runs for an hour. Everything the
# page needs to know about is generated in THIS process -- the job manager owns
# the subprocess, sampler.py owns the metrics, models.py owns the search -- so the
# server can simply say when something changed and the browser can stop asking.
#
# This module is that side of it: a fan-out queue per connected browser, plus a
# log pump that turns the job log file into incremental deltas while a job runs.
#
# Why not reuse balance_service.py's /app/events: that route is registered
# directly on the balancer app, and the benchmark blueprint also mounts on the
# monitor-only service, where it does not exist. Its payload is app-status shaped
# and its fan-out lives in utils.app_utils, which the benchmark feature has no
# business reaching into.

import json
import queue
import threading
from typing import Dict, Optional

from utils.logger import logger

from benchmark.service import env, jobs, models

# One browser tab holds one stream. The cap is a backstop against a client that
# reconnects without closing: each subscriber costs a thread in the WSGI server.
MAX_CLIENTS = 8

# Per-subscriber backlog. A browser that stops reading (laptop asleep mid-build)
# must not grow this process's memory without bound, so the queue is dropped from
# rather than blocked on -- see publish().
_MAX_BACKLOG = 256

# How often the pump looks for new output while a job runs. Fast enough that the
# log reads as live, slow enough that a chatty build does not become one event
# per line.
_PUMP_INTERVAL_SEC = 0.5

# Log tail handed to a client that connects mid-job. The full log stays on disk
# (and the REST endpoint still serves any offset); this only bounds what a fresh
# connection has to swallow before it can show anything.
_SNAPSHOT_TAIL_BYTES = 64 * 1024

_lock = threading.Lock()
# queue -> client id (the browser tab it belongs to), or None for a caller that
# did not identify itself. See subscribe().
_subscribers: Dict[queue.Queue, Optional[str]] = {}

# Queued at a subscriber to tell its generator to finish. A stream blocked on
# get() cannot notice its socket has gone until it next tries to write, so
# replacing a stale connection has to be done from this side.
_CLOSE = object()

# Bumped whenever a run finishes, i.e. whenever /bench/results could have changed.
# The dashboard refetches on a change rather than being handed the whole table.
_results_rev = 0


class TooManyClients(RuntimeError):
    """Raised when MAX_CLIENTS streams are already connected."""


# --- fan-out --------------------------------------------------------------

def subscribe(client_id: Optional[str] = None) -> queue.Queue:
    """Register a stream, replacing any previous one from the same client.

    ``client_id`` identifies a browser tab, which reconnects whenever it starts
    or stops wanting log deltas. Without the replacement, each of those would add
    a subscriber while the one it superseded sat blocked on get() -- invisible
    until its next heartbeat write failed, up to HEARTBEAT_TIMEOUT_SEC later. A
    user flicking between tabs would hit MAX_CLIENTS with a single browser open.
    """
    with _lock:
        if client_id is not None:
            for existing, owner in list(_subscribers.items()):
                if owner == client_id:
                    existing.put_nowait(_CLOSE)
                    del _subscribers[existing]
        if len(_subscribers) >= MAX_CLIENTS:
            raise TooManyClients(f"at most {MAX_CLIENTS} benchmark event streams")
        q: queue.Queue = queue.Queue()
        _subscribers[q] = client_id
        return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.pop(q, None)


def publish(event: dict) -> None:
    """Hand ``event`` to every connected stream. Never blocks, never raises.

    A subscriber whose backlog is full is not waited for: the event is dropped
    for that client alone. The dashboard tolerates a dropped delta -- log events
    carry absolute offsets, so the next one it does receive tells it there is a
    gap, and it refetches the missing span over REST.
    """
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        if q.qsize() >= _MAX_BACKLOG:
            continue
        try:
            q.put_nowait(event)
        except queue.Full:  # pragma: no cover - unbounded queue
            pass


def has_subscribers() -> bool:
    with _lock:
        return bool(_subscribers)


# --- snapshot -------------------------------------------------------------

def snapshot(with_log: bool = False) -> dict:
    """The whole state a freshly connected client needs, in one event.

    This is what lets the dashboard drop its polling entirely: it never has to
    ask "what is going on" on load, and reconnecting after a dropped stream
    re-syncs it in a single message.
    """
    try:
        status = env.probe()
        setup_job = jobs.manager.latest(kind="setup")
        status["setup_job"] = setup_job.to_dict() if setup_job else None
        current = jobs.manager.current()
        status["busy"] = current.to_dict() if current else None
    except Exception:
        logger.exception("Benchmark snapshot: failed to probe the environment")
        status, current = {}, None

    # The job worth showing is whatever is running; with nothing running, the last
    # setup is the only thing a freshly opened tab could usefully be looking at.
    job = current or jobs.manager.latest(kind="setup")

    log = None
    if with_log and job is not None:
        offset = jobs.tail_offset(job.log_path, _SNAPSHOT_TAIL_BYTES)
        read = jobs.read_log(job.log_path, offset)
        log = {
            "job_id": job.id,
            "start": offset,
            "end": read["offset"],
            "chunk": read["chunk"],
            # A tail means the client is NOT holding the start of the log. It uses
            # this to say so rather than pretending the run began here.
            "truncated": offset > 0,
        }

    try:
        model_state = models.load_state()
    except Exception:
        logger.exception("Benchmark snapshot: failed to read the model cache state")
        model_state = None

    return {
        "type": "snapshot",
        "env": status,
        "job": job.to_dict() if job else None,
        "log": log,
        "models": model_state,
        "results_rev": _results_rev,
    }


# --- producers ------------------------------------------------------------

def publish_env() -> None:
    """Announce a new environment status (setup finished, venv probe landed)."""
    try:
        status = env.probe()
        setup_job = jobs.manager.latest(kind="setup")
        status["setup_job"] = setup_job.to_dict() if setup_job else None
        current = jobs.manager.current()
        status["busy"] = current.to_dict() if current else None
    except Exception:
        logger.exception("Failed to publish the benchmark environment status")
        return
    publish({"type": "env", "env": status})


def publish_models() -> None:
    """Announce that the cached model list, or the state of it, changed.

    Sent when a background search starts and again when it lands, and after any
    job finishes -- what the list says about a model (downloaded, and with which
    precisions) is read off disk per request, so a run that downloaded weights
    only shows up once the browser asks again.
    """
    try:
        state = models.load_state()
    except Exception:
        logger.exception("Failed to publish the benchmark model-cache state")
        return
    publish({"type": "models", "models": state})


def publish_results() -> None:
    """Tell every tab the results tree changed -- a run finished, or cases were
    deleted. The event carries a revision, not the rows: the tables are a REST
    read away and too big to push."""
    global _results_rev
    with _lock:
        _results_rev += 1
        rev = _results_rev
    publish({"type": "results", "rev": rev})


# --- log pump -------------------------------------------------------------

class _LogPump(threading.Thread):
    """Tails one job's log and publishes the deltas.

    Deltas carry absolute [start, end) offsets rather than "here is more text":
    that is what lets a client which connected mid-run, missed an event, or
    reconnected, splice them onto whatever it already has -- and detect when it
    cannot, so it can fall back to a REST read for the gap.
    """

    def __init__(self, job: jobs.Job):
        super().__init__(daemon=True, name=f"bench-logpump-{job.id}")
        self.job = job
        self._offset = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _drain(self) -> None:
        read = jobs.read_log(self.job.log_path, self._offset)
        chunk = read.get("chunk") or ""
        if not chunk:
            # read_log rewinds a stale offset past EOF; adopt whatever it settled
            # on so we do not keep asking from beyond the end of the file.
            self._offset = read["offset"]
            return
        start, self._offset = self._offset, read["offset"]
        publish({
            "type": "log",
            "job_id": self.job.id,
            "start": start,
            "end": self._offset,
            "chunk": chunk,
        })

    def run(self) -> None:
        try:
            while not self._stop.wait(_PUMP_INTERVAL_SEC):
                self._drain()
            # The job reached a terminal status while we were sleeping. read_log
            # caps a single read, so keep going until the file stops growing --
            # otherwise the last seconds of a run (including the exit line the
            # reaper appends) never reach the browser.
            for _ in range(64):
                before = self._offset
                self._drain()
                if self._offset == before:
                    break
        except Exception:
            logger.exception(f"Benchmark log pump for job {self.job.id} died")


_pump: Optional[_LogPump] = None
_pump_lock = threading.Lock()


def _on_job_event(job: jobs.Job) -> None:
    """jobs.JobManager listener: a job started or reached a terminal status."""
    global _pump
    running = job.status == jobs.STATUS_RUNNING

    with _pump_lock:
        if running:
            if _pump is not None:
                _pump.stop()
            _pump = _LogPump(job)
            _pump.start()
        elif _pump is not None and _pump.job is job:
            # Signals the final drain; the thread exits on its own afterwards.
            _pump.stop()
            _pump = None

    publish({"type": "job", "job": job.to_dict()})

    if not running:
        # A finished job changes both what the environment looks like (a setup
        # just installed the venv, a run just downloaded models) and what results
        # exist. Publishing on the reaper thread is fine: probe() answers from
        # cache and never blocks here.
        publish_env()
        # The model list changes on both kinds of job, in different ways: a setup
        # installs the `hf` CLI that makes a refresh possible at all, and a run
        # leaves newly downloaded weights on disk that only a re-fetch of the
        # list will show as downloaded. load_state() is a JSON read and a stat,
        # so this is as cheap as the two calls above.
        publish_models()
        if job.kind == "run":
            publish_results()


jobs.manager.add_listener(_on_job_event)


# --- SSE framing ----------------------------------------------------------

# Matches balance_service.py's /app/events: a comment line every 30s keeps
# proxies and idle-connection timeouts from tearing down a quiet stream.
HEARTBEAT_TIMEOUT_SEC = 30


def stream(q: queue.Queue, with_log: bool = False):
    """Generator of `text/event-stream` frames for an already-subscribed queue.

    The caller subscribes rather than this function doing it, because a generator
    body does not run until it is first iterated -- and by then the response has
    been handed to the WSGI server, far too late to answer "too many clients"
    with a status code.
    """
    try:
        # The caller subscribed BEFORE we snapshot, so an event published in
        # between is queued rather than lost. The client de-duplicates log deltas
        # by offset, so receiving one already covered by the snapshot is harmless.
        yield _frame(snapshot(with_log=with_log))
        while True:
            try:
                event = q.get(timeout=HEARTBEAT_TIMEOUT_SEC)
            except queue.Empty:
                # Doubles as the liveness check: writing to a browser that has
                # gone away is what finally raises and unwinds this generator.
                yield ": heartbeat\n\n"
                continue
            if event is _CLOSE:
                # Superseded by a newer stream from the same tab.
                return
            if not with_log and event.get("type") == "log":
                # The tab is not on screen. Job/env/results events still flow (a
                # background tab has to be able to report that a run finished),
                # but a build writes megabytes that nobody is looking at.
                continue
            yield _frame(event)
    except GeneratorExit:
        pass
    finally:
        unsubscribe(q)


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"
