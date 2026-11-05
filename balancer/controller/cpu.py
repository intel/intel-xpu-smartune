# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CPU controller: per-cgroup core affinity through cgroup v2 ``cpuset.cpus``.

Writing ``cpuset.cpus`` migrates the cgroup's running threads at once, so the
binding can change while the app runs.  Each cgroup's original value is
recorded before the first write and put back by :meth:`CPUController.restore`
-- a set without a restore would leave the app pinned for good.

``cpuset`` is absent from ``cgroup.subtree_control`` below the root on most
systemd hosts, so the controller is enabled lazily on every ancestor of the
target (the same idea as ``IOController.enable_io_controller``).
"""

import errno
import os
import re
import threading
from typing import Dict, Iterable, List, Optional, Set, Tuple

from utils.logger import get_logger
logger = get_logger(__name__)
from utils.app_utils import write_cgroup_file

CORE_CLASSES = ("p", "e", "lpe")


def parse_cpu_list(spec: Optional[str]) -> Set[int]:
    """``"0-1,4"`` -> ``{0, 1, 4}``.  Raises ValueError on malformed input."""
    result: Set[int] = set()
    for token in re.split(r"[,\s]+", (spec or "").strip()):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ValueError(f"invalid cpu list: {spec!r}")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if end < start:
            raise ValueError(f"invalid cpu range: {token!r}")
        result.update(range(start, end + 1))
    return result


def format_cpu_list(cpus: Iterable[int]) -> str:
    """``{0, 1, 4}`` -> ``"0-1,4"``."""
    ordered = sorted(set(cpus))
    parts: List[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def core_class_options(groups: dict) -> Dict[str, List[int]]:
    """The non-empty core classes of a ``detect_core_groups()`` result."""
    mapping = {"p": "p_cores", "e": "e_cores", "lpe": "lpe_cores"}
    return {name: sorted(groups.get(key) or []) for name, key in mapping.items() if groups.get(key)}


def resolve_cpus(cpu_class: str, cpus: Optional[str], groups: dict,
                 online: Set[int]) -> Tuple[Optional[str], str]:
    """Turn an API request into a ``cpuset.cpus`` string.

    Returns ``(value, message)``; ``value`` is None when the request is refused.
    """
    cpu_class = (cpu_class or "").lower()
    if cpu_class == "custom":
        try:
            wanted = parse_cpu_list(cpus)
        except ValueError as exc:
            return None, str(exc)
        if not wanted:
            return None, "custom requires a non-empty cpus list"
        offline = wanted - online
        if offline:
            return None, f"cpus not online: {format_cpu_list(offline)}"
        return format_cpu_list(wanted), "ok"
    if cpu_class in CORE_CLASSES:
        options = core_class_options(groups)
        if cpu_class not in options:
            return None, (f"this machine has no {cpu_class.upper()} cores "
                          f"(available: {', '.join(options) or 'none'})")
        return format_cpu_list(options[cpu_class]), "ok"
    return None, f"unknown cpu class {cpu_class!r}"


class CPUController:
    """CPU core affinity (cgroup v2 cpuset); one instance lives on ControlManager.cpu."""

    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
        self._originals: Dict[str, str] = {}   # cgroup dir -> original cpuset.cpus
        # app_id -> last successful selection.  Server lifetime only: stopping the service
        # restores every binding, so nothing needs to outlive it.
        self._last_choices: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _read(self, directory: str, name: str) -> Optional[str]:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    def _ancestors(self, directory: str) -> List[str]:
        """Directories from the mount down to (excluding) *directory*."""
        mount = os.path.realpath(self.cgroup_mount)
        rel = os.path.relpath(os.path.realpath(directory), mount)
        parts = [] if rel == "." else rel.split(os.sep)
        chain = [mount]
        for part in parts[:-1]:
            chain.append(os.path.join(chain[-1], part))
        return chain

    def enable_for(self, directory: str) -> bool:
        """Make sure every ancestor passes ``cpuset`` down to *directory*."""
        ok = True
        for ancestor in self._ancestors(directory):
            control = os.path.join(ancestor, "cgroup.subtree_control")
            current = self._read(ancestor, "cgroup.subtree_control")
            if current is None:
                continue
            if "cpuset" in current.split():
                continue
            try:
                write_cgroup_file("+cpuset", control)
                logger.info("Enabled cpuset controller in %s", ancestor)
            except OSError as exc:
                logger.error("Cannot enable cpuset in %s: %s", ancestor, exc)
                ok = False
        return ok

    def read_cpus(self, directory: str) -> Optional[str]:
        """The cpus the cgroup is really confined to, from cgroupfs."""
        return self._read(directory, "cpuset.cpus.effective")

    def is_bound(self, directory: str) -> bool:
        with self._lock:
            return os.path.realpath(directory) in self._originals

    def bound_cgroups(self) -> List[str]:
        with self._lock:
            return list(self._originals)

    # -- actions ---------------------------------------------------------
    def set_cpus(self, directory: str, cpus: str) -> Tuple[bool, str]:
        directory = os.path.realpath(directory)
        if not os.path.isdir(directory):
            return False, f"cgroup {os.path.basename(directory)} no longer exists"
        if not self.enable_for(directory):
            return False, "cannot enable the cpuset controller for this cgroup"
        original = self._read(directory, "cpuset.cpus")
        if original is None:
            return False, "cpuset.cpus is not available for this cgroup"
        try:
            write_cgroup_file(cpus, os.path.join(directory, "cpuset.cpus"))
        except OSError as exc:
            return False, f"write cpuset.cpus failed: {exc.strerror or exc}"
        with self._lock:
            # setdefault: a second set keeps the first original, never our own value.
            self._originals.setdefault(directory, original)
        effective = self.read_cpus(directory)
        try:
            same = effective is not None and parse_cpu_list(effective) == parse_cpu_list(cpus)
        except ValueError:
            same = False
        if not same:
            return False, f"cgroupfs reports cpuset.cpus.effective={effective!r}, wanted {cpus!r}"
        return True, "ok"

    def restore(self, directory: str) -> Tuple[bool, str]:
        directory = os.path.realpath(directory)
        with self._lock:
            original = self._originals.get(directory)
        if original is None:
            return True, "not bound"
        if not os.path.isdir(directory):
            with self._lock:
                self._originals.pop(directory, None)
            return True, "cgroup already gone"
        target = os.path.join(directory, "cpuset.cpus")
        written = original
        try:
            write_cgroup_file(original, target)
        except OSError as exc:
            fallback = self._unpin_fallback(directory, original, exc)
            if fallback is None:
                return False, f"restore cpuset.cpus failed: {exc.strerror or exc}"
            try:
                write_cgroup_file(fallback, target)
            except OSError as exc2:
                return False, f"restore cpuset.cpus failed: {exc2.strerror or exc2}"
            written = fallback
        with self._lock:
            self._originals.pop(directory, None)
        logger.info("Restored cpuset.cpus of %s to %r (empty = all cores)",
                    os.path.basename(directory), written)
        return True, "ok"

    def _unpin_fallback(self, directory: str, original: str, exc: OSError) -> Optional[str]:
        """What to write instead of an empty ``cpuset.cpus`` the kernel refused.

        Kernels before 7.0 (upstream 272bd8183376) reject clearing a non-empty
        ``cpuset.cpus`` with ENOSPC while the cgroup still has tasks -- e.g. the
        shell left in a shared terminal scope after the app exits.  Writing the
        parent's effective cpus unpins just the same; it only stops tracking
        later parent/hotplug changes, and the scope is reaped by systemd anyway.
        """
        if original or exc.errno != errno.ENOSPC:
            return None
        parent = self._read(os.path.dirname(directory), "cpuset.cpus.effective")
        if not parent:
            return None
        logger.info("Kernel refused to clear cpuset.cpus of populated %s (pre-7.0); "
                    "unpinning to parent's effective cpus %s",
                    os.path.basename(directory), parent)
        return parent

    def restore_all(self) -> int:
        """Shutdown path: put every bound cgroup back; returns how many failed."""
        failed = 0
        bound = self.bound_cgroups()
        if bound:
            logger.info("Restoring CPU affinity for %d cgroup(s)", len(bound))
        for directory in bound:
            ok, msg = self.restore(directory)
            if not ok:
                failed += 1
                logger.error("Restore affinity failed for %s: %s", directory, msg)
        return failed

    # -- last successful selection (UI default, shared by every client) --
    def last_choice(self, app_id: str) -> Optional[dict]:
        """``{"class": "p"|"e"|"lpe"|"custom", "cpus": "0-1"}`` or None."""
        with self._lock:
            choice = self._last_choices.get(app_id)
            return dict(choice) if choice else None

    def remember_choice(self, app_id: str, cpu_class: str, cpus: str) -> None:
        """Record a selection that was applied successfully."""
        with self._lock:
            self._last_choices[app_id] = {"class": cpu_class, "cpus": cpus}
