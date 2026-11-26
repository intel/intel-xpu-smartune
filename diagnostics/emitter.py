# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# The low-coupling seam. Callers do not know where events are stored or who
# consumes them, so diagnostics can move to a separate process without changing
# caller code.
#
#   post_event()  returns at once; the write runs on the diagnostics writer thread.
#   emit_event()  writes before it returns and returns the stored event.
#
# Two invariants make hooking business code risk-free:
#   1. Best-effort: any failure is swallowed and logged, never raised into the
#      caller. A diagnostics hiccup must never break balancer/monitor/benchmark.
#   2. Context fill-in: app_id / job_id default from the ambient
#      bind_log_context(), so a hook at a deep call site need not thread ids.

from diagnostics import writer
from utils.logger import get_logger, current_log_context

from utils.logger import diagnostics_internal

logger = get_logger(__name__)


def emit_event(event_type, *, severity="info", category="service", summary=None,
               source=None, service=None, app_id=None, job_id=None,
               impact=None, resource_type=None, protection_id=None, episode_id=None,
               attributes=None, ts_utc=None, identity=None, config_revision_id=None,
               boot_id=None):
    """Record a structured operational event. Returns the event dict (or None).

    ``event_type`` is the normalized reason_code -- the stable machine key in
    ``<DOMAIN>_<SUBJECT>_<STATE>`` form, for example
    ``CONTROL_CPU_LIMIT_APPLIED``; ``summary`` is the human sentence.
    ``severity`` rates this one observation, not an alert or advice derived from it.
    ``impact`` / ``resource_type`` / ``protection_id`` / ``episode_id`` are the
    read-model columns. Sensitive values in summary/attributes are scrubbed here
    so no caller has to. Every event reaches the alert policy so an info-level
    recovery can resolve a matching alert; unmatched events remain ledger-only.
    ``identity`` is a stable per-fact anchor a producer that might
    replay the same fact (log/journal scanners) passes to make the write
    idempotent; ``config_revision_id`` links the event to the hardware/software
    inventory version active when it happened.
    ``service`` names the unit the event is about and narrows its alert scope.
    ``boot_id`` defaults to the current boot.
    """
    try:
        # Import lazily so importing emit_event never drags the DB/event stack
        # into a module that only wants the symbol available behind a guard.
        from diagnostics import alerts, event_catalog, event_store, sanitize

        if not event_catalog.enabled(event_type):
            return None

        ctx = current_log_context()
        app_id = app_id or ctx.get("app_id")
        job_id = job_id or ctx.get("job_id")

        summary = sanitize.scrub_text(summary or event_type)
        attributes = sanitize.scrub(attributes) if attributes else None

        # The store's own log lines are diagnostics' trouble, not application errors.
        with diagnostics_internal():
            event = event_store.record_event(
                event_type=event_type, severity=severity, category=category,
                summary=summary, source=source, service=service,
                app_id=app_id, job_id=job_id,
                impact=impact, resource_type=resource_type,
                protection_id=protection_id, episode_id=episode_id,
                attributes=attributes, ts_utc=ts_utc,
                identity=identity, config_revision_id=config_revision_id,
                boot_id=boot_id,
            )
            if event is not None:
                alerts.evaluate(event)
        return event
    except Exception as exc:
        # Never propagate into the business hook.
        logger.warning("emit_event(%s) failed: %s", event_type, exc)
        return None


def post_event(event_type, **fields):
    """emit_event() on the writer thread, stamped with the time of this call; never raises.

    Returns False if the writer's queue was full and the event was dropped.
    """
    try:
        from diagnostics import event_catalog

        if not event_catalog.enabled(event_type):
            return True  # nothing to record; do not spend queue room on it
        if not fields.get("ts_utc"):
            fields["ts_utc"] = writer.now_iso()
        if fields.get("attributes"):
            fields["attributes"] = writer.settled(fields["attributes"])
        return writer.submit(emit_event, event_type, **fields)
    except Exception as exc:
        logger.warning("post_event(%s) failed: %s", event_type, exc)
        return False
