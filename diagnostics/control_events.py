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
"""

from diagnostics.emitter import emit_event
from diagnostics.system_info import read_boot_id

_ACTIONS = {"APPLIED", "RECOVERED", "FAILED"}


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
        for pid in app_utils.get_pids_in_cgroup(cgroup):
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


def record_control_action(action, *, app_id, app_name, protection_id,
                          resources, facts=None, source="balancer"):
    """Record one resource-scoped event per resource for a completed control action.

    ``action`` is one of APPLIED / RECOVERED / FAILED. ``resources`` is the
    caller-decided set of affected resources in the neutral vocabulary; an empty
    set records nothing (e.g. a re-cap with no new channel, or a FAILED action
    where nothing actually failed). ``facts`` are opaque, already-settled context
    values carried onto the event attributes; ``cgroups`` / ``pids`` among them
    drive the process snapshot.
    """
    if action not in _ACTIONS:
        return
    resources = [resource for resource in (resources or []) if resource]
    if not resources:
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

    for resource in resources:
        emit_event(
            f"CONTROL_{resource}_LIMIT_{action}", severity=severity,
            category="platform.control", source=source, app_id=app_id,
            impact=impact, resource_type=resource.lower(), protection_id=protection_id,
            summary=f"{resource.replace('_', ' ').title()} limit {action.lower()} for {app_name}",
            attributes=attributes,
            identity=f"{protection_id}:{action}:{resource}",
        )
