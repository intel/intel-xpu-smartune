# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Service start, with a crash check: a STARTED with no STOPPED or CRASHED after it
# means the previous run died without recording anything (SIGKILL, OOM kill, native crash).

from diagnostics.emitter import emit_event
from diagnostics.system_info import normalize_boot_id, read_boot_id
from utils.logger import get_logger

logger = get_logger(__name__)

STARTED = "PLATFORM_SERVICE_STARTED"
STOPPED = "PLATFORM_SERVICE_STOPPED"
CRASHED = "PLATFORM_SERVICE_CRASHED"
LIFECYCLE_EVENT_TYPES = (STARTED, STOPPED, CRASHED)
_CATEGORY = "platform.availability"


def record_service_started(source, *, summary, attributes):
    """Record that ``source`` started, after recording a crash of its previous run if any."""
    try:
        record_unclean_exit(source)
    except Exception as exc:
        logger.warning("Unclean-exit check for %s failed: %s", source, exc)
    return emit_event(STARTED, severity="info", category=_CATEGORY, source=source,
                      summary=summary, attributes=attributes)


def record_unclean_exit(source):
    """Emit a crash for ``source`` if its last lifecycle record is a STARTED from this boot.

    A STARTED from an earlier boot means the host went down, not the service. Returns the event."""
    from diagnostics import event_store

    rows = event_store.query_events(source=source, event_types=list(LIFECYCLE_EVENT_TYPES), limit=1)
    if not rows or rows[0].get("event_type") != STARTED:
        return None
    previous = rows[0]
    boot_id = read_boot_id()
    if not boot_id or normalize_boot_id(previous.get("boot_id")) != boot_id:
        return None
    return emit_event(
        CRASHED, severity="critical", category=_CATEGORY, impact="failed", source=source,
        summary=f"{source.capitalize()} service exited without shutting down "
                f"(killed, OOM-killed or crashed in native code)",
        attributes={"detected_by": "restart", "previous_started_at": previous.get("ts_utc")},
    )
