# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Keep SmarTune from limiting, freezing or pinning its own cgroup.

A limit is a property of a cgroup, not of a process. When SmarTune is started
from a terminal it lives in that terminal's ``vte-spawn-*.scope``, and so does
everything started from the same terminal that did not get a scope of its own.
Limiting such an app writes ``cpu.max`` / ``memory.high`` / ``io.max`` on the
very cgroup the service runs in, so the service throttles itself (the dashboard
hangs), and a freeze stops it for good: nothing is left to thaw it.

The same holds for every *ancestor* of the service's cgroup (``app.slice``,
``user@N.service`` ...): cpu, memory, io and freeze all apply to descendants.

Unit names are unique in the cgroup tree, so "is this cgroup the service's own
or an ancestor of it" is a membership test on the service's cgroup path. That
lets callers that only hold a cgroup id (a unit basename, as every limit key
here is) use it without finding the directory first.

Only *applying* something is refused. Restoring must always go through: a limit
that was installed before this guard existed has to stay removable.
"""

import os
import threading
from typing import Iterable, List, Optional, Tuple

REFUSAL = (
    "{names} is the cgroup SmarTune itself runs in (or one above it), so a limit, "
    "freeze or core pin on it would hit SmarTune too and is not allowed. "
    "Start the app in its own cgroup instead, for example with "
    "'systemd-run --user --scope <command>'."
)

_lock = threading.Lock()
_own_cache: Optional[Tuple[str, ...]] = None


def _read_cgroup_path(pid: int) -> Optional[str]:
    """The cgroup v2 path of *pid* (``/user.slice/...``), or None when unreadable."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    return parts[2].replace(" (deleted)", "") or None
    except OSError:
        pass
    return None


def own_units(refresh: bool = False) -> Tuple[str, ...]:
    """Unit names from the root down to the service's own cgroup.

    Read once and kept: a process only moves cgroup when something moves it, and
    the service never does. ``refresh`` re-reads (tests; an operator who moved it).
    """
    global _own_cache
    with _lock:
        if _own_cache is None or refresh:
            path = _read_cgroup_path(os.getpid()) or ""
            _own_cache = tuple(part for part in path.split("/") if part)
        return _own_cache


def set_own_units_for_test(units: Optional[Iterable[str]]) -> None:
    """Pin (or with None, clear) the cached cgroup path. Tests only."""
    global _own_cache
    with _lock:
        _own_cache = None if units is None else tuple(units)


def is_protected(cgroup: str) -> bool:
    """True when *cgroup* (an id, or a path whose last part is the id) is SmarTune's own or an ancestor of it."""
    name = os.path.basename(str(cgroup or "").rstrip("/"))
    return bool(name) and name in own_units()


def protected_among(cgroups: Iterable[str]) -> List[str]:
    """The ids in *cgroups* that are protected, as basenames, without repeats."""
    seen: List[str] = []
    for cgroup in cgroups:
        name = os.path.basename(str(cgroup or "").rstrip("/"))
        if name and name in own_units() and name not in seen:
            seen.append(name)
    return seen


def refusal(cgroups: Iterable[str]) -> str:
    """The sentence shown to the user for the protected ids in *cgroups*."""
    names = ", ".join(protected_among(cgroups)) or "This cgroup"
    return REFUSAL.format(names=names)
