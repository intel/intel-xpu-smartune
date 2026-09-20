# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# /diag/* REST API. Read-mostly endpoints over the event ledger, alerts, the
# unified log-query façade and the context assembler. Auth is handled app-wide
# by smartune_api's before_app_request gate, so these routes need no per-route
# token checks.

from flask import Blueprint, request
from datetime import datetime, timedelta, timezone

from utils.http_utils import RetCode, construct_response
from utils.logger import get_logger

logger = get_logger(__name__)

diag_bp = Blueprint("diag", __name__, url_prefix="/diag")


def _int_arg(name, default=None):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _str_arg(name):
    v = request.args.get(name)
    return v or None


@diag_bp.route("/event-catalog", methods=["GET"])
def get_event_catalog():
    """List all built-in diagnostic event metadata for Settings."""
    from diagnostics import event_catalog

    return construct_response(data={"events": event_catalog.entries()})


@diag_bp.route("/events", methods=["GET"])
def get_events():
    """Filtered operational-event query.

    Query params: severity, category, source, event_type, app_id, job_id,
    impact, resource_type, protection_id, episode_id, boot_id, keyword,
    from/to (epoch seconds), limit, offset.
    """
    try:
        from diagnostics import event_store

        events = event_store.query_events(
            event_id=_str_arg("event_id"),
            severity=_str_arg("severity"),
            category=_str_arg("category"),
            source=_str_arg("source"),
            event_type=_str_arg("event_type"),
            app_id=_str_arg("app_id"),
            job_id=_str_arg("job_id"),
            impact=_str_arg("impact"),
            resource_type=_str_arg("resource_type"),
            protection_id=_str_arg("protection_id"),
            episode_id=_str_arg("episode_id"),
            boot_id=_str_arg("boot_id"),
            keyword=_str_arg("keyword"),
            start_time=_int_arg("from"),
            end_time=_int_arg("to"),
            limit=_int_arg("limit", 200),
            offset=_int_arg("offset", 0),
        )
        return construct_response(data={"events": events, "count": len(events)})
    except Exception as exc:
        logger.error("get_events failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/alerts", methods=["GET"])
def get_alerts():
    """List derived diagnostics alerts, optionally limited to current active state."""
    try:
        from diagnostics import event_store

        active_only = request.args.get("active", "true").lower() not in ("0", "false", "no")
        notifyable_only = request.args.get("notifyable", "false").lower() in ("1", "true", "yes")
        alerts = event_store.query_alerts(
            active_only=active_only, notifyable_only=notifyable_only,
            limit=_int_arg("limit", 100),
        )
        return construct_response(data={"alerts": alerts, "count": len(alerts)})
    except Exception as exc:
        logger.error("get_alerts failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/alerts/<dedup_key>/acknowledge", methods=["POST"])
def acknowledge_alert(dedup_key):
    from db.DatabaseModel import AlertState

    if not AlertState.acknowledge(dedup_key, datetime.now(timezone.utc).isoformat(timespec="seconds")):
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="Active alert not found")
    return construct_response(data={"acknowledged": True})


@diag_bp.route("/alerts/<dedup_key>/silence", methods=["POST"])
def silence_alert(dedup_key):
    payload = request.get_json(silent=True) or {}
    minutes = payload.get("minutes")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="minutes must be an integer")
    if minutes not in (30, 120):
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="minutes must be 30 or 120")
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    from db.DatabaseModel import AlertState

    if not AlertState.silence(dedup_key, until.isoformat(timespec="seconds"), "operator_silenced"):
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="Active alert not found")
    return construct_response(data={"silenced_until": until.isoformat(timespec="seconds")})


@diag_bp.route("/control-lifecycles", methods=["GET"])
def get_control_lifecycles():
    """List user-facing resource-limit lifecycles reconstructed from events."""
    try:
        from diagnostics import control_lifecycle

        lifecycles = control_lifecycle.query(
            start_time=_int_arg("from"), end_time=_int_arg("to"),
            app_id=_str_arg("app_id"), protection_id=_str_arg("protection_id"),
            limit=_int_arg("limit", 200), offset=_int_arg("offset", 0),
        )
        return construct_response(data={"lifecycles": lifecycles, "count": len(lifecycles)})
    except Exception as exc:
        logger.error("get_control_lifecycles failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/control-lifecycles/clear-without-runtime-state", methods=["POST"])
def clear_control_lifecycle_without_runtime_state():
    """Close an orphaned control lifecycle after runtime state was checked."""
    protection_id = (request.get_json(silent=True) or {}).get("protection_id")
    if not protection_id:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="protection_id is required")
    try:
        from diagnostics import control_lifecycle

        if not control_lifecycle.clear_without_runtime_state(str(protection_id)):
            return construct_response(retcode=RetCode.ARGUMENT_ERROR,
                                      retmsg="Control lifecycle still has recoverable state")
        return construct_response(data={"cleared": True})
    except Exception as exc:
        logger.error("clear_control_lifecycle_without_runtime_state failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/retention", methods=["GET"])
def get_retention():
    """Return retention coverage for diagnostics evidence sources."""
    try:
        from diagnostics import retention

        return construct_response(data=retention.status())
    except Exception as exc:
        logger.error("get_retention failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/logs", methods=["GET"])
def get_logs():
    """Unified, read-only, federated log query across sources.

    Params: source (repeatable / comma list), from/to (epoch seconds), level,
    job_id, keyword, limit. Values are redacted before return.
    """
    try:
        from diagnostics import log_query

        sources = request.args.get("source")
        source_list = [s for s in (sources.split(",") if sources else []) if s] or None
        result = log_query.query(
            sources=source_list,
            start_time=_int_arg("from"),
            end_time=_int_arg("to"),
            level=_str_arg("level"),
            job_id=_str_arg("job_id"),
            boot_id=_str_arg("boot_id"),
            keyword=_str_arg("keyword"),
            limit=_int_arg("limit", 500),
        )
        return construct_response(data=result)
    except Exception as exc:
        logger.error("get_logs failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/boots", methods=["GET"])
def get_boots():
    """List recent boot sessions for the Logs boot-session selector.

    Returns ``{"boots": [{boot_id, index, first_ts, last_ts, running, source}]}``
    newest-first; empty when no boot-aware source (journald) is present.
    """
    try:
        from diagnostics import log_query

        result = log_query.list_boots(limit=_int_arg("limit", 15))
        return construct_response(data=result)
    except Exception as exc:
        logger.error("get_boots failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/context", methods=["GET"])
def get_context():
    """On-demand context assembly for root-cause / "why slow" analysis.

    Exactly one scope: job_id | app_id, or a bare from/to window.
    Returns structured, redacted evidence even when there is zero error in the
    window (metrics + control actions still explain a slow-but-not-broken run).
    """
    try:
        from diagnostics import context

        findings_only = request.args.get("findings_only", "false").lower() in (
            "1", "true", "yes")
        if findings_only:
            start, end = _int_arg("from"), _int_arg("to")
            if start is None or end is None:
                return construct_response(
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="findings_only requires from & to")
            return construct_response(data={
                "findings": context.assemble_findings(start_time=start, end_time=end),
            })

        for kind in ("job_id", "app_id"):
            value = _str_arg(kind)
            if value:
                ctx = context.assemble_context(
                    scope_kind=kind, scope_value=value,
                    start_time=_int_arg("from"), end_time=_int_arg("to"))
                return construct_response(data=ctx)

        start, end = _int_arg("from"), _int_arg("to")
        if start is None or end is None:
            return construct_response(
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="provide one of job_id/app_id, or from & to")
        ctx = context.assemble_context(scope_kind="window", scope_value=None,
                                       start_time=start, end_time=end)
        return construct_response(data=ctx)
    except Exception as exc:
        logger.error("get_context failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/monitor-metrics", methods=["GET"])
def get_monitor_metrics():
    """Return monitor samples for a selected diagnostics time range."""
    try:
        from diagnostics import metrics

        start, end = _int_arg("from"), _int_arg("to")
        if start is None or end is None:
            return construct_response(
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="from and to are required")
        if end <= start:
            return construct_response(
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="to must be later than from")
        if _str_arg("summary") == "resource-utilization":
            return construct_response(data={
                "monitor": metrics.read_resource_utilization(start, end),
            })
        return construct_response(data={"monitor": metrics.read_monitor(start, end)})
    except Exception as exc:
        logger.error("get_monitor_metrics failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))


@diag_bp.route("/digest", methods=["GET"])
def get_digest():
    """Generate an on-demand day, week, boot, or explicit-range diagnostics digest."""
    period = request.args.get("period")
    digest_date = request.args.get("date")
    boot_id = _str_arg("boot_id")
    start_time, end_time = _int_arg("from"), _int_arg("to")
    has_range = start_time is not None or end_time is not None
    if has_range and (start_time is None or end_time is None):
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="from and to must be provided together")
    if has_range:
        period = "range"
    elif period is None:
        period = "day"
    if period not in ("day", "week", "boot", "range"):
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="period must be day, week, boot, or range")
    if period == "boot" and not boot_id:
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="boot_id is required for period=boot")
    if period not in ("boot", "range") and not digest_date:
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="date is required (YYYY-MM-DD)")
    try:
        from diagnostics.digest import DigestGenerator

        generator = DigestGenerator()
        if period == "range":
            digest = generator.generate_range(start_time, end_time)
        elif period == "day":
            digest = generator.generate_daily(digest_date)
        elif period == "week":
            digest = generator.generate_weekly(digest_date)
        else:
            digest = generator.generate_per_boot(boot_id)
        return construct_response(data=digest)
    except ValueError as exc:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg=str(exc))
    except Exception as exc:
        logger.error("get_digest failed: %s", exc)
        return construct_response(retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc))
