# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Turn confirmed resource-control facts into the CONTROL_* event ledger.

This is the diagnostics side of the control seam. The balancer decides *what*
happened -- which resources were capped or lifted, under which protection id --
and hands those already-settled facts here as a neutral resource vocabulary
(``CPU`` / ``MEMORY`` / ``DISK_IO``). This module owns the event *protocol*: the
reason-code expansion, severity/impact, the human summary, boot-id enrichment
and the process snapshot. The balancer therefore never spells a CONTROL_* event
type, and this module never reads the balancer's internal limit state.

``record_control_action`` records the resource-limit lifecycle, ``record_control_change``
any other control change. Both copy the facts, stamp the event and return at once; the
work runs on the diagnostics writer thread.
"""

from diagnostics import event_catalog, writer
from diagnostics.emitter import emit_event
from diagnostics.system_info import read_boot_id
from utils.logger import get_logger

logger = get_logger(__name__)

# How long a control fact waits for room in a full writer queue before it is dropped
# (other events do not wait: the active-protections read model is rebuilt from these).
_CONTROL_WAIT_SECONDS = 2.0

# The limit actions callers report, one per lifecycle resource action.
_ACTIONS = tuple(action.upper() for action in event_catalog.RESOURCE_ACTIONS)


def _process_name(pid):
    import psutil

    try:
        return psutil.Process(pid).name() or "unknown"
    except (psutil.Error, OSError, ValueError):
        return "unknown"


def _scope_process_snapshot(cgroups, fallback_pids, app_name):
    import psutil
    from utils import app_utils

    snapshot = {}
    for cgroup in cgroups:
        processes = []
        for pid in app_utils.read_cgroup_pids(cgroup):
            try:
                process = psutil.Process(pid)
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    continue
                processes.append({
                    "pid": pid,
                    "process_name": process.name() or app_name,
                    "cmdline": " ".join(process.cmdline()).strip(),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        snapshot[cgroup] = processes
    if not any(snapshot.values()) and fallback_pids:
        snapshot[cgroups[0] if cgroups else ""] = [
            {"pid": pid, "process_name": _process_name(pid), "cmdline": ""}
            for pid in fallback_pids
        ]
    return snapshot


def _cycle(protection_id, resource, action):
    """Which cap of ``resource`` under ``protection_id`` this action belongs to.

    Protection ids are reused across re-caps, so this count of APPLIED events goes into
    the dedup identity; a FAILED or RECOVERED retried within one cap keeps the same cycle.
    """
    from diagnostics import event_store

    types = [event_catalog.lifecycle_event_type(name, resource)
             for name in event_catalog.RESOURCE_ACTIONS]
    applied_type = types[0]
    rows = event_store.query_events(protection_id=protection_id, event_types=types, limit=1000)
    applied = sum(1 for row in rows if row.get("event_type") == applied_type)
    if action == "APPLIED" and rows and rows[0].get("event_type") == applied_type:
        return applied - 1  # the open cap reported again, not a new one
    return applied


def record_control_action(action, *, app_id, app_name, protection_id,
                          resources, facts=None, source="balancer"):
    """Record one resource-scoped event per resource for a completed control action.

    ``action`` is one of APPLIED / RECOVERED / FAILED. ``resources`` is the
    caller-decided set of affected resources in the neutral vocabulary; an empty
    set records nothing (e.g. a re-cap with no new channel, or a FAILED action
    where nothing actually failed). ``facts`` are opaque, already-settled context
    values carried onto the event attributes; ``cgroups`` / ``pids`` among them
    drive the process snapshot. Resources without a lifecycle event are skipped.
    Returns False if the writer dropped the action.
    """
    resources = [resource for resource in (resources or []) if resource]
    if action not in _ACTIONS or not resources:
        return True
    return writer.submit(
        _record_control_action, action, app_id=app_id, app_name=app_name,
        protection_id=protection_id, resources=resources,
        facts=writer.settled(facts or {}), source=source, ts_utc=writer.now_iso(),
        wait=_CONTROL_WAIT_SECONDS)


def _record_control_action(action, *, app_id, app_name, protection_id, resources, facts,
                           source, ts_utc):
    event_types = {}
    for resource in resources:
        event_type = event_catalog.lifecycle_event_type(action, resource)
        if event_type is None:
            logger.warning("No lifecycle event for %s on %s; not recorded", action, resource)
            continue
        event_types[resource] = event_type
    if not event_types:
        return

    severity = "error" if action == "FAILED" else "info"
    impact = "failed" if action == "FAILED" else ("degraded" if action == "APPLIED" else "none")

    attributes = dict(facts or {})
    attributes["boot_id"] = read_boot_id()
    cgroups = [str(value) for value in attributes.get("cgroups", []) if value]
    pids = [int(value) for value in attributes.get("pids", []) if value]
    if cgroups:
        attributes["cgroups"] = cgroups
        attributes["scope_processes"] = _scope_process_snapshot(cgroups, pids, app_name)
    elif pids:
        attributes["scope_processes"] = {
            "": [{"pid": pid, "process_name": _process_name(pid), "cmdline": ""} for pid in pids]
        }
        attributes.pop("pids", None)

    for resource, event_type in event_types.items():
        emit_event(
            event_type, severity=severity,
            category="platform.control", source=source, app_id=app_id,
            impact=impact, resource_type=resource.lower(), protection_id=protection_id,
            summary=f"{resource.replace('_', ' ').title()} limit {action.lower()} for {app_name}",
            attributes=attributes, ts_utc=ts_utc,
            identity=f"{protection_id}:{action}:{resource}:{_cycle(protection_id, resource, action)}",
        )


# Control changes outside the limit lifecycle (freeze, CPU affinity, ...). They leave the
# protection_id column, which keys that lifecycle, empty; facts carry ``limit_protection_id``.


class _FactFormat(dict):
    """str.format_map source: a placeholder with no matching fact reads "?"."""

    def __missing__(self, key):
        return "?"


def record_control_change(family, action, *, app_id, app_name, cgroups=None, pids=None,
                          trigger="user", facts=None, source="balancer"):
    """Record an app's control change that is not a resource-limit action.

    ``family`` / ``action`` select the event in ``event_catalog.CONTROL_ACTION_EVENTS``;
    ``trigger`` names the cause (user, restore, app_closed, shutdown, startup_recovery, ...);
    ``facts`` go onto the attributes and into its summary template. Returns False for an
    unknown change or one the writer dropped.
    """
    spec = event_catalog.control_action(family, action)
    if spec is None:
        logger.warning("Unknown control change %s/%s; not recorded", family, action)
        return False
    return writer.submit(
        _record_control_change, spec, app_id=app_id, app_name=app_name,
        cgroups=[str(value) for value in (cgroups or []) if value],
        pids=[int(pid) for pid in (pids or []) if pid],
        trigger=trigger, facts=writer.settled(facts or {}), source=source, ts_utc=writer.now_iso(),
        wait=_CONTROL_WAIT_SECONDS)


def _record_control_change(spec, *, app_id, app_name, cgroups, pids, trigger, facts, source,
                           ts_utc):
    # A startup-recovery thaw knows only the cgroup, not the app.
    app_label = app_name or app_id or ", ".join(cgroups) or "unknown app"
    attributes = {key: value for key, value in facts.items() if value is not None}
    attributes["app_name"] = app_name
    attributes["trigger"] = trigger
    attributes["boot_id"] = read_boot_id()
    if cgroups:
        attributes["cgroups"] = cgroups
    if spec.snapshot and (cgroups or pids):
        attributes["scope_processes"] = _scope_process_snapshot(cgroups, pids, app_label)
    emit_event(
        spec.event_type, severity=spec.severity, category="platform.control", source=source,
        app_id=app_id, impact=spec.impact, resource_type=spec.resource_type,
        summary=spec.summary.format_map(_FactFormat(attributes, app=app_label)),
        attributes=attributes, ts_utc=ts_utc,
    )
