# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""cgroup v2 freezer helpers for the manual "Freeze" action.

Freezing a limited app is a last resort for apps that run on a GPU/NPU and
barely touch CPU/memory/disk, so the ordinary limit cannot slow them down.
Freeze is per cgroup (``cgroup.freeze``), not per PID, so multi-process apps are
stopped atomically and the app sees no signal.

Every freeze is recorded in a small state file.  If smartune dies while an app
is frozen, the next start thaws exactly the recorded cgroups -- never every
frozen cgroup on the box, which would also thaw ``docker pause`` and friends.
"""

import json
import os
import threading
import time
from typing import Callable, Iterable, List, Optional, Tuple

from utils.app_utils import write_cgroup_file
from utils.logger import get_logger

logger = get_logger(__name__)

# How long to wait for ``frozen 1``/``frozen 0`` in cgroup.events.  Freezing the
# cgroup itself takes milliseconds; a task stuck in an uninterruptible driver
# call is what makes it slower.
FREEZE_TIMEOUT_S = 10.0
_POLL_S = 0.05

STATE_FILE = os.environ.get("SMARTUNE_FREEZE_STATE", "/run/smartune/frozen_cgroups.json")
_state_lock = threading.Lock()


def is_frozen(cgroup_dir: str) -> Optional[bool]:
    """Return the cgroup's frozen state from cgroup.events, or None if unreadable."""
    try:
        with open(os.path.join(cgroup_dir, "cgroup.events"), "r", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(" ")
                if key == "frozen":
                    return value.strip() == "1"
    except OSError:
        return None
    return None


def set_frozen(cgroup_dir: str, frozen: bool, timeout: Optional[float] = None,
               allowed_roots=None) -> Tuple[bool, str]:
    """Freeze or thaw *cgroup_dir* and wait until cgroup.events confirms it."""
    if timeout is None:
        timeout = FREEZE_TIMEOUT_S
    want = "1" if frozen else "0"
    try:
        write_cgroup_file(want, os.path.join(cgroup_dir, "cgroup.freeze"),
                          allowed_roots=allowed_roots)
    except OSError as e:
        return False, f"cannot write cgroup.freeze: {e}"

    deadline = time.monotonic() + timeout
    while True:
        state = is_frozen(cgroup_dir)
        if state is frozen:
            return True, "ok"
        if time.monotonic() >= deadline:
            return False, f"cgroup did not reach frozen={want} within {timeout:.0f}s"
        time.sleep(_POLL_S)


# -- crash-safe record of what *we* froze --------------------------------------

def _read_state() -> List[str]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(c) for c in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_state(cgroups: Iterable[str]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(set(cgroups)), f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        logger.warning(f"Could not persist freeze state to {STATE_FILE}: {e}")


def record(cgroup: str, frozen: bool) -> None:
    """Remember (or forget) that *cgroup* was frozen by smartune."""
    with _state_lock:
        current = set(_read_state())
        if frozen:
            current.add(cgroup)
        else:
            current.discard(cgroup)
        _write_state(current)


def thaw_recorded(resolve: Callable[[str], str]) -> List[str]:
    """Thaw every recorded cgroup (startup safety net).  Returns the thawed ids.

    *resolve* maps a cgroup id to its directory.  Records whose cgroup is gone
    are dropped; a thaw that fails keeps its record so the next start retries.
    """
    thawed, keep = [], []
    with _state_lock:
        recorded = _read_state()
    for cgroup in recorded:
        path = resolve(cgroup)
        if not os.path.isdir(path):
            continue
        ok, msg = set_frozen(path, False)
        if ok:
            thawed.append(cgroup)
            logger.warning(f"Thawed {cgroup}, left frozen by a previous smartune run")
        else:
            keep.append(cgroup)
            logger.error(f"Failed to thaw leftover frozen cgroup {cgroup}: {msg}")
    with _state_lock:
        _write_state(keep)
    return thawed
