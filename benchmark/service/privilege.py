# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Run the benchmark pipeline's subprocesses as an unprivileged user.
#
# SmarTune's service runs as root (smartune.service is User=root) because the
# monitor needs perf, sysfs, cgroups and BPF. The benchmark pipeline inherited
# that: cloning openvino.genai, building venvs with uv, fetching weights with
# `hf download`, and then executing the vendored scripts -- plus whatever code a
# HuggingFace repo ships -- all ran with full privileges, on input that comes
# off the internet.
#
# The parent process has to stay root, so the drop happens per child: every
# spawn point passes ``**spawn_kwargs()`` and CPython does setgid/setgroups/
# setuid between fork and exec. There is deliberately no setuid/seteuid in this
# process -- one of those would take the monitor's descriptors down with it.
#
# The target user is the owner of the benchmark source tree. That is the account
# the deployment already belongs to, it needs no configuration, and it is the
# one whose supplementary groups (notably `render`) grant the GPU access a
# benchmark cannot run without. When there is nobody to drop to -- not root to
# begin with, a root-owned tree (the .deb installs to /opt), or a uid with no
# passwd entry -- nothing changes and the reason is logged once. A benchmark
# that refuses to start is worse than one that runs as it did yesterday.
#
# Dropping the child also means the files it creates belong to that user, so
# anything the root parent creates *ahead* of it has to be handed over too:
# see ensure_dir() and chown().

import os
import pwd
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from utils.logger import logger

# <repo>/benchmark/service/privilege.py -> <repo>/benchmark. Same derivation as
# env.SRC_ROOT, repeated rather than imported: env.py imports jobs.py, which
# needs this module, so a dependency the other way would close the cycle.
SRC_ROOT = Path(__file__).resolve().parent.parent

# Mode for the child's own files. 022 keeps the results readable by the root
# parent (results.py reads every summary and CSV the pipeline writes) whatever
# UMask= the service unit happens to carry.
_CHILD_UMASK = 0o022


class Target(NamedTuple):
    """The account benchmark subprocesses are dropped to."""

    uid: int
    gid: int
    name: str
    groups: List[int]


# Why target() came back None, in a sentence, for describe() to repeat into the
# job log. Set by target() on its one uncached call.
_no_drop_reason = "not resolved yet"


def _decline(reason: str, level: str = "warning") -> None:
    global _no_drop_reason
    _no_drop_reason = reason
    getattr(logger, level)(f"Benchmark subprocesses are not dropped: {reason}")


@lru_cache(maxsize=1)
def target() -> Optional[Target]:
    """The account to drop to, or None to keep running as whoever we are.

    Cached: none of the inputs can change inside one service lifetime, and the
    "why not" is logged from here so it is said exactly once.
    """
    if os.geteuid() != 0:
        # Already unprivileged -- a source checkout started by hand. Dropping
        # would be a no-op at best and a failed setuid at worst.
        _decline(f"this process is not root (euid={os.geteuid()}), so there is "
                 "nothing to drop", level="debug")
        return None

    try:
        owner_uid = SRC_ROOT.stat().st_uid
    except OSError as exc:
        _decline(f"cannot stat {SRC_ROOT}: {exc}")
        return None

    if owner_uid == 0:
        # The .deb installs root-owned under /opt/intel/smartune, so this is a
        # normal outcome there, not a misconfiguration.
        _decline(f"{SRC_ROOT} is owned by root, so there is no unprivileged "
                 "account to drop to")
        return None

    try:
        entry = pwd.getpwuid(owner_uid)
    except KeyError:
        # A tree copied off another machine. We could still setuid to the bare
        # uid, but without a passwd entry there is no group list either -- and a
        # child missing `render` cannot open the GPU, which fails the run in a
        # far more confusing way than not dropping at all.
        _decline(f"{SRC_ROOT} is owned by uid {owner_uid}, which has no passwd "
                 "entry, so its groups -- including render, for GPU access -- "
                 "cannot be resolved")
        return None

    try:
        groups = sorted(os.getgrouplist(entry.pw_name, entry.pw_gid))
    except OSError as exc:
        _decline(f"cannot read the groups of {entry.pw_name}: {exc}")
        return None

    logger.info(f"Benchmark subprocesses will run as {entry.pw_name} "
                f"(uid={entry.pw_uid}, gid={entry.pw_gid}, groups={groups}).")
    return Target(uid=entry.pw_uid, gid=entry.pw_gid, name=entry.pw_name,
                  groups=groups)


def _current_user() -> str:
    """Name of the account this process is running as, or its bare euid."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return f"uid {os.geteuid()}"


def spawn_kwargs() -> Dict[str, object]:
    """Keyword arguments that drop a subprocess. Empty when there is no target.

    Spread into ``subprocess.Popen``/``subprocess.run``. Empty rather than
    ``user=0`` on purpose: passing the kwargs unconditionally would make every
    non-root caller (a developer running the service by hand) attempt a setuid
    it is not allowed to do.
    """
    who = target()
    if who is None:
        return {}
    return {
        # Applied by CPython in the child, between fork and exec, in the only
        # order that works: setgid, then setgroups, then setuid.
        "user": who.uid,
        "group": who.gid,
        "extra_groups": list(who.groups),
        "umask": _CHILD_UMASK,
    }


def chown(path: Path) -> None:
    """Hand ``path`` to the target user. No-op when there is no target.

    Never raises: the caller is always in the middle of something more important
    than the ownership of one file, and a run that works with a root-owned
    artifact left behind beats a run that did not start.
    """
    who = target()
    if who is None:
        return
    try:
        os.chown(path, who.uid, who.gid)
    except OSError as exc:
        logger.warning(f"Could not give {path} to {who.name}: {exc}")


def ensure_dir(path: Path) -> Path:
    """``mkdir -p`` the directory and give the target user what this created.

    Only the levels that did not exist are handed over. An existing directory is
    left alone: a deployment that deliberately made part of the runtime tree
    root-owned (or pointed env_root at a shared volume) should not have that
    undone by a job starting up.
    """
    missing: List[Path] = []
    if target() is not None:
        probe = path
        while not probe.exists():
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent

    path.mkdir(parents=True, exist_ok=True)
    # Top down, so each level exists by the time it is chowned.
    for created in reversed(missing):
        chown(created)
    return path


def describe() -> List[str]:
    """Lines describing where the pipeline will run and as whom.

    Emitted at startup and prepended to every job log, because every failure
    mode this module can produce is a permission error deep inside a vendored
    script -- and the fastest way to read one of those is to already know which
    paths were involved and which account was holding the pen.

    Imports env lazily: env.py imports jobs.py, which imports this module.
    """
    from benchmark.service import env

    who = target()
    lines = [
        f"service as    : {_current_user()} (euid={os.geteuid()})",
        f"src_root      : {SRC_ROOT}",
    ]
    try:
        stat = SRC_ROOT.stat()
        lines.append(f"src_root owner: uid={stat.st_uid} gid={stat.st_gid}")
    except OSError as exc:
        lines.append(f"src_root owner: unreadable ({exc})")

    if who is None:
        lines.append(f"run as        : {_current_user()} -- not dropped, because "
                     f"{_no_drop_reason}")
    else:
        lines.append(f"run as        : {who.name} uid={who.uid} gid={who.gid} "
                     f"groups={who.groups}")

    try:
        paths = env.paths()
    except Exception as exc:                              # pragma: no cover
        lines.append(f"paths         : unresolvable ({exc})")
        return lines

    if who is None:
        lines.append(f"child HOME    : {os.environ.get('HOME', '-')} (inherited)")
    else:
        lines.append(f"child HOME    : {child_home(paths['env_root'])}")
    for name in sorted(paths):
        value = paths[name]
        lines.append(f"path {name:<10}: {value} "
                     f"[{'exists' if value.exists() else 'missing'}]")
    return lines


def child_home(env_root: Path) -> Path:
    """HOME for benchmark subprocesses: inside the runtime tree, not /root.

    The children inherit the service's environment, so without this HOME is
    /root and every tool that keeps state there fails once the child is no
    longer root -- uv installing a managed Python, pip's cache, git reading its
    config. The runtime tree is the right place for it for the same reason
    HF_HOME and UV_CACHE_DIR already live there (configs/global_vars.sh,
    uv_build_envs.sh): everything the benchmark generates stays under one
    directory that can be deleted to reclaim it.
    """
    return env_root / ".gen" / "home"


def log_startup() -> None:
    """INFO the description once, at service start."""
    for line in describe():
        logger.info(f"Benchmark environment: {line}")
