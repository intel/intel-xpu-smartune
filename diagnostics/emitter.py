# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# emit_event() -- the low-coupling seam.
#
# Business modules import ONLY this function to report an event. They do not know
# where events are stored or who consumes them. The event writer is intentionally
# isolated so diagnostics can move to a separate process without changing caller
# code.
#
# Two invariants make hooking business code risk-free:
#   1. Best-effort: any failure is swallowed and logged, never raised into the
#      caller. A diagnostics hiccup must never break balancer/monitor/benchmark.
#   2. Context fill-in: app_id / job_id default from the ambient
#      bind_log_context(), so a hook at a deep call site need not thread ids.

from utils.logger import get_logger, current_log_context

logger = get_logger(__name__)


def emit_event(event_type, *, severity="info", category="service", summary=None,
               source=None, app_id=None, job_id=None,
               impact=None, resource_type=None, protection_id=None, episode_id=None,
               attributes=None, ts_utc=None, identity=None, config_revision_id=None):
    """Record a structured operational event. Returns the event dict (or None).

    ``event_type`` is the normalized reason_code -- the stable machine key in
    ``<DOMAIN>_<SUBJECT>_<STATE>`` form, for example
    ``CONTROL_CPU_LIMIT_APPLIED``; ``summary`` is the human sentence.
    ``impact`` / ``resource_type`` / ``protection_id`` / ``episode_id`` are the
    read-model columns. Sensitive values in summary/attributes are scrubbed here
    so no caller has to. Every event reaches the alert policy so an info-level
    recovery can resolve a matching alert; unmatched events remain ledger-only.
    ``identity`` is a stable per-fact anchor a producer that might
    replay the same fact (log/journal scanners) passes to make the write
    idempotent; ``config_revision_id`` links the event to the hardware/software
    inventory version active when it happened.
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

        event = event_store.record_event(
            event_type=event_type, severity=severity, category=category,
            summary=summary, source=source, app_id=app_id, job_id=job_id,
            impact=impact, resource_type=resource_type,
            protection_id=protection_id, episode_id=episode_id,
            attributes=attributes, ts_utc=ts_utc,
            identity=identity, config_revision_id=config_revision_id,
        )
        if event is not None:
            alerts.evaluate(event)
        return event
    except Exception as exc:
        # Never propagate into the business hook.
        logger.warning("emit_event(%s) failed: %s", event_type, exc)
        return None
