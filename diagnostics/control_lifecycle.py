# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read and reconcile persisted resource-control lifecycle events."""

from collections import defaultdict

from diagnostics.system_info import read_boot_id


_ACTION_SUFFIXES = ("_APPLIED", "_RECOVERED", "_FAILED")
_RECONCILIATION_EVENT = "CONTROL_LIFECYCLE_REQUIRES_VERIFICATION"
_REBOOT_CLEARED_EVENT = "CONTROL_LIFECYCLE_CLEARED_BY_REBOOT"


def _event_boot_id(event):
    attributes = event.get("attributes") or {}
    return attributes.get("boot_id") if isinstance(attributes, dict) else None


def _action_for(event_type):
    for suffix in _ACTION_SUFFIXES:
        if event_type.endswith(suffix):
            return suffix[1:].lower()
    return None


def _resource_for(event):
    return event.get("resource_type") or "unknown"


def summarize(events):
    """Return one user-facing lifecycle for every protection identifier."""
    grouped = defaultdict(list)
    for event in events:
        protection_id = event.get("protection_id")
        if protection_id:
            grouped[protection_id].append(event)

    lifecycles = []
    for protection_id, entries in grouped.items():
        entries.sort(key=lambda event: (event.get("ts_utc") or "", event.get("event_id") or ""))
        # A resource's current state is its LATEST action by time, not a cumulative
        # set: the same protection_id is reused across re-caps (see balancer
        # _protection_id reuse), so a resource applied -> recovered -> applied again
        # is active on its last action. Set subtraction (applied - recovered) would
        # wrongly drop it, hiding a live limit from the "active" read model.
        latest_action = {}
        ever_applied = []
        for event in entries:
            action = _action_for(event.get("event_type") or "")
            if action is None:
                continue
            resource = _resource_for(event)
            latest_action[resource] = action
            if action == "applied" and resource not in ever_applied:
                ever_applied.append(resource)

        applied = sorted(ever_applied)
        active = sorted(r for r, a in latest_action.items() if a == "applied")
        recovered = sorted(r for r, a in latest_action.items() if a == "recovered")
        failed = sorted(r for r, a in latest_action.items() if a == "failed")
        event_types = {event.get("event_type") for event in entries}
        if failed:
            status = "failed"
        elif active:
            status = "active"
        else:
            status = "recovered"
        if _REBOOT_CLEARED_EVENT in event_types:
            status = "cleared_by_reboot"
        elif _RECONCILIATION_EVENT in event_types and status == "active":
            status = "requires_verification"
        first = entries[0]
        last = entries[-1]
        lifecycles.append({
            "protection_id": protection_id,
            "status": status,
            "started_at": first.get("ts_utc"),
            "last_updated_at": last.get("ts_utc"),
            "app_id": first.get("app_id"),
            "source": first.get("source"),
            "boot_id": _event_boot_id(first),
            "applied_resources": applied,
            "recovered_resources": recovered,
            "failed_resources": failed,
            "active_resources": active,
            "events": entries,
        })
    return sorted(lifecycles, key=lambda item: item["last_updated_at"] or "", reverse=True)


def query(*, start_time=None, end_time=None, app_id=None, protection_id=None,
          limit=200, offset=0):
    """Build lifecycle read models from the append-only event ledger."""
    from diagnostics import event_store

    events = event_store.query_events(
        category="platform.control", start_time=start_time, end_time=end_time,
        app_id=app_id, protection_id=protection_id, limit=2000)
    # The newest-2000 window can push a still-active protection out of view on a
    # busy host. For the dashboard's "current protections" call (no time window,
    # no single-protection scope) fold in the full history of every unresolved
    # protection so a live resource limit is never dropped from the read model.
    if protection_id is None and start_time is None and end_time is None:
        active_ids = event_store.unresolved_protection_ids()
        if active_ids:
            seen = {event.get("event_id") for event in events}
            for event in event_store.events_for_protections(active_ids, app_id=app_id):
                if event.get("event_id") not in seen:
                    events.append(event)
                    seen.add(event.get("event_id"))
    return summarize(events)[max(0, offset):max(0, offset) + max(1, limit)]


def reconcile_interrupted_lifecycles(current_boot_id=None):
    """Classify unfinished lifecycles without changing live resource limits."""
    from diagnostics import emit_event, event_store

    current_boot_id = current_boot_id if current_boot_id is not None else read_boot_id()
    # Reconcile against the unresolved protections directly rather than the newest
    # 2000 events: an interrupted lifecycle that aged past that window must still be
    # classified, or it silently escapes the reboot/verification guarantee.
    active_ids = event_store.unresolved_protection_ids()
    existing_events = event_store.events_for_protections(active_ids) if active_ids else []
    markers = {
        event.get("protection_id") for event in existing_events
        if event.get("event_type") in {_RECONCILIATION_EVENT, _REBOOT_CLEARED_EVENT}
    }
    marked = []
    for lifecycle in summarize(existing_events):
        if lifecycle["status"] != "active" or lifecycle["protection_id"] in markers:
            continue
        lifecycle_boot_id = lifecycle.get("boot_id")
        rebooted = bool(current_boot_id and lifecycle_boot_id and lifecycle_boot_id != current_boot_id)
        event_type = _REBOOT_CLEARED_EVENT if rebooted else _RECONCILIATION_EVENT
        summary = (
            "Resource limit lifecycle ended because the system rebooted"
            if rebooted else
            "Resource limit lifecycle was interrupted and requires verification"
        )
        emit_event(
            event_type, severity="info" if rebooted else "warning", category="platform.control",
            source="diagnostics", app_id=lifecycle.get("app_id"),
            protection_id=lifecycle["protection_id"], impact="none" if rebooted else "degraded",
            summary=summary,
            attributes={
                "active_resources": lifecycle["active_resources"],
                "last_updated_at": lifecycle["last_updated_at"],
                "previous_boot_id": lifecycle_boot_id,
                "boot_id": current_boot_id,
            },
        )
        marked.append(lifecycle["protection_id"])
    return marked