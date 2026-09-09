# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Integration hooks that wire diagnostics onto other subsystems WITHOUT those
# subsystems importing diagnostics. Registered from mount_diagnostics at startup.
#
# Benchmark lifecycle is captured via the JobManager's existing observer
# (jobs.manager.add_listener), so a job start / done / failed / cancelled
# becomes a persisted operational_event. This also outlives the in-memory 20-entry
# job history (the event ledger keeps the record even after the deque drops the
# Job).

from diagnostics.emitter import emit_event
from utils.logger import get_logger

logger = get_logger(__name__)

_bench_listener_registered = False

# Benchmark job status -> (reason_code event_type, severity, impact).
_STATUS_EVENT = {
    "running": ("WORKLOAD_BENCHMARK_STARTED", "info", "none"),
    "done": ("WORKLOAD_BENCHMARK_DONE", "info", "none"),
    "failed": ("WORKLOAD_BENCHMARK_FAILED", "error", "failed"),
    "cancelled": ("WORKLOAD_BENCHMARK_CANCELLED", "warning", "none"),
}


def _on_job(job):
    """JobManager listener: fires on start and on terminal status."""
    try:
        status = getattr(job, "status", None)
        mapping = _STATUS_EVENT.get(status)
        if mapping is None:
            return
        event_type, severity, impact = mapping
        rc = getattr(job, "returncode", None)
        started = getattr(job, "started_at", None)
        finished = getattr(job, "finished_at", None)
        duration = (finished - started) if (started and finished) else None
        kind = getattr(job, "kind", "run")
        summary = f"Benchmark {kind} job {job.id} {status}" + (
            f" (exit {rc})" if rc is not None else "")
        meta = getattr(job, "meta", None) or {}
        emit_event(
            event_type, severity=severity, category="workload.benchmark",
            impact=impact, summary=summary, source="benchmark", job_id=job.id,
            attributes={"kind": kind, "status": status, "returncode": rc,
                        "duration": duration, "run_name": meta.get("run_name"),
                        "meta": meta},
        )
    except Exception as exc:  # a diagnostics hiccup must not disturb benchmark
        logger.warning("benchmark job hook failed: %s", exc)


def register_benchmark_listener():
    """Attach the benchmark lifecycle listener if the benchmark toolchain is
    present. Idempotent and best-effort."""
    global _bench_listener_registered
    if _bench_listener_registered:
        return
    try:
        from benchmark.service import jobs
    except Exception:
        return  # benchmark not deployed here -- nothing to hook
    try:
        jobs.manager.add_listener(_on_job)
        _bench_listener_registered = True
        logger.info("Diagnostics benchmark lifecycle listener registered.")
    except Exception as exc:
        logger.warning("Could not register benchmark listener: %s", exc)
