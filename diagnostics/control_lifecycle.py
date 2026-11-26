# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Read and reconcile persisted resource-control lifecycle events.

What each event means to a lifecycle comes from
``event_catalog.LIFECYCLE_EVENTS``, never from its name.
"""

from collections import defaultdict
from datetime import datetime

from diagnostics import event_catalog
from diagnostics.system_info import normalize_boot_id, read_boot_id


_RECONCILIATION_EVENT = event_catalog.lifecycle_event_type("requires_verification")
_REBOOT_CLEARED_EVENT = event_catalog.lifecycle_event_type("cleared_by_reboot")
_RUNTIME_STATE_CLEARED_EVENT = event_catalog.lifecycle_event_type("cleared_without_runtime_state")
# Statuses of a lifecycle that may still hold a live limit. "failed" is one of
# them: one resource can fail while another stays limited.
_OPEN_STATUSES = ("active", "failed")
_CLEARABLE_STATUSES = ("active", "requires_verification")


def _holds_live_limit(lifecycle, statuses):
    return lifecycle["status"] in statuses and bool(lifecycle["active_resources"])


def _event_boot_id(event):
    attributes = event.get("attributes") or {}
    return normalize_boot_id(attributes.get("boot_id")) if isinstance(attributes, dict) else None


def summarize(events):
    """Return one user-facing lifecycle for every protection identifier.

    Only lifecycle events count; other events (e.g. a memory reclaim) carry a protection id too.
    """
    grouped = defaultdict(list)
    for event in events:
        protection_id = event.get("protection_id")
        if protection_id and event_catalog.lifecycle_event(event.get("event_type")):
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
        markers = set()
        for event in entries:
            definition = event_catalog.lifecycle_event(event.get("event_type"))
            if definition.resource is None:  # a marker on the whole lifecycle
                markers.add(definition.action)
                continue
            resource = event.get("resource_type") or definition.resource
            latest_action[resource] = definition.action
            if definition.action == "applied" and resource not in ever_applied:
                ever_applied.append(resource)

        applied = sorted(ever_applied)
        active = sorted(r for r, a in latest_action.items() if a == "applied")
        recovered = sorted(r for r, a in latest_action.items() if a == "recovered")
        failed = sorted(r for r, a in latest_action.items() if a == "failed")
        if failed:
            status = "failed"
        elif active:
            status = "active"
        else:
            status = "recovered"
        if "cleared_by_reboot" in markers:
            status = "cleared_by_reboot"
        elif "cleared_without_runtime_state" in markers:
            status = "cleared_without_runtime_state"
        elif "requires_verification" in markers and active:
            # Also after a partial failure: resources still applied need the check too.
            status = "requires_verification"
        first = entries[0]
        last = entries[-1]
        attributes = first.get("attributes") or {}
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
            "cgroups": list(attributes.get("cgroups") or []),
            "events": entries,
        })
    return sorted(lifecycles, key=lambda item: item["last_updated_at"] or "", reverse=True)


_PROTECTION_ID_CHUNK = 500


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _full_histories(event_store, protection_ids, app_id):
    ids = sorted(protection_ids)
    events = []
    for index in range(0, len(ids), _PROTECTION_ID_CHUNK):
        events.extend(event_store.events_for_protections(
            ids[index:index + _PROTECTION_ID_CHUNK], app_id=app_id))
    return events


def query(*, start_time=None, end_time=None, app_id=None, protection_id=None,
          limit=200, offset=0):
    """Build lifecycle read models from the append-only event ledger.

    Lifecycles with an event in the window or a live limit are summarized from their
    whole history. ``limit`` / ``offset`` page only closed lifecycles.
    """
    from diagnostics import event_store

    if protection_id is not None:
        return summarize(_full_histories(event_store, {protection_id}, app_id))

    in_window = event_store.query_events(
        category="platform.control", start_time=start_time, end_time=end_time,
        app_id=app_id, limit=2000)
    open_ids = event_store.unresolved_protection_ids()
    ids = {event.get("protection_id") for event in in_window
           if event.get("protection_id") and event_catalog.lifecycle_event(event.get("event_type"))}
    lifecycles = summarize(_full_histories(event_store, ids | open_ids, app_id))
    if end_time is not None:
        lifecycles = [item for item in lifecycles
                      if (_epoch(item["started_at"]) or 0) <= end_time]
    live = [item for item in lifecycles if item["protection_id"] in open_ids]
    closed = [item for item in lifecycles if item["protection_id"] not in open_ids]
    page = closed[max(0, offset):max(0, offset) + max(1, limit)]
    return sorted(live + page, key=lambda item: item["last_updated_at"] or "", reverse=True)


def reconcile_interrupted_lifecycles(current_boot_id=None):
    """Classify unfinished lifecycles without changing live resource limits."""
    from diagnostics import emit_event, event_store

    current_boot_id = normalize_boot_id(
        current_boot_id if current_boot_id is not None else read_boot_id())
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
        if (not _holds_live_limit(lifecycle, _OPEN_STATUSES)
                or lifecycle["protection_id"] in markers):
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
    marked.extend(_clear_failed_from_previous_boots(current_boot_id))
    return marked


def _clear_failed_from_previous_boots(current_boot_id):
    """Close limit-failed alerts whose protection is from an earlier boot (its limit is gone)."""
    if not current_boot_id:
        return []
    from db.DatabaseModel import AlertState
    from diagnostics import emit_event, event_store

    failed = [alert for alert in AlertState.active_alerts()
              if event_catalog.lifecycle_action(alert.event_type) == "failed" and alert.scope]
    if not failed:
        return []
    events = event_store.events_for_protections(sorted({alert.scope for alert in failed}))
    boots = defaultdict(set)
    markers = set()
    for event in events:
        boot = _event_boot_id(event) or normalize_boot_id(event.get("boot_id"))
        if boot:
            boots[event["protection_id"]].add(boot)
        if event.get("event_type") == _REBOOT_CLEARED_EVENT:
            markers.add(event["protection_id"])
    now = event_store.now_iso()
    cleared = []
    for alert in failed:
        protection_id = alert.scope
        seen = boots.get(protection_id)
        if not seen or current_boot_id in seen:
            continue  # unknown boot, or this boot: the cap may still be in place
        AlertState.resolve(event_type=alert.event_type, scope=protection_id,
                           ts_utc=now, reason="cleared_by_reboot")
        if protection_id not in markers:
            emit_event(
                _REBOOT_CLEARED_EVENT, severity="info", category="platform.control",
                source="diagnostics", protection_id=protection_id, impact="none",
                summary="Resource limit lifecycle ended because the system rebooted",
                attributes={"previous_boot_id": sorted(seen)[-1], "boot_id": current_boot_id},
            )
            markers.add(protection_id)
            cleared.append(protection_id)
    return cleared


def clear_without_runtime_state(protection_id):
    """Close an orphaned lifecycle that cannot be checked or restored.

    This is only appropriate after the caller has confirmed there is no live
    runtime limit and the original event did not retain a cgroup target.
    """
    from diagnostics import emit_event, event_store

    events = event_store.events_for_protections([protection_id])
    lifecycles = summarize(events)
    # Usually requires_verification: the startup reconcile marks interrupted lifecycles,
    # and an orphan without cgroups can never be verified.
    if len(lifecycles) != 1 or not _holds_live_limit(lifecycles[0], _CLEARABLE_STATUSES):
        return False
    lifecycle = lifecycles[0]
    if lifecycle["cgroups"]:
        return False
    emit_event(
        _RUNTIME_STATE_CLEARED_EVENT, severity="info", category="platform.control",
        source="diagnostics", app_id=lifecycle.get("app_id"),
        protection_id=protection_id, impact="none",
        summary="Resource limit lifecycle cleared because no runtime state is available",
        attributes={"active_resources": lifecycle["active_resources"],
                    "last_updated_at": lifecycle["last_updated_at"]},
    )
    return True