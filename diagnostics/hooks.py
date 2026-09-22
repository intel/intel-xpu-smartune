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

# The same lifecycle for a run whose stage only fetches and converts weights.
# It measures nothing, so labelling it a benchmark would bury the runs that do;
# to a diagnostic reader it is a model download.
_DOWNLOAD_EVENT = {
    "running": ("WORKLOAD_MODEL_DOWNLOAD_STARTED", "info", "none"),
    "done": ("WORKLOAD_MODEL_DOWNLOAD_DONE", "info", "none"),
    "failed": ("WORKLOAD_MODEL_DOWNLOAD_FAILED", "error", "failed"),
    "cancelled": ("WORKLOAD_MODEL_DOWNLOAD_CANCELLED", "warning", "none"),
}

# Stages that produce a measured run directory (runner._MEASURED_STAGES). Only
# these have KPI numbers worth freezing onto the terminal event; a "build" does
# not. Inlined rather than imported so a diagnostics import never drags in the
# benchmark runner.
_MEASURED_STAGES = ("benchmark", "all")


def _event_family(kind, meta):
    """Pick the event vocabulary and human label for a job.

    A "build" run downloads models; everything else keeps benchmark wording.
    """
    if kind == "run" and meta.get("stage") == "build":
        return _DOWNLOAD_EVENT, "Model download"
    return _STATUS_EVENT, f"Benchmark {kind or 'run'}"


def _on_job(job):
    """JobManager listener: fires on start and on terminal status."""
    try:
        status = getattr(job, "status", None)
        kind = getattr(job, "kind", "run")
        meta = getattr(job, "meta", None) or {}
        family, label = _event_family(kind, meta)
        mapping = family.get(status)
        if mapping is None:
            return
        event_type, severity, impact = mapping
        rc = getattr(job, "returncode", None)
        started = getattr(job, "started_at", None)
        finished = getattr(job, "finished_at", None)
        log_path = getattr(job, "log_path", None)
        duration = (finished - started) if (started and finished) else None
        summary = f"{label} job {job.id} {status}" + (
            f" (exit {rc})" if rc is not None else "")
        attributes = {"kind": kind, "status": status, "returncode": rc,
                      "duration": duration, "started_at": started, "finished_at": finished,
                      "log_path": log_path,
                      "run_name": meta.get("run_name"), "meta": meta}
        # Freeze the run's KPI numbers onto the terminal event so they outlive the
        # Results tab deleting the run directory. Best-effort and only where there
        # is something to freeze: a build/download or an unmeasured run yields
        # None and nothing is stored. metrics.read_benchmark reads this back once
        # the on-disk tree is gone.
        if status != "running" and kind == "run" and meta.get("stage") in _MEASURED_STAGES:
            try:
                from benchmark.service import results
                snapshot = results.result_snapshot(meta.get("run_name"))
                if snapshot:
                    attributes["result"] = snapshot
            except Exception as exc:
                logger.warning("Benchmark result snapshot skipped for %s: %s", job.id, exc)
            # Capture environment telemetry snapshot at completion time (thermal/power/pressure).
            try:
                from diagnostics import metrics as diag_metrics
                env_snapshot = diag_metrics.environment_snapshot(finished)
                if env_snapshot:
                    attributes["environment_snapshot"] = env_snapshot
            except Exception as exc:
                logger.warning("Environment snapshot skipped for %s: %s", job.id, exc)
        config_revision_id = None
        if kind == "run":
            try:
                from diagnostics import config_revision
                config_revision_id = config_revision.current_revision_id()
            except Exception as exc:
                logger.debug("config revision lookup skipped for %s: %s", job.id, exc)
        emit_event(
            event_type, severity=severity, category="workload.benchmark",
            impact=impact, summary=summary, source="benchmark", job_id=job.id,
            attributes=attributes, identity=f"{job.id}:{status}",
            config_revision_id=config_revision_id,
        )
    except Exception as exc:  # a diagnostics hiccup must not disturb benchmark
        logger.warning("benchmark job hook failed: %s", exc)


# One-off benchmark actions (no job lifecycle) -> (event_type, severity). These
# do not pass through the job manager, so diagnostics observes them via
# events.add_action_listener. Audit-level: they record a user action, not a
# system-health change, so all are info.
_ACTION_EVENT = {
    "model.weights.deleted": "WORKLOAD_MODEL_WEIGHTS_DELETED",
    "benchmark.results.deleted": "WORKLOAD_BENCHMARK_RESULTS_DELETED",
    "benchmark.cases.deleted": "WORKLOAD_BENCHMARK_CASES_DELETED",
}


def _action_summary(action, fields):
    """One human sentence for an audited benchmark action."""
    if action == "model.weights.deleted":
        removed = fields.get("removed") or []
        gib = (fields.get("freed_bytes") or 0) / float(1 << 30)
        return (f"Deleted {', '.join(removed) or 'no'} weights for "
                f"{fields.get('model') or '?'} ({gib:.2f} GiB freed)")
    if action == "benchmark.results.deleted":
        return (f"Deleted benchmark job {fields.get('job') or '?'} "
                f"({fields.get('removed_cases', 0)} case(s) in "
                f"{fields.get('removed_runs', 0)} run directory(ies))")
    if action == "benchmark.cases.deleted":
        return f"Deleted {fields.get('removed', 0)} benchmark case(s)"
    return f"Benchmark action: {action}"


def _on_action(action, fields):
    """events action listener: a one-off benchmark action happened."""
    try:
        event_type = _ACTION_EVENT.get(action)
        if event_type is None:
            return
        fields = fields or {}
        emit_event(
            event_type, severity="info", category="workload.benchmark",
            impact="none", summary=_action_summary(action, fields),
            source="benchmark", job_id=fields.get("job"),
            attributes={"action": action, **fields},
        )
    except Exception as exc:  # never disturb the user action that triggered it
        logger.warning("benchmark action hook failed: %s", exc)


def register_benchmark_listener():
    """Attach the benchmark lifecycle and action listeners if the benchmark
    toolchain is present. Idempotent and best-effort."""
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
    # One-off actions ride a separate seam; a failure here must not undo the
    # lifecycle listener above.
    try:
        from benchmark.service import events
        events.add_action_listener(_on_action)
        logger.info("Diagnostics benchmark action listener registered.")
    except Exception as exc:
        logger.debug("benchmark action listener not attached: %s", exc)
