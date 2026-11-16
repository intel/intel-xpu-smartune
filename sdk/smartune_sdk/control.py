# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune control`: the balancer actions an agent may take on a controlled app
# (limit, restore, freeze, priority, CPU affinity, taking over an auto limit, ...),
# registering an app for control in the first place (register / unregister), and
# device-wide CPU / GPU frequency limits (freq-limit / freq-restore).
#
# Every action is plan first, apply second:
#
#   smartune control limit <app_id> --cpu 30          prints the plan, changes nothing
#   smartune control limit <app_id> --cpu 30 ... --yes  re-checks the plan, then applies it
#
# The plan is built from the server's live state: the app must exist and be in a
# state the action applies to, values must sit inside the bounds the server
# reports (resource_limit_profile), and it shows the current value next to the
# target. `apply_with` is the plan written out as one fully explicit command
# (every value pinned, plus --expect <control_status>), so applying it does
# exactly what was shown -- and refuses if the app changed state in between.
# After applying, the command reads the state back and returns before/after.
#
# Only the endpoints in client.CONTROL_ROUTES are reachable, through
# client.control(). The consent rules agents follow are in agent/policy/consent.md.

import os
import re
import shlex

from .client import EXIT_REFUSED, EXIT_USAGE, SmartuneError
from .shape import pick

PRIORITIES = ("low", "medium", "high", "critical")
# balance_service.normalize_network_priority: medium is folded into low.
NETWORK_PRIORITIES = ("low", "high", "critical", "system")
AFFINITY_CLASSES = ("p", "e", "lpe", "custom", "restore")
CONTROL_STATES = ("NORMAL", "MANUAL_LIMITED", "AUTO_LIMITED")
DISK_FIELDS = (("read", "disk_read", "MB/s"), ("write", "disk_write", "MB/s"),
               ("read_iops", "disk_read_iops", "IOPS"), ("write_iops", "disk_write_iops", "IOPS"))
CPU_LIST_RE = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*\Z")
MAX_LISTED_PROCESSES = 10

# Written at the start of the remark of every app `control register` adds.
# `control unregister` deletes only apps carrying it: an app someone registered
# from the dashboard is deleted from the dashboard.
REGISTER_MARK = "[smartune-control]"
APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}\Z")
# A match rule naming one of these would take every interpreter, shell or
# container runtime on the machine under control, not one application.
GENERIC_PROCESS_NAMES = frozenset({
    "python", "python2", "python3", "bash", "sh", "dash", "zsh", "fish", "node", "nodejs",
    "java", "perl", "ruby", "php", "lua", "env", "sudo", "su", "nohup", "timeout", "xargs",
    "docker", "dockerd", "containerd", "containerd-shim", "runc", "podman", "conmon",
    "flatpak", "snap", "bwrap", "tmux", "screen", "sshd", "ssh",
})
# Never registered by an agent: SmarTune itself, the agents that drive it, and
# processes the desktop or the system cannot run without.
PROTECTED_NAME_PARTS = ("smartune", "openclaw", "claude", "codex", "gnai", "copilot")
PROTECTED_PROCESS_NAMES = frozenset({
    "systemd", "init", "dbus-daemon", "dbus-broker", "xorg", "xwayland", "gnome-shell",
    "kwin_wayland", "kwin_x11", "plasmashell", "gdm", "sddm", "pipewire", "wireplumber",
    "pulseaudio", "networkmanager", "systemd-journald", "systemd-logind", "polkitd",
})

NOT_APPLIED = ("Nothing was changed. Show this plan to the user and run `apply_with` only after "
               "they explicitly agree to this change.")


def command(argv):
    return "smartune " + " ".join(shlex.quote(str(a)) for a in argv)


def _num(value):
    """30.0 -> '30', 12.5 -> '12.5': how a value is written back into a command."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


class Plan:
    """What one control action would do, and how to carry it out."""

    def __init__(self, action, argv, app=None):
        self.action = action
        self.argv = argv          # the `smartune` argv that reproduces this plan (no --yes)
        self.app = app or {}
        self.summary = ""
        self.current = {}
        self.target = {}
        self.effects = []
        self.blockers = []
        self.no_change = None     # why applying would change nothing
        self.path = None          # CONTROL_ROUTES endpoint and its body
        self.body = None
        self.bodies = None        # several bodies for the same endpoint, sent in order
        self.undo = None          # argv of the command that plans the reverse change
        self.undo_note = None
        self.read_back = None     # client -> state after applying
        self.apply_hint = None    # replaces the hint of an error from the write itself

    @property
    def can_apply(self):
        return not self.blockers and not self.no_change

    def preview(self):
        out = {"dry_run": True, "action": self.action, "summary": self.summary, "app": self.app,
               "current": self.current, "target": self.target, "effects": self.effects,
               "can_apply": self.can_apply}
        if self.blockers:
            out["blockers"] = self.blockers
        if self.no_change:
            out["no_change"] = self.no_change
        if self.can_apply:
            out["apply_with"] = command(self.argv + ["--yes"])
            out["note"] = NOT_APPLIED
        self._undo_fields(out)
        return out

    def _undo_fields(self, out):
        if self.undo:
            out["undo_with"] = command(self.undo)
        if self.undo_note:
            out["undo_note"] = self.undo_note


def run(builder):
    """A CLI handler: print the plan, or with --yes apply it and read the result back."""
    def handler(client, args):
        plan = builder(client, args)
        if not args.yes:
            return plan.preview()
        if plan.blockers:
            raise SmartuneError(
                f"Refused to {plan.action}: " + " ".join(plan.blockers),
                hint="Nothing was changed. Run the command without --yes to see the current plan.",
                exit_code=EXIT_REFUSED)
        if plan.no_change:
            return {"applied": False, "action": plan.action, "app": plan.app,
                    "no_change": plan.no_change}
        result = {}
        try:
            # Usually one request; freq-limit sends one per distinct min/max pair.
            for body in plan.bodies or [plan.body]:
                result = client.control(plan.path, body) or {}
        except SmartuneError as exc:
            if plan.apply_hint:
                exc.hint = plan.apply_hint
            raise
        data = result.get("data") or {}
        out = {"applied": True, "action": plan.action, "app": plan.app,
               "message": result.get("message"), "before": plan.current, "requested": plan.target}
        if isinstance(data, dict) and data.get("skipped"):
            # resource_limit reports "evaluated, deliberately did nothing" this way.
            out["applied"] = False
            out["skipped"] = True
        if plan.read_back:
            try:
                out["after"] = plan.read_back(client)
            except SmartuneError as exc:
                out["after"] = exc.to_dict()
        plan._undo_fields(out)
        return out
    return handler


# -- reading the current state -------------------------------------------------

def _controlled_row(client, app_id):
    return next((a for a in client.controlled_apps() if a.get("app_id") == app_id), None)


def _require_controlled(client, app_id):
    row = _controlled_row(client, app_id)
    if row is None:
        raise SmartuneError(
            f"No controlled app has app_id '{app_id}'",
            hint="List them with `smartune apps`. Registering a new app is done from the SmarTune dashboard.",
            exit_code=EXIT_USAGE)
    return row


def _status(row):
    return (row or {}).get("control_status") or "NORMAL"


def _app_view(row):
    view = pick(row, "app_id", "app_name", "priority", "network_priority", "status")
    view["control_status"] = _status(row)
    if row.get("frozen"):
        view["frozen"] = True
    if row.get("cpu_affinity"):
        view["cpu_affinity"] = row["cpu_affinity"]
    return view


def _pct(rate):
    return round(rate * 100, 1) if isinstance(rate, (int, float)) else None


def _limit_view(row):
    """The limit in force, from a controlled-app row, an auto row or a limit snapshot."""
    view = {"control_status": _status(row)}
    effective = (row or {}).get("effective") or {}
    cpu_mem = effective.get("cpu_mem") or {}
    if cpu_mem.get("limited"):
        view["cpu_pct_of_usage"] = _pct(cpu_mem.get("cpu_rate"))
        view["memory_pct_of_usage"] = _pct(cpu_mem.get("mem_rate"))
    disk = effective.get("disk_io") or {}
    if disk.get("limited"):
        view["disk_io"] = pick(disk, "read_mb_s", "write_mb_s", "read_iops", "write_iops", "disks")
    if (row or {}).get("frozen"):
        view["frozen"] = True
    return view


def _check_expect(plan, args, status):
    expect = getattr(args, "expect", None)
    if expect and expect != status:
        plan.blockers.append(
            f"The app's state changed since the plan was made (planned for {expect}, now {status}); "
            "plan again and show the user the new plan.")


def _app_argv(action, app_id, status, *extra):
    return ["control", action, app_id, *extra, "--expect", status]


def _snapshot_view(app_id):
    def read(client):
        snap = client.limit_snapshot(app_id) or {}
        return _limit_view(snap)
    return read


def _row_view(app_id):
    def read(client):
        row = _controlled_row(client, app_id)
        return _app_view(row) if row else {"note": "the app is no longer in the controlled list"}
    return read


# -- read-only helpers ---------------------------------------------------------

def cmd_profile(client, args):
    """Limit defaults and bounds for one app: what `control limit` accepts."""
    row = _require_controlled(client, args.app_id)
    priority = row.get("priority") or "medium"
    profile = client.resource_limit_profile(args.app_id, row.get("app_name"), priority) or {}
    return {
        "app": _app_view(row),
        "current_limit": _limit_view(row),
        "cpu_pct": pick(profile.get("cpu") or {}, "enabled", "value", "min", "max"),
        "memory_pct": pick(profile.get("memory") or {}, "enabled", "value", "min", "max"),
        "disk_io": {k: (v.get("value") if isinstance(v, dict) else v)
                    for k, v in (profile.get("disk_io") or {}).items()},
        "cgroups": profile.get("cgroup_ids") or [],
        "processes": (profile.get("target_processes") or [])[:MAX_LISTED_PROCESSES],
        "units": "cpu_pct / memory_pct are a share of the app's own current usage, bounded per "
                 "priority; disk_io read/write in MB/s and IOPS, absolute",
    }


def cmd_exclusions(client, args):
    rows = client.auto_limit_exclusions()
    return {"count": len(rows), "exclusions": rows,
            "note": "Excluded apps are never limited automatically. The list is kept in memory "
                    "and cleared when the SmarTune service restarts."}


# -- limit ---------------------------------------------------------------------

def _bounded(name, value, bounds, priority):
    lo, hi = bounds.get("min"), bounds.get("max")
    if value <= 0 or (lo is not None and value < lo) or (hi is not None and value > hi):
        raise SmartuneError(
            f"--{name} {_num(value)} is outside the allowed range for a {priority}-priority app",
            hint=f"Use a value from {_num(lo)} to {_num(hi)} (percent of the app's own current usage); "
                 f"`smartune control profile <app_id>` shows the range.",
            exit_code=EXIT_USAGE)
    return value


def plan_limit(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    priority = row.get("priority") or "medium"
    profile = client.resource_limit_profile(args.app_id, row.get("app_name"), priority) or {}
    cpu_p, mem_p, disk_p = (profile.get(k) or {} for k in ("cpu", "memory", "disk_io"))

    def channel(profile_part, value, off):
        if off:
            return False, None
        if value is not None:
            return True, value
        return bool(profile_part.get("enabled")), profile_part.get("value")

    cpu_on, cpu = channel(cpu_p, args.cpu, args.no_cpu)
    mem_on, mem = channel(mem_p, args.mem, args.no_mem)
    if cpu_on:
        cpu = _bounded("cpu", cpu, cpu_p, priority)
    if mem_on:
        mem = _bounded("mem", mem, mem_p, priority)

    disk_given = {key: getattr(args, attr) for key, attr, _ in DISK_FIELDS if getattr(args, attr) is not None}
    if args.no_disk and disk_given:
        raise SmartuneError("--no-disk cannot be combined with --disk-* values",
                            hint="Pass either --no-disk or the disk caps you want.", exit_code=EXIT_USAGE)
    disk_on = not args.no_disk and (bool(disk_given) or bool(disk_p.get("enabled")))
    disk = {}
    for key, attr, _ in DISK_FIELDS:
        value = disk_given.get(key, (disk_p.get(key) or {}).get("value"))
        if disk_on and (not isinstance(value, (int, float)) or value < 1):
            raise SmartuneError(f"--{attr.replace('_', '-')} must be at least 1",
                                hint="Disk limits are absolute MB/s and IOPS; to lift one, use `smartune control restore`.",
                                exit_code=EXIT_USAGE)
        disk[key] = int(value) if isinstance(value, (int, float)) else value

    if not (cpu_on or mem_on or disk_on):
        raise SmartuneError("Nothing to limit: CPU, memory and disk IO are all off",
                            hint="Pass at least one of --cpu, --mem or a --disk-* value.",
                            exit_code=EXIT_USAGE)

    cgroups = profile.get("cgroup_ids") or []
    if args.cgroup:
        unknown = [c for c in args.cgroup if c not in cgroups]
        if unknown:
            raise SmartuneError(f"Not a running instance of this app: {', '.join(unknown)}",
                                hint="`smartune control profile <app_id>` lists its cgroups.",
                                exit_code=EXIT_USAGE)
    targets = list(args.cgroup or cgroups)

    argv = ["control", "limit", args.app_id]
    argv += ["--cpu", _num(cpu)] if cpu_on else ["--no-cpu"]
    argv += ["--mem", _num(mem)] if mem_on else ["--no-mem"]
    if disk_on:
        for key, attr, _ in DISK_FIELDS:
            argv += ["--" + attr.replace("_", "-"), _num(disk[key])]
    else:
        argv += ["--no-disk"]
    for c in args.cgroup or []:
        argv += ["--cgroup", c]
    argv += ["--expect", status]

    plan = Plan("limit", argv, _app_view(row))
    plan.current = _limit_view(row)
    plan.target = {"cpu_pct_of_usage": cpu if cpu_on else "off",
                   "memory_pct_of_usage": mem if mem_on else "off",
                   "disk_io": ({key: disk[key] for key, _, _ in DISK_FIELDS} if disk_on else "off"),
                   "cgroups": targets,
                   "processes": (profile.get("target_processes") or [])[:MAX_LISTED_PROCESSES]}
    parts = []
    if cpu_on:
        parts.append(f"CPU to {_num(cpu)}%")
        plan.effects.append(f"CPU is capped at {_num(cpu)}% of what the app uses now (cpu.max).")
    if mem_on:
        parts.append(f"memory to {_num(mem)}%")
        plan.effects.append(
            f"Memory gets a soft cap (memory.high) at {_num(mem)}% of what the app holds now (RSS + swap). "
            "SmarTune reclaims down to it right away, which pushes pages to swap and can make the app "
            "noticeably slower.")
    if disk_on:
        parts.append("disk IO")
        plan.effects.append(
            f"Disk IO is capped at read {disk['read']} MB/s, write {disk['write']} MB/s, "
            f"{disk['read_iops']} read IOPS and {disk['write_iops']} write IOPS (io.max; absolute values).")
    plan.summary = f"Limit {row.get('app_name') or args.app_id}: " + ", ".join(parts)
    if status == "MANUAL_LIMITED":
        plan.effects.append("This replaces the app's current manual limit.")
    plan.effects += [
        f"Applies to {len(targets)} running instance(s) of the app.",
        "These values are saved as the app's limit settings and become its defaults next time.",
        "A manual limit stays until it is restored or the SmarTune service restarts; automatic "
        "control leaves the app alone meanwhile.",
    ]

    _check_expect(plan, args, status)
    if status == "AUTO_LIMITED":
        plan.blockers.append(
            "SmarTune is already limiting this app automatically. Take the limit over first with "
            f"`smartune control lock {args.app_id}`, or release it with `smartune control restore {args.app_id}`.")
    if not targets:
        plan.blockers.append("The app has no running process to limit.")

    plan.path = "/app/resource_limit"
    plan.body = {
        "app_id": args.app_id, "app_name": row.get("app_name"), "priority": priority,
        "target_cgroups": targets,
        "limit_overrides": {
            "cpu": {"enabled": cpu_on, "rate": (cpu if cpu_on else cpu_p.get("value") or 0) / 100},
            "memory": {"enabled": mem_on, "rate": (mem if mem_on else mem_p.get("value") or 0) / 100},
            "disk_io": {"enabled": disk_on, "rate": disk},
        },
    }
    plan.undo = ["control", "restore", args.app_id]
    plan.read_back = _snapshot_view(args.app_id)
    return plan


# -- restore -------------------------------------------------------------------

def _auto_row(client, app_id):
    auto = client.auto_limited_apps() or {}
    for row in auto.get("apps") or []:
        if app_id in (row.get("app_id"), row.get("effective_app_id")):
            return row, auto.get("sys_pressure_level")
    return None, auto.get("sys_pressure_level")


def plan_restore(client, args):
    row = _controlled_row(client, args.app_id)
    auto_row, pressure = (None, None)
    if row is None or _status(row) == "AUTO_LIMITED":
        auto_row, pressure = _auto_row(client, args.app_id)
    if row is None and auto_row is None:
        raise SmartuneError(
            f"No controlled or automatically limited app has app_id '{args.app_id}'",
            hint="Use an app_id from `smartune apps` or `smartune limits`.", exit_code=EXIT_USAGE)
    source = row or auto_row
    status = "AUTO_LIMITED" if auto_row is not None else _status(row)
    name = source.get("app_name") or args.app_id

    plan = Plan("restore", _app_argv("restore", args.app_id, status),
                _app_view(row) if row else {**pick(auto_row, "app_id", "app_name", "priority"),
                                            "control_status": status, "controlled": False})
    plan.current = _limit_view(source)
    plan.target = {"control_status": "NORMAL"}
    _check_expect(plan, args, status)

    if status == "MANUAL_LIMITED":
        plan.summary = f"Lift the manual limit on {name}"
        plan.effects = [
            "Removes the app's CPU, memory and disk IO limits" + (" and thaws it." if source.get("frozen") else "."),
            "The app becomes a candidate for automatic limiting again if pressure gets critical.",
            "CPU affinity is not changed (that is `smartune control affinity <app_id> restore`).",
        ]
        plan.path = "/app/resource_restore"
        plan.undo = ["control", "limit", args.app_id]
        plan.undo_note = "Planning `control limit` again without values reuses the saved limit settings."
    elif status == "AUTO_LIMITED":
        plan.summary = f"Release the automatic limit on {name}"
        plan.effects = [
            "Releases the limit SmarTune applied because of system pressure, right away.",
            "Excludes the app from automatic limiting until the SmarTune service restarts, so the next "
            "critical tick does not limit it again.",
        ]
        if pressure in ("high", "critical"):
            plan.effects.append(
                f"System pressure is {pressure} now: releasing the limit can bring it back up. "
                f"`smartune control lock {args.app_id}` keeps the limit and hands it to the user instead.")
        plan.current["system_pressure_level"] = pressure
        plan.path = "/app/auto_limit_restore"
        plan.undo = ["control", "exclusion-remove", args.app_id]
        plan.undo_note = ("That only makes the app a candidate for automatic limiting again; it does not "
                          "re-apply the limit.")
    else:
        plan.summary = f"{name} is not limited"
        plan.no_change = "The app has no limit to restore."
    plan.body = {"app_id": args.app_id}
    plan.read_back = _snapshot_view(args.app_id)
    return plan


# -- lock (take over an auto limit) ---------------------------------------------

def plan_lock(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    plan = Plan("lock", _app_argv("lock", args.app_id, status), _app_view(row))
    plan.summary = f"Take over the automatic limit on {row.get('app_name') or args.app_id} as a manual limit"
    plan.current = _limit_view(row)
    plan.target = {**{k: v for k, v in plan.current.items() if k != "control_status"},
                   "control_status": "MANUAL_LIMITED"}
    plan.effects = [
        "The limit stays exactly where it is (no cgroup is written); only its owner changes from "
        "SmarTune's automatic control to the user.",
        "SmarTune will no longer relax it as pressure drops: it stays until restored or until the "
        "service restarts.",
        "Afterwards `smartune control limit`, `restore` and `freeze` apply to the app.",
    ]
    _check_expect(plan, args, status)
    if status != "AUTO_LIMITED":
        plan.no_change = "The app is not limited automatically, so there is nothing to take over."
    plan.path = "/app/lock_to_manual"
    plan.body = {"app_id": args.app_id}
    plan.undo_note = ("There is no way back to automatic ownership; "
                      f"`smartune control restore {args.app_id}` lifts the limit.")
    plan.read_back = _snapshot_view(args.app_id)
    return plan


# -- freeze / thaw ----------------------------------------------------------------

def _plan_freeze(client, args, freeze):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    name = row.get("app_name") or args.app_id
    action = "freeze" if freeze else "thaw"
    plan = Plan(action, _app_argv(action, args.app_id, status), _app_view(row))
    frozen = bool(row.get("frozen"))
    plan.current = {"control_status": status, "frozen": frozen}
    plan.target = {"control_status": status, "frozen": freeze}
    _check_expect(plan, args, status)
    if freeze:
        plan.summary = f"Freeze {name}: pause all of its processes"
        plan.effects = [
            "Every process of the app is paused (cgroup.freeze): it stops running until thawed. Nothing "
            "is killed, but anything waiting on the app (requests, clients) stalls meanwhile.",
            "There is no automatic thaw; SmarTune only sends a reminder after a while.",
            "A GPU or NPU is freed only once the jobs already submitted finish (about 20 s on an NPU).",
        ]
        if status != "MANUAL_LIMITED":
            plan.blockers.append(
                "Only an app with a manual limit can be frozen. Limit it first with "
                f"`smartune control limit {args.app_id}`" + (
                    f", or take over its automatic limit with `smartune control lock {args.app_id}`."
                    if status == "AUTO_LIMITED" else "."))
        elif frozen:
            plan.no_change = "The app is already frozen."
        plan.undo = ["control", "thaw", args.app_id]
    else:
        plan.summary = f"Thaw {name}: let its processes run again"
        plan.effects = ["The app resumes; its manual limit stays in place."]
        if not frozen:
            plan.no_change = "The app is not frozen."
        plan.undo = ["control", "freeze", args.app_id]
    plan.path = "/app/freeze"
    plan.body = {"app_id": args.app_id, "freeze": freeze}
    plan.read_back = _snapshot_view(args.app_id)
    return plan


def plan_freeze(client, args):
    return _plan_freeze(client, args, True)


def plan_thaw(client, args):
    return _plan_freeze(client, args, False)


# -- priorities ---------------------------------------------------------------------

def plan_priority(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    old = row.get("priority")
    plan = Plan("priority", _app_argv("priority", args.app_id, status, args.priority), _app_view(row))
    plan.summary = f"Set the priority of {row.get('app_name') or args.app_id} to {args.priority}"
    plan.current = {"priority": old}
    plan.target = {"priority": args.priority}
    plan.effects = [
        "Priority decides how SmarTune treats the app under pressure: lower-priority apps are limited "
        "first and harder, and queued launches start in priority order.",
        "It also sets the default limit values and their allowed range for manual limits.",
        "The new priority is saved and kept across restarts.",
    ]
    if args.priority == "critical":
        plan.effects.append("Critical also lowers the app's OOM score so the kernel is less likely "
                            "to kill it when memory runs out; moving it off critical later does not "
                            "undo that until the app restarts.")
    if status != "NORMAL":
        plan.effects.append("The limit in force now is not changed.")
    _check_expect(plan, args, status)
    if old == args.priority:
        plan.no_change = f"The priority is already {old}."
    plan.path = "/app/set_priority"
    plan.body = {"app_id": args.app_id, "priority": args.priority}
    if old in PRIORITIES:
        plan.undo = ["control", "priority", args.app_id, old]
    plan.read_back = _row_view(args.app_id)
    return plan


def plan_network_priority(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    old = row.get("network_priority")
    plan = Plan("net-priority", _app_argv("net-priority", args.app_id, status, args.priority),
                _app_view(row))
    plan.summary = f"Put {row.get('app_name') or args.app_id}'s network traffic in the {args.priority} class"
    plan.current = {"network_priority": old}
    plan.target = {"network_priority": args.priority}
    plan.effects = [
        "When network pressure is critical SmarTune slows the low class first, then high; critical "
        "and system are never slowed.",
        "This only has an effect when network control is enabled in SmarTune's configuration.",
        "The new class is saved and kept across restarts.",
    ]
    _check_expect(plan, args, status)
    if old == args.priority:
        plan.no_change = f"The network priority is already {old}."
    plan.path = "/app/set_network_priority"
    plan.body = {"app_id": args.app_id, "network_priority": args.priority}
    if old in NETWORK_PRIORITIES:
        plan.undo = ["control", "net-priority", args.app_id, old]
    plan.read_back = _row_view(args.app_id)
    return plan


# -- CPU affinity --------------------------------------------------------------------

AFFINITY_LABELS = {"p": "the P (performance) cores", "e": "the E (efficiency) cores",
                   "lpe": "the LPE (low-power efficiency) cores"}


def plan_affinity(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    cls = args.cpu_class
    if cls == "custom" and not args.cpus:
        raise SmartuneError("custom needs --cpus, e.g. --cpus 0-3,8",
                            hint="List core ids and ranges, as in cpuset.cpus.", exit_code=EXIT_USAGE)
    if args.cpus and (cls != "custom" or not CPU_LIST_RE.match(args.cpus)):
        raise SmartuneError(f"--cpus '{args.cpus}' is only valid with custom, as a list like 0-3,8",
                            hint="Use `affinity <app_id> custom --cpus 0-3,8`.", exit_code=EXIT_USAGE)
    current = row.get("cpu_affinity") or None
    extra = [cls] + (["--cpus", args.cpus] if args.cpus else [])
    plan = Plan("affinity", _app_argv("affinity", args.app_id, status, *extra), _app_view(row))
    name = row.get("app_name") or args.app_id
    plan.current = {"cpu_affinity": current or "not pinned"}
    _check_expect(plan, args, status)
    if cls == "restore":
        plan.summary = f"Unpin {name} from its cores"
        plan.target = {"cpu_affinity": "not pinned"}
        plan.effects = ["The app may run on every core again (its original cpuset.cpus is written back)."]
        if not current:
            plan.no_change = "The app is not pinned to any cores."
    else:
        where = AFFINITY_LABELS.get(cls) or f"cores {args.cpus}"
        plan.summary = f"Pin {name} to {where}"
        plan.target = {"cpu_affinity": cls if cls != "custom" else args.cpus}
        plan.effects = [
            f"Every process of the app moves to {where} at once (cpuset.cpus).",
            "Fewer cores than the app has busy threads makes it slower; with a CPU limit, the smaller "
            "of the two caps applies.",
            "Independent of limits: `control restore` does not undo it. It is released when the app "
            "exits or the SmarTune service stops.",
            "A core type this machine does not have is refused by SmarTune.",
        ]
        plan.undo = ["control", "affinity", args.app_id, "restore"]
    plan.path = "/app/cpu_affinity"
    plan.body = {"app_id": args.app_id, "class": cls}
    if args.cpus:
        plan.body["cpus"] = args.cpus
    plan.read_back = _row_view(args.app_id)
    return plan


# -- auto-limit exclusions and the launch queue -----------------------------------------

def plan_exclusion_remove(client, args):
    rows = client.auto_limit_exclusions()
    match = next((r for r in rows if args.key in (r.get("key"), r.get("app_id"))), None)
    plan = Plan("exclusion-remove", ["control", "exclusion-remove", args.key],
                pick(match or {}, "key", "app_id", "app_name"))
    plan.summary = f"Let SmarTune limit {(match or {}).get('app_name') or args.key} automatically again"
    plan.current = {"excluded": match is not None, **({"exclusion": match} if match else {})}
    plan.target = {"excluded": False}
    plan.effects = ["The app is a candidate for automatic limiting again: SmarTune may limit it the "
                    "next time system pressure gets critical."]
    if match is None:
        plan.no_change = "The app is not excluded from automatic limiting (see `smartune control exclusions`)."
    plan.path = "/app/auto_limit_exclusion_remove"
    plan.body = {"key": (match or {}).get("key") or args.key}
    plan.undo_note = "Restoring an automatically limited app (`control restore`) excludes it again."
    plan.read_back = lambda c: {"excluded": any(args.key in (r.get("key"), r.get("app_id"))
                                                for r in c.auto_limit_exclusions())}
    return plan


def plan_cancel_launch(client, args):
    pending = client.pending_apps()
    match = next((p for p in pending if p.get("app_id") == args.app_id), None)
    plan = Plan("cancel-launch", ["control", "cancel-launch", args.app_id],
                pick(match or {"app_id": args.app_id}, "app_id", "app_name", "priority"))
    plan.summary = f"Cancel the queued launch of {(match or {}).get('app_name') or args.app_id}"
    plan.current = {"queued": match is not None, "queue_length": len(pending)}
    plan.target = {"queued": False}
    plan.effects = ["The app will not be started when resources free up, and its status becomes "
                    "stopped. It has to be started again by hand if it is still wanted."]
    if match is None:
        plan.no_change = "The app has no queued launch (see `smartune pending`)."
    plan.path = "/app/cancel_relaunch"
    plan.body = {"app_id": args.app_id}
    plan.undo_note = "A cancelled launch cannot be re-queued from here; start the app again instead."
    plan.read_back = lambda c: {"queued": any(p.get("app_id") == args.app_id for p in c.pending_apps())}
    return plan


# -- registering an app ------------------------------------------------------------------

def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _identity_problems(names):
    """Blockers for match-rule names that are too generic or protected."""
    problems = []
    for name in names:
        low = os.path.basename(name or "").lower()
        if low in GENERIC_PROCESS_NAMES:
            problems.append(
                f"'{name}' would match every {low} process on the machine, not one application; "
                "register it from the SmarTune dashboard, where its identity can be narrowed by hand.")
        elif low in PROTECTED_PROCESS_NAMES or any(p in low for p in PROTECTED_NAME_PARTS):
            problems.append(f"'{name}' is SmarTune itself, an agent, or a system process; "
                            "it cannot be registered from here.")
    return problems


def _discover(client, args):
    """Running processes for the app and the identity the wizard would derive from them."""
    keywords = args.keyword or [args.name]
    found = client.discover_search(keywords)
    candidates = found.get("candidates") or []
    by_pid = {c.get("pid"): c for c in candidates}
    if args.pid:
        missing = [p for p in args.pid if p not in by_pid]
        if missing:
            raise SmartuneError(
                f"PID(s) {', '.join(map(str, missing))} not among the processes matching "
                f"{', '.join(keywords)}",
                hint="Pick PIDs from `candidates` in the plan (run it without --pid), or widen --keyword.",
                exit_code=EXIT_USAGE)
        chosen = [by_pid[p] for p in args.pid]
    else:
        # Default: the processes that *are* the program the user named, not
        # every process whose command line mentions it.
        name = args.name.lower()
        chosen = [c for c in candidates
                  if name in ((c.get("comm") or "").lower(), (c.get("process_name") or "").lower(),
                              os.path.basename(c.get("exe") or "").lower())]
    extracted = client.discover_extract([c["pid"] for c in chosen], args.name) if chosen else {}
    return keywords, candidates, chosen, extracted


def _default_app_id(name, extracted):
    # The wizard proposes the processes' shared systemd unit. That is a stable
    # identity for a service, but for a program started from a terminal it is
    # the terminal's transient scope, so fall back to <name>.id there.
    suggestion = extracted.get("id_suggestion") or ""
    if suggestion.endswith(".service"):
        return suggestion
    return f"{_slug(name)}.id" if _slug(name) else ""


def plan_register(client, args):
    apps = client.controlled_apps()
    pinned = bool(args.process_name or args.bpf_name)
    candidates, chosen = [], []
    if pinned:
        # apply_with: the identity the plan showed, written out in full.
        bpf_name, process_names = sorted(args.bpf_name or []), sorted(args.process_name or [])
        commandline = args.commandline or ""
        app_id = args.id
    else:
        keywords, candidates, chosen, extracted = _discover(client, args)
        bpf_name = extracted.get("bpf_name") or []
        process_names = extracted.get("process_names") or []
        commandline = args.commandline or (extracted.get("commandline") or [""])[0]
        app_id = args.id or _default_app_id(args.name, extracted)

    argv = ["control", "register", args.name, "--id", app_id or "", "--priority", args.priority]
    for n in bpf_name:
        argv += ["--bpf-name", n]
    for n in process_names:
        argv += ["--process-name", n]
    if commandline:
        argv += ["--commandline", commandline]

    plan = Plan("register", argv, {"app_id": app_id, "app_name": args.name, "priority": args.priority})
    plan.summary = f"Put {args.name} under SmarTune control with {args.priority} priority"
    plan.current = {"controlled": False}
    if not pinned:
        plan.current["running_processes"] = [
            pick(c, "pid", "comm", "process_name", "exe", "cmdline") for c in chosen][:MAX_LISTED_PROCESSES]
        others = [c for c in candidates if c not in chosen]
        if others:
            plan.current["other_matches_not_included"] = [
                pick(c, "pid", "comm", "process_name", "cmdline") for c in others][:MAX_LISTED_PROCESSES]
    plan.target = {"controlled": True, "app_id": app_id, "app_name": args.name,
                   "priority": args.priority,
                   "match_rule": {"bpf_name": bpf_name, "process_names": process_names,
                                  "commandline": commandline}}
    plan.effects = [
        "SmarTune recognises the app by this match rule from now on: every process whose name "
        f"matches {', '.join(sorted(set(bpf_name + process_names))) or 'it'} belongs to the app, "
        "including ones started later.",
        f"Under critical system pressure SmarTune may limit it automatically ({args.priority} "
        "priority: lower priorities are limited first and harder).",
        "While pressure is critical or the disk is busy, new launches of the app are held in a "
        "queue and started when resources recover.",
        "Afterwards `smartune control limit`, `freeze`, `priority` and the other actions apply to it.",
        "The registration is saved in SmarTune's configuration and survives restarts.",
    ]
    if args.priority == "critical":
        plan.effects.append("Critical also lowers its OOM score so the kernel is less likely to kill it.")

    # -- blockers
    if not app_id or not APP_ID_RE.match(app_id):
        plan.blockers.append("No usable app id; pass --id with letters, digits and . _ - @ : only.")
    if not (bpf_name or process_names):
        plan.blockers.append(
            f"No running process of {args.name} was found to derive its identity from. Start the "
            "application first, or choose processes with --pid (see `other_matches_not_included`).")
    plan.blockers += _identity_problems(bpf_name + process_names + [args.name])
    for app in apps:
        if app.get("app_id") == app_id:
            plan.no_change = f"An app with id {app_id} is already under control (`smartune app {app_id}`)."
        elif (app.get("app_name") or "").lower() == args.name.lower():
            plan.no_change = (f"An app named {args.name} is already under control as "
                              f"{app.get('app_id')}.")
        else:
            shared = {n.lower() for n in app.get("process_names") or []} & {n.lower() for n in process_names}
            if shared:
                plan.blockers.append(f"{app.get('app_name')} ({app.get('app_id')}) already matches "
                                     f"{', '.join(sorted(shared))}.")

    plan.path = "/app/new_controlled_app"
    plan.body = {"name": args.name, "id": app_id, "priority": args.priority,
                 "remark": f"{REGISTER_MARK} registered by {client.agent_id}",
                 "commandline": commandline, "bpf_name": bpf_name, "process_names": process_names}
    plan.undo = ["control", "unregister", app_id or "<app_id>"]
    plan.read_back = _row_view(app_id)
    return plan


def plan_unregister(client, args):
    row = _require_controlled(client, args.app_id)
    status = _status(row)
    plan = Plan("unregister", _app_argv("unregister", args.app_id, status), _app_view(row))
    name = row.get("app_name") or args.app_id
    plan.summary = f"Remove {name} from SmarTune control"
    plan.current = {"controlled": True, **_limit_view(row), "remark": row.get("remark")}
    plan.target = {"controlled": False}
    plan.effects = [
        "Deletes the app's registration (configuration entry and database record) for good; "
        "registering it again starts from scratch.",
        "SmarTune no longer limits, queues or protects it.",
    ]
    if status == "MANUAL_LIMITED":
        plan.effects.append("Its manual limit is lifted first" + (", and it is thawed." if row.get("frozen") else "."))
    if row.get("cpu_affinity"):
        plan.effects.append("Its CPU affinity is released.")
    _check_expect(plan, args, status)
    if not str(row.get("remark") or "").startswith(REGISTER_MARK):
        plan.blockers.append(
            "This app was not registered by `smartune control register`; delete it from the SmarTune "
            "dashboard instead.")
    if status == "AUTO_LIMITED":
        plan.blockers.append(
            "SmarTune is limiting the app automatically; release it with "
            f"`smartune control restore {args.app_id}` or take it over with "
            f"`smartune control lock {args.app_id}` first.")
    plan.path = "/app/purge_controlled_app"
    plan.body = {"id": args.app_id}
    plan.undo_note = "Register it again with `smartune control register <name>`."
    plan.read_back = _row_view(args.app_id)
    return plan


# -- device frequency limits (a whole device, not one app) -------------------------------
#
# A unit is one cpufreq policy (cores sharing a policy move together) or one GPU gt. The
# server looks unit ids up only in the list it discovers, clamps a range to each unit's
# hardware range, records the range seen before its first change, and restores every
# unit when the service stops. Plans name every unit explicitly, so a group (`cpu`, `e`,
# `card0`) is expanded once, when the plan is made.

FREQ_KINDS = ("cpu", "gpu")
FREQ_CORE_CLASSES = ("p", "e", "lpe")
# A CPU maximum below this share of a unit's top clock slows everything on those cores
# noticeably, SmarTune and the agent included.
FREQ_LOW_CPU_SHARE = 0.5
FREQ_SELECTORS_HINT = ("Use unit ids from `smartune control freq-status`, or cpu, gpu, p, e, lpe, "
                       "or a GPU card such as card0.")
FREQ_PARTIAL_HINT = ("Some units may already have changed. Do not repeat the command: check "
                     "`smartune control freq-status` and report what it shows.")


def _freq_units(client):
    return [u for u in (client.freq_status() or {}).get("units") or []
            if isinstance(u, dict) and u.get("id")]


def _mhz(lo, hi):
    return f"{_num(lo)}-{_num(hi)} MHz"


def _cpu_list(cpus):
    """[0, 1, 2, 5] -> '0-2,5'."""
    out, run_start, prev = [], None, None
    for cpu in sorted(cpus or []) + [None]:
        if prev is not None and cpu == prev + 1:
            prev = cpu
            continue
        if run_start is not None:
            out.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
        run_start = prev = cpu
    return ",".join(out)


def _freq_unit_view(unit):
    view = pick(unit, "id", "kind", "label", "group", "core_class", "driver",
                "hw_min_mhz", "hw_max_mhz", "min_mhz", "max_mhz", "cur_mhz", "limited")
    if unit.get("cpus"):
        view["cpus"] = _cpu_list(unit["cpus"])
    if unit.get("limited"):
        view["restores_to"] = _mhz(unit.get("original_min_mhz"), unit.get("original_max_mhz"))
    return view


def _unit_label(unit):
    if unit.get("kind") == "cpu":
        cls = {"p": "P cores", "e": "E cores", "lpe": "LPE cores"}.get(unit.get("core_class"))
        cpus = _cpu_list(unit.get("cpus"))
        parts = [p for p in (cls, f"CPU {cpus}" if cpus else None) if p]
        return f"{unit['id']} ({', '.join(parts)})" if parts else unit["id"]
    return f"{unit['id']} (GPU {unit.get('group')} {unit.get('label')})"


def _select_units(units, targets):
    """Unit ids for `targets`: exact ids, or cpu / gpu / p / e / lpe / a GPU card (card0)."""
    chosen, unknown = [], []
    for target in targets:
        if any(u["id"] == target for u in units):
            hits = [target]
        elif target in FREQ_KINDS:
            hits = [u["id"] for u in units if u.get("kind") == target]
        elif target in FREQ_CORE_CLASSES:
            hits = [u["id"] for u in units if u.get("kind") == "cpu" and u.get("core_class") == target]
        else:
            hits = [u["id"] for u in units if u.get("kind") == "gpu" and u.get("group") == target]
        if not hits:
            unknown.append(target)
        chosen += [h for h in hits if h not in chosen]
    if unknown:
        raise SmartuneError(f"No frequency unit on this machine matches {', '.join(unknown)}",
                            hint=FREQ_SELECTORS_HINT, exit_code=EXIT_USAGE)
    return chosen


def _freq_read_back(ids):
    def read(client):
        by_id = {u["id"]: u for u in _freq_units(client)}
        return {uid: (_mhz(by_id[uid]["min_mhz"], by_id[uid]["max_mhz"])
                      + (" (limited by SmarTune)" if by_id[uid].get("limited") else ""))
                if uid in by_id else "no longer present" for uid in ids}
    return read


def cmd_freq_status(client, args):
    units = [u for u in _freq_units(client) if not args.kind or u.get("kind") == args.kind]
    return {
        "count": len(units),
        "units": [_freq_unit_view(u) for u in units],
        "limited_by_smartune": [u["id"] for u in units if u.get("limited")],
        "note": "Ranges limit a whole device: every app on it is affected. cur_mhz is a live reading, "
                "not the limit. `control freq-limit` takes unit ids or cpu, gpu, p, e, lpe or a GPU "
                "card (group). There is no NPU unit: the NPU's frequency is managed by its firmware.",
    }


def plan_freq_limit(client, args):
    if args.min_mhz is None and args.max_mhz is None:
        raise SmartuneError("Give --min-mhz, --max-mhz or both",
                            hint="`smartune control freq-status` shows each unit's hardware range.",
                            exit_code=EXIT_USAGE)
    units = _freq_units(client)
    ids = _select_units(units, args.units)
    by_id = {u["id"]: u for u in units}
    selected = [by_id[i] for i in ids]
    lo, hi = args.min_mhz, args.max_mhz
    if lo is not None and hi is not None and lo > hi:
        raise SmartuneError(f"--min-mhz {_num(lo)} is above --max-mhz {_num(hi)}",
                            hint="The minimum must not exceed the maximum.", exit_code=EXIT_USAGE)

    # A side left out keeps each unit's current value: capping the maximum must not also move a
    # minimum the driver set above the hardware floor (Xe GPUs default to RPe, not RPn). The
    # server takes one min/max pair per request, so units are grouped by the pair they need.
    argv = ["control", "freq-limit", *ids]
    argv += ["--min-mhz", _num(lo)] if lo is not None else []
    argv += ["--max-mhz", _num(hi)] if hi is not None else []
    plan = Plan("freq-limit", argv, {"units": ids})
    requests = {}  # (min sent, max sent) -> unit ids
    clamped, slow, raised, already, crossed, unchanged = [], [], [], [], [], 0
    for u in selected:
        hw_lo, hw_hi = u["hw_min_mhz"], u["hw_max_mhz"]
        send = (lo if lo is not None else u["min_mhz"], hi if hi is not None else u["max_mhz"])
        t_lo, t_hi = (min(max(v, hw_lo), hw_hi) for v in send)
        plan.current[u["id"]] = _mhz(u["min_mhz"], u["max_mhz"])
        plan.target[u["id"]] = _mhz(t_lo, t_hi)
        requests.setdefault(send, []).append(u["id"])
        if (lo is not None and t_lo != lo) or (hi is not None and t_hi != hi):
            clamped.append(f"{u['id']} ({_mhz(hw_lo, hw_hi)})")
        if t_lo > t_hi:
            crossed.append(f"{u['id']} (now {plan.current[u['id']]})")
        if u.get("kind") == "cpu" and t_hi < hw_hi * FREQ_LOW_CPU_SHARE:
            slow.append(u["id"])
        if lo is not None and t_lo > hw_lo:
            raised.append(u["id"])
        if u.get("limited"):
            already.append(u["id"])
        if (u["min_mhz"], u["max_mhz"]) == (t_lo, t_hi):
            unchanged += 1

    if lo is None:
        what = f"Cap the maximum frequency at {_num(hi)} MHz"
    elif hi is None:
        what = f"Set the minimum frequency to {_num(lo)} MHz"
    else:
        what = f"Limit the frequency to {_mhz(lo, hi)}"
    plan.summary = f"{what} on {len(ids)} unit(s): " + ", ".join(_unit_label(u) for u in selected)
    plan.effects = [
        "This acts on the device, not on one app: every app running on these cores or this GPU is "
        "affected. It caps the range the hardware may choose; the actual clock can sit lower.",
    ]
    if lo is None or hi is None:
        plan.effects.append(f"Each unit keeps its current {'minimum' if lo is None else 'maximum'}.")
    if clamped:
        plan.effects.append("Values outside a unit's hardware range are applied as that unit's own "
                            "limit: " + ", ".join(clamped) + ".")
    if slow:
        plan.effects.append(f"The maximum is below half of the top clock of {', '.join(slow)}: "
                            "everything on those cores, SmarTune and this agent included, runs "
                            "noticeably slower.")
    if raised:
        plan.effects.append(f"A minimum above the hardware floor keeps {', '.join(raised)} clocked up "
                            "even when idle: more power and heat.")
    if already:
        plan.effects.append(f"Already limited by SmarTune: {', '.join(already)}. Restore still goes "
                            "back to the range they had before SmarTune first changed them.")
    plan.effects.append("Not saved: when the SmarTune service stops, every unit it limited is "
                        "restored, and nothing survives a reboot. The CPU governor still chooses a "
                        "clock within this range.")
    if crossed:
        side = "--min-mhz" if lo is None else "--max-mhz"
        plan.blockers.append("The kept value would leave the minimum above the maximum on "
                             f"{', '.join(crossed)}; give {side} as well.")
    elif unchanged == len(selected):
        plan.no_change = "Every selected unit already has this range."

    plan.path = "/device/freq/set"
    plan.bodies = [{"units": group, "min_mhz": pair[0], "max_mhz": pair[1]}
                   for pair, group in requests.items()]
    plan.body = plan.bodies[0]
    plan.undo = ["control", "freq-restore", *ids]
    plan.undo_note = "Restore puts back the range each unit had before SmarTune first changed it."
    plan.read_back = _freq_read_back(ids)
    plan.apply_hint = FREQ_PARTIAL_HINT
    return plan


def plan_freq_restore(client, args):
    units = _freq_units(client)
    by_id = {u["id"]: u for u in units}
    ids = _select_units(units, args.units) if args.units else [u["id"] for u in units if u.get("limited")]
    limited = [by_id[i] for i in ids if by_id[i].get("limited")]
    left = [i for i in ids if not by_id[i].get("limited")]

    plan = Plan("freq-restore", ["control", "freq-restore", *[u["id"] for u in limited]],
                {"units": [u["id"] for u in limited]})
    plan.summary = ("Restore the frequency range of " + ", ".join(_unit_label(u) for u in limited)
                    if limited else "No frequency limit to restore")
    for u in limited:
        plan.current[u["id"]] = _mhz(u["min_mhz"], u["max_mhz"])
        plan.target[u["id"]] = _mhz(u.get("original_min_mhz"), u.get("original_max_mhz"))
    plan.effects = [
        "Each unit gets back the range it had before SmarTune first changed it.",
        "A unit whose range was changed outside SmarTune meanwhile (for example by a power tool) "
        "keeps that range.",
    ]
    if left:
        plan.effects.append(f"Not limited by SmarTune, left alone: {', '.join(left)}.")
    if not limited:
        plan.no_change = ("None of the selected units is limited by SmarTune." if args.units
                          else "SmarTune is not limiting any device frequency.")
    plan.path = "/device/freq/restore"
    # Always explicit: an empty list would restore every unit, including any limited after this plan.
    plan.body = {"units": [u["id"] for u in limited]}
    plan.undo_note = "Limit again with `smartune control freq-limit <unit> --min-mhz N --max-mhz N`."
    plan.read_back = _freq_read_back([u["id"] for u in limited])
    plan.apply_hint = FREQ_PARTIAL_HINT
    return plan


# -- parser ------------------------------------------------------------------------------

def _percent(text):
    try:
        value = float(text.rstrip("%"))
    except ValueError:
        raise ValueError(f"expected a percentage, got '{text}'") from None
    return value


def add_parser(sub, add):
    """Register `smartune control ...` on the top-level subparsers; `add` is cli.build_parser's helper."""
    import argparse

    control = sub.add_parser(
        "control", help="Change how SmarTune controls an app or a device's frequency: plan first, apply with --yes",
        description="Balancer actions on a controlled app, and device frequency limits. Without --yes a command only prints its "
                    "plan and changes nothing; the plan's apply_with is the exact command to apply it.")
    csub = control.add_subparsers(dest="control_command", metavar="CONTROL_COMMAND")
    csub.required = True

    def percent(text):
        try:
            return _percent(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    def positive(text):
        try:
            value = float(text)
        except ValueError:
            value = 0
        if value < 1:
            raise argparse.ArgumentTypeError(f"expected a number >= 1, got '{text}'")
        return value

    def action(name, builder, help_text, app=True, expect=True):
        p = add(csub, name, run(builder), help_text)
        if app:
            p.add_argument("app_id")
        p.add_argument("--yes", action="store_true",
                       help="apply the change; only after the user agreed to the plan")
        if app and expect:
            p.add_argument("--expect", choices=CONTROL_STATES,
                           help="refuse unless the app is still in this state (apply_with sets it)")
        return p

    p = add(csub, "profile", cmd_profile, "Limit defaults and allowed ranges for one app (read-only)")
    p.add_argument("app_id")
    add(csub, "exclusions", cmd_exclusions, "Apps excluded from automatic limiting (read-only)")

    p = action("limit", plan_limit, "Limit an app's CPU, memory and/or disk IO by hand")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--cpu", type=percent, help="CPU cap, %% of the app's current usage")
    g.add_argument("--no-cpu", action="store_true", help="leave CPU unlimited")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--mem", type=percent, help="memory soft cap, %% of the app's current usage")
    g.add_argument("--no-mem", action="store_true", help="leave memory unlimited")
    for _, attr, unit in DISK_FIELDS:
        p.add_argument("--" + attr.replace("_", "-"), dest=attr, type=positive, help=f"disk cap in {unit}")
    p.add_argument("--no-disk", action="store_true", help="leave disk IO unlimited")
    p.add_argument("--cgroup", action="append",
                   help="limit only this running instance (repeatable; see `control profile`)")

    action("restore", plan_restore, "Lift an app's manual or automatic limit")
    action("lock", plan_lock, "Take over an automatic limit as a manual one, without releasing it")
    action("freeze", plan_freeze, "Pause every process of a manually limited app")
    action("thaw", plan_thaw, "Resume a frozen app")
    p = action("priority", plan_priority, "Set an app's priority")
    p.add_argument("priority", choices=PRIORITIES)
    p = action("net-priority", plan_network_priority, "Set an app's network traffic class")
    p.add_argument("priority", choices=NETWORK_PRIORITIES)
    p = action("affinity", plan_affinity, "Pin an app to P / E / LPE cores or a core list, or unpin it")
    p.add_argument("cpu_class", choices=AFFINITY_CLASSES)
    p.add_argument("--cpus", help="core list for custom, e.g. 0-3,8")
    p = action("exclusion-remove", plan_exclusion_remove,
               "Make an excluded app a candidate for automatic limiting again", app=False)
    p.add_argument("key", help="exclusion key or app_id, from `control exclusions`")
    action("cancel-launch", plan_cancel_launch, "Cancel an app's queued launch", expect=False)

    p = action("register", plan_register, "Put a running application under SmarTune control",
               app=False)
    p.add_argument("name", help="the application's name, e.g. benchmark_app")
    p.add_argument("--priority", choices=PRIORITIES, default="low",
                   help="priority to register it with (default low)")
    p.add_argument("--keyword", action="append",
                   help="search running processes for this text instead of the name (repeatable)")
    p.add_argument("--pid", action="append", type=int,
                   help="derive the identity from this process only (repeatable)")
    p.add_argument("--id", help="app id (default: derived from the name)")
    p.add_argument("--bpf-name", action="append", help=argparse.SUPPRESS)      # set by apply_with
    p.add_argument("--process-name", action="append", help=argparse.SUPPRESS)  # set by apply_with
    p.add_argument("--commandline", help=argparse.SUPPRESS)                    # set by apply_with
    action("unregister", plan_unregister,
           "Remove an app that `control register` added from SmarTune control")

    def mhz(text):
        try:
            value = float(text)
        except ValueError:
            value = 0
        if value <= 0:
            raise argparse.ArgumentTypeError(f"expected a frequency in MHz above 0, got '{text}'")
        return value

    p = add(csub, "freq-status", cmd_freq_status,
            "CPU and GPU frequency ranges per unit, and which ones SmarTune limits (read-only)")
    p.add_argument("--kind", choices=FREQ_KINDS, help="only CPU policies or only GPU gts")
    p = action("freq-limit", plan_freq_limit,
               "Limit the min/max frequency of CPU policies or GPU gts (the whole device)", app=False)
    p.add_argument("units", nargs="+", metavar="UNIT",
                   help="unit id from `control freq-status`, or cpu, gpu, p, e, lpe, or a GPU card such as card0")
    p.add_argument("--min-mhz", type=mhz, help="minimum in MHz (default: keep each unit's current minimum)")
    p.add_argument("--max-mhz", type=mhz, help="maximum in MHz (default: keep each unit's current maximum)")
    p = action("freq-restore", plan_freq_restore,
               "Put device frequency ranges back to what they were before SmarTune changed them",
               app=False)
    p.add_argument("units", nargs="*", metavar="UNIT",
                   help="units or groups to restore (default: every unit SmarTune limits)")
    return control
