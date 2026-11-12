# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Give every benchmark run a cgroup of its own.
#
# A child inherits its parent's cgroup, so a benchmark the service starts lands
# in the service's own: the terminal's ``vte-spawn-*.scope`` when it was started
# from one. Limits, freeze and core pinning are properties of a cgroup, so
# managing the benchmark there manages SmarTune with it -- the service throttles
# itself and the dashboard hangs.
#
# The run is therefore started under ``systemd-run --scope``, which registers a
# transient scope, moves the process into it and then execs the command in
# place: same pid, process group, environment and stdio, so the job code that
# waits on, signals and logs the process is unchanged.
#
# Where the scope goes follows from where the service itself runs, so it lands
# next to it and in a place SmarTune's limit code already knows how to reach:
#
#   service under user@N.service/...   -> a scope in the same slice, created by
#                                         the user's own manager
#                                         (``systemd-run --user``; SmarTune
#                                         limits it with ``systemctl --user``)
#   service under user-N.slice         -> a scope in user-N.slice, root manager
#   service under system.slice         -> a scope in system.slice, root manager
#   anything else                      -> nothing changes (and it is logged)
#
# No limit is set on the scope: it is accounting only, so the benchmark runs
# exactly as before and the user decides, after leaving quiet mode, whether and
# how to limit it.
#
# Nothing here may stop a benchmark from running. When a scope cannot be made
# (no systemd, no user bus, a layout not listed above) the command is run as
# it was, and the reason is logged once. The scope is probed with a no-op
# command first, so a failure shows up there and not as a benchmark that never
# started.

import os
import shutil
import subprocess  # nosec - argv lists only, shell=False
import threading
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

from benchmark.service import privilege

_PROBE_TIMEOUT_SEC = 15


class Placement(NamedTuple):
    """Which systemd manager creates the scope, and the slice it goes in."""

    manager: str            # "user" | "system"
    slice: Optional[str]    # None: the manager's default (app.slice for "user")
    uid: Optional[int]      # the user whose manager it is, for "user"


class Scope(NamedTuple):
    """A scope that was created, with what is needed to signal it later."""

    unit: str
    placement: Placement
    env: Dict[str, str]     # extra environment systemctl needs (user bus)

    def describe(self) -> str:
        where = self.placement.slice or "the default slice"
        return f"{self.unit}.scope in {where} ({self.placement.manager} manager)"


def placement_for(own_path: str, spawn_uid: Optional[int], euid: int) -> Tuple[Optional[Placement], str]:
    """Where a scope should go for a service running at *own_path*.

    Pure: takes the service's cgroup path (``/user.slice/.../vte-spawn-x.scope``),
    the uid the benchmark will run as, and the service's euid. Returns
    ``(placement, reason)``; the placement is None when there is no usable place
    and the reason says why.
    """
    parts = [p for p in (own_path or "").split("/") if p]
    if len(parts) < 2:
        return None, f"the service's cgroup {own_path or '(unknown)'!r} has no parent to sit next to"
    parent = parts[-2]

    for index, part in enumerate(parts[:-1]):
        if part.startswith("user@") and part.endswith(".service"):
            try:
                owner = int(part[len("user@"):-len(".service")])
            except ValueError:
                return None, f"cannot read the uid out of {part}"
            if spawn_uid != owner:
                return None, (f"the service runs under {part} but the benchmark runs as uid "
                              f"{spawn_uid}, so it would need another user's manager")
            slice_name = parent if parent.endswith(".slice") else None
            return Placement("user", slice_name, owner), "ok"

    if euid != 0:
        return None, "the service is not root, so it cannot create a scope in the system manager"
    if parent == "system.slice" or (parent.startswith("user-") and parent.endswith(".slice")):
        return Placement("system", parent, None), "ok"
    return None, f"no scope placement is defined next to {parent!r}"


def _user_env(uid: int) -> Optional[Dict[str, str]]:
    """Environment that lets ``systemctl --user`` reach *uid*'s manager, or None without a bus."""
    runtime_dir = f"/run/user/{uid}"
    bus = os.path.join(runtime_dir, "bus")
    if not os.path.exists(bus):
        return None
    return {"XDG_RUNTIME_DIR": runtime_dir, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}"}


def _base_cmd(placement: Placement, unit: Optional[str]) -> List[str]:
    cmd = ["systemd-run"]
    if placement.manager == "user":
        cmd.append("--user")
    cmd += ["--scope", "--quiet", "--collect"]
    if unit:
        cmd.append(f"--unit={unit}")
    if placement.slice:
        cmd.append(f"--slice={placement.slice}")
    return cmd


def _drop_prefix(placement: Placement) -> List[str]:
    """Privilege drop for a root-manager scope (the user-manager one drops via Popen).

    Creating a scope in the system manager needs root, so the drop cannot be done
    by Popen before exec: it happens inside the scope instead. The groups are passed
    explicitly -- a child without ``render`` cannot open the GPU.
    """
    who = privilege.target()
    if placement.manager != "system" or who is None:
        return []
    groups = f"--groups={','.join(map(str, who.groups))}" if who.groups else "--clear-groups"
    return ["setpriv", f"--reuid={who.uid}", f"--regid={who.gid}", groups, "--"]


def _spawn_kwargs(placement: Placement) -> dict:
    if placement.manager == "system":
        # Popen must stay root to create the scope; only the umask is carried over.
        return {"umask": privilege._CHILD_UMASK}
    return privilege.spawn_kwargs()


_probe_lock = threading.Lock()
_probe_cache: Dict[Placement, Tuple[bool, str]] = {}


def _probe(placement: Placement, env: Dict[str, str]) -> Tuple[bool, str]:
    """Whether a scope can really be created there, found out once with a no-op command."""
    with _probe_lock:
        cached = _probe_cache.get(placement)
        if cached is not None:
            return cached
        full_env = {**os.environ, **env}
        try:
            result = subprocess.run(
                _base_cmd(placement, None) + ["--"] + _drop_prefix(placement) + ["true"],
                env=full_env, capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_SEC, **_spawn_kwargs(placement))
            outcome = (result.returncode == 0,
                       (result.stderr or result.stdout or "").strip()[:300] or f"exit {result.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            outcome = (False, str(exc))
        _probe_cache[placement] = outcome
        return outcome


def _unavailable() -> Optional[str]:
    if not os.path.isdir("/run/systemd/system"):
        return "systemd is not the init system"
    if shutil.which("systemd-run") is None:
        return "systemd-run is not installed"
    return None


def wrap(argv: Sequence[str], env: Dict[str, str], unit: str) -> Tuple[List[str], Dict[str, str], dict, Optional[Scope]]:
    """Return ``(argv, env, popen_kwargs, scope)`` to start *argv* in a scope of its own.

    On any reason not to, the arguments come back as they went in (with the plain
    privilege drop) and ``scope`` is None, so the caller needs no second path.
    """
    plain = (list(argv), dict(env), privilege.spawn_kwargs(), None)

    reason = _unavailable()
    if reason:
        logger.warning(f"Benchmark runs without a cgroup of its own: {reason}.")
        return plain

    from utils import self_guard
    own_path = "/" + "/".join(self_guard.own_units())
    who = privilege.target()
    spawn_uid = who.uid if who is not None else os.geteuid()
    placement, why = placement_for(own_path, spawn_uid, os.geteuid())
    if placement is None:
        logger.warning(f"Benchmark runs without a cgroup of its own: {why}. "
                       "Limiting it would also limit SmarTune.")
        return plain

    extra_env: Dict[str, str] = {}
    if placement.manager == "user":
        extra_env = _user_env(placement.uid) or {}
        if not extra_env:
            logger.warning("Benchmark runs without a cgroup of its own: the user's systemd "
                           f"session bus (/run/user/{placement.uid}/bus) is not there. "
                           "Limiting it would also limit SmarTune.")
            return plain

    ok, detail = _probe(placement, extra_env)
    if not ok:
        logger.warning(f"Benchmark runs without a cgroup of its own: a test scope could not be "
                       f"created ({detail}). Limiting it would also limit SmarTune.")
        return plain

    wrapped = (_base_cmd(placement, unit) + ["--"] + _drop_prefix(placement) + list(argv))
    scope = Scope(unit=unit, placement=placement, env=extra_env)
    logger.info(f"Benchmark will run in {scope.describe()}.")
    return wrapped, {**env, **extra_env}, _spawn_kwargs(placement), scope


def kill(scope: Optional[Scope], signal_name: str = "SIGKILL") -> bool:
    """Signal every process left in the scope, including ones that left the process group.

    Best effort: the caller has already signalled the process group.
    """
    if scope is None:
        return False
    cmd = ["systemctl"]
    if scope.placement.manager == "user":
        cmd.append("--user")
    cmd += ["kill", f"--signal={signal_name}", f"{scope.unit}.scope"]
    try:
        result = subprocess.run(
            cmd, env={**os.environ, **scope.env}, capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_SEC, **_spawn_kwargs(scope.placement))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"Could not signal {scope.unit}.scope: {exc}")
        return False
    # "not loaded" just means the scope is already gone with its last process.
    return result.returncode == 0
