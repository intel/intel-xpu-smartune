# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Device-level frequency limits: CPU (cpufreq policy) and GPU (Xe / i915 gt) min/max.

Unlike the per-app controllers, this acts on a device, not on an app, and is
manual only -- no pressure trigger.  The contract mirrors the CPU affinity one
(:mod:`controller.cpu`): the value seen before the first set is recorded and
:meth:`FreqController.restore` puts it back, a second set keeps the first
original, and :meth:`FreqController.restore_all` runs on service shutdown, so
nothing outlives SmarTune.

A *unit* is one cpufreq policy (cores that share a policy move together) or one
GPU gt.  Unit ids are only ever looked up in the list this module discovers; a
client-supplied id is never turned into a path.
"""

import glob
import os
import re
import threading
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

CPU_ROOT = "/sys/devices/system/cpu"
DRM_ROOT = "/sys/class/drm"

# Native sysfs unit per kind: cpufreq counts in kHz, the GPU drivers in MHz.
_UNIT_HZ = {"cpu": 1000, "gpu": 1}
# Readback may be rounded by the driver/hardware: allow one MHz.
_TOLERANCE = {"cpu": 1000, "gpu": 1}

_CARD_RE = re.compile(r"^card\d+$")


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _write_int(path: str, value: int) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{int(value)}\n")


class _Unit:
    """One controllable min/max pair, in the sysfs native unit (kHz for CPU, MHz for GPU)."""

    def __init__(self, uid: str, kind: str, label: str, group: str,
                 min_path: str, max_path: str, hw_min_path: str, hw_max_path: str,
                 cur_path: Optional[str], extra: Optional[dict] = None):
        self.uid = uid
        self.kind = kind
        self.label = label
        self.group = group
        self.min_path = min_path
        self.max_path = max_path
        self.hw_min_path = hw_min_path
        self.hw_max_path = hw_max_path
        self.cur_path = cur_path
        self.extra = extra or {}

    # Native values; None when the file is unreadable (the device went away).
    def hw_range(self) -> Tuple[Optional[int], Optional[int]]:
        return _read_int(self.hw_min_path), _read_int(self.hw_max_path)

    def current(self) -> Tuple[Optional[int], Optional[int]]:
        return _read_int(self.min_path), _read_int(self.max_path)

    def cur_freq(self) -> Optional[int]:
        return _read_int(self.cur_path) if self.cur_path else None


# -- discovery ---------------------------------------------------------------
def _discover_cpu(cpu_root: str) -> List[_Unit]:
    units = []
    for path in sorted(glob.glob(os.path.join(cpu_root, "cpufreq", "policy[0-9]*")),
                       key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
        name = os.path.basename(path)
        if _read_int(os.path.join(path, "scaling_max_freq")) is None:
            continue
        cpus = (_read_str(os.path.join(path, "related_cpus")) or "").split()
        units.append(_Unit(
            uid=f"cpu:{name}", kind="cpu", label=name, group="cpu",
            min_path=os.path.join(path, "scaling_min_freq"),
            max_path=os.path.join(path, "scaling_max_freq"),
            hw_min_path=os.path.join(path, "cpuinfo_min_freq"),
            hw_max_path=os.path.join(path, "cpuinfo_max_freq"),
            cur_path=os.path.join(path, "scaling_cur_freq"),
            extra={"cpus": [int(c) for c in cpus if c.isdigit()],
                   "driver": _read_str(os.path.join(path, "scaling_driver"))}))
    return units


def _gt_unit(card: str, driver: str, gt_name: str, base: str, names: dict) -> Optional[_Unit]:
    min_path = os.path.join(base, names["min"])
    if _read_int(min_path) is None:
        return None
    return _Unit(
        uid=f"gpu:{card}/{gt_name}", kind="gpu", label=gt_name, group=card,
        min_path=min_path, max_path=os.path.join(base, names["max"]),
        hw_min_path=os.path.join(base, names["rpn"]),
        hw_max_path=os.path.join(base, names["rp0"]),
        cur_path=os.path.join(base, names["cur"]),
        extra={"driver": driver})


_XE = {"min": "min_freq", "max": "max_freq", "rpn": "rpn_freq", "rp0": "rp0_freq", "cur": "cur_freq"}
_I915 = {"min": "rps_min_freq_mhz", "max": "rps_max_freq_mhz", "rpn": "rps_RPn_freq_mhz",
         "rp0": "rps_RP0_freq_mhz", "cur": "rps_cur_freq_mhz"}
_I915_LEGACY = {"min": "gt_min_freq_mhz", "max": "gt_max_freq_mhz", "rpn": "gt_RPn_freq_mhz",
                "rp0": "gt_RP0_freq_mhz", "cur": "gt_cur_freq_mhz"}


def _discover_gpu(drm_root: str) -> List[_Unit]:
    units = []
    for card_path in sorted(glob.glob(os.path.join(drm_root, "card*"))):
        card = os.path.basename(card_path)
        if not _CARD_RE.match(card):
            continue
        found = []
        # Xe: device/tile*/gt*/freq0
        for freq0 in sorted(glob.glob(os.path.join(card_path, "device", "tile*", "gt*", "freq0"))):
            gt_name = os.path.basename(os.path.dirname(freq0))
            found.append(_gt_unit(card, "xe", gt_name, freq0, _XE))
        if not any(found):
            # i915 multi-GT: card/gt/gt*/rps_*_freq_mhz
            for gt_path in sorted(glob.glob(os.path.join(card_path, "gt", "gt*"))):
                found.append(_gt_unit(card, "i915", os.path.basename(gt_path), gt_path, _I915))
        if not any(found):
            # i915 legacy single-GT: card/gt_*_freq_mhz
            found.append(_gt_unit(card, "i915", "gt0", card_path, _I915_LEGACY))
        units.extend(u for u in found if u)
    return units


def _core_class(cpus: List[int], groups: Optional[dict]) -> str:
    """"p" / "e" / "lpe" when every cpu of the policy is in that class, else "mixed" / ""."""
    if not groups or not cpus:
        return ""
    mapping = {"p": groups.get("p_cores") or [], "e": groups.get("e_cores") or [],
               "lpe": groups.get("lpe_cores") or []}
    classes = {name for cpu in cpus for name, members in mapping.items() if cpu in members}
    if len(classes) == 1 and all(any(cpu in m for m in mapping.values()) for cpu in cpus):
        return classes.pop()
    return "mixed" if classes else ""


class FreqController:
    """Device frequency limits; one instance lives on ControlManager.freq."""

    def __init__(self, cpu_root: str = CPU_ROOT, drm_root: str = DRM_ROOT):
        self.cpu_root = cpu_root
        self.drm_root = drm_root
        self._originals: Dict[str, Tuple[int, int]] = {}  # unit id -> (min, max) before first set
        self._applied: Dict[str, Tuple[int, int]] = {}    # unit id -> what we last wrote
        # Server lifetime only: stopping the service restores every unit.
        self._lock = threading.Lock()

    # -- discovery / state ------------------------------------------------
    def _units(self) -> List[_Unit]:
        return _discover_cpu(self.cpu_root) + _discover_gpu(self.drm_root)

    def _find(self, uid: str) -> Optional[_Unit]:
        for unit in self._units():
            if unit.uid == uid:
                return unit
        return None

    def limited_units(self) -> List[str]:
        with self._lock:
            return list(self._originals)

    def list_units(self, core_groups: Optional[dict] = None) -> dict:
        """Everything the dashboard needs, in MHz."""
        out = []
        with self._lock:
            originals = dict(self._originals)
        for unit in self._units():
            hz = _UNIT_HZ[unit.kind]
            hw_min, hw_max = unit.hw_range()
            cur_min, cur_max = unit.current()
            if None in (hw_min, hw_max, cur_min, cur_max):
                continue
            original = originals.get(unit.uid)
            entry = {
                "id": unit.uid,
                "kind": unit.kind,
                "label": unit.label,
                "group": unit.group,
                "hw_min_mhz": hw_min // hz,
                "hw_max_mhz": hw_max // hz,
                "min_mhz": cur_min // hz,
                "max_mhz": cur_max // hz,
                "cur_mhz": (unit.cur_freq() or 0) // hz or None,
                "limited": original is not None,
                "original_min_mhz": original[0] // hz if original else None,
                "original_max_mhz": original[1] // hz if original else None,
                "driver": unit.extra.get("driver"),
            }
            if unit.kind == "cpu":
                entry["cpus"] = unit.extra.get("cpus", [])
                entry["core_class"] = _core_class(entry["cpus"], core_groups)
            out.append(entry)
        return {"units": out}

    # -- actions ----------------------------------------------------------
    @staticmethod
    def _write_pair(unit: _Unit, want: Tuple[int, int], have: Tuple[int, int]) -> None:
        """Write min and max in an order that never leaves min > max in between."""
        want_min, want_max = want
        if want_min <= have[1]:
            _write_int(unit.min_path, want_min)
            _write_int(unit.max_path, want_max)
        else:
            _write_int(unit.max_path, want_max)
            _write_int(unit.min_path, want_min)

    def set_range(self, uid: str, min_mhz: float, max_mhz: float,
                  clamp: bool = False) -> Tuple[bool, str, Optional[dict]]:
        """Limit one unit to ``[min_mhz, max_mhz]``.  Returns ``(ok, message, applied)``.

        ``clamp`` pulls the request into the unit's hardware range instead of
        refusing it -- for a group of units whose ranges differ (P cores that top out
        at different clocks).
        """
        unit = self._find(uid)
        if unit is None:
            return False, "unknown or no longer present", None
        hz = _UNIT_HZ[unit.kind]
        hw_min, hw_max = unit.hw_range()
        have = unit.current()
        if None in (hw_min, hw_max) or None in have:
            return False, "frequency files are not readable", None
        want_min, want_max = int(round(min_mhz * hz)), int(round(max_mhz * hz))
        if clamp:
            want_min = min(max(want_min, hw_min), hw_max)
            want_max = min(max(want_max, hw_min), hw_max)
        if want_min > want_max:
            return False, "min must not exceed max", None
        if want_min < hw_min or want_max > hw_max:
            return False, (f"outside the hardware range {hw_min // hz}-{hw_max // hz} MHz"), None

        try:
            self._write_pair(unit, (want_min, want_max), have)
        except OSError as exc:
            # A half-written pair would leave a limit we do not track: put the old values back.
            try:
                self._write_pair(unit, have, unit.current())
            except OSError:
                pass
            return False, f"write failed: {exc.strerror or exc}", None
        with self._lock:
            # setdefault: a second set keeps the first original, never our own value.
            self._originals.setdefault(uid, have)
            self._applied[uid] = (want_min, want_max)

        got = unit.current()
        applied = {"min_mhz": want_min // hz, "max_mhz": want_max // hz}
        tol = _TOLERANCE[unit.kind]
        if None in got or abs(got[0] - want_min) > tol or abs(got[1] - want_max) > tol:
            return False, (f"sysfs reports {None if got[0] is None else got[0] // hz}-"
                           f"{None if got[1] is None else got[1] // hz} MHz, "
                           f"wanted {applied['min_mhz']}-{applied['max_mhz']} MHz"), applied
        return True, "ok", applied

    def restore(self, uid: str) -> Tuple[bool, str]:
        """Put back the min/max seen before the first set; a value changed elsewhere is left alone."""
        with self._lock:
            original = self._originals.get(uid)
            applied = self._applied.get(uid)
        if original is None:
            return True, "not limited by SmarTune"
        unit = self._find(uid)
        if unit is None:
            self._forget(uid)
            return True, "device already gone"
        current = unit.current()
        if None in current:
            self._forget(uid)
            return True, "device already gone"
        hz = _UNIT_HZ[unit.kind]
        tol = _TOLERANCE[unit.kind]
        if applied is not None and (abs(current[0] - applied[0]) > tol
                                    or abs(current[1] - applied[1]) > tol):
            self._forget(uid)
            logger.info("Frequency range of %s was changed outside SmarTune to %s; leaving it",
                        uid, current)
            return True, f"{current[0] // hz}-{current[1] // hz} MHz (changed outside SmarTune, kept)"
        try:
            self._write_pair(unit, original, current)
        except OSError as exc:
            return False, f"restore failed: {exc.strerror or exc}"
        self._forget(uid)
        logger.info("Restored frequency range of %s to %d-%d MHz", uid,
                    original[0] // hz, original[1] // hz)
        return True, f"{original[0] // hz}-{original[1] // hz} MHz"

    def _forget(self, uid: str) -> None:
        with self._lock:
            self._originals.pop(uid, None)
            self._applied.pop(uid, None)

    def restore_all(self) -> int:
        """Shutdown path: put every limited unit back; returns how many failed."""
        failed = 0
        limited = self.limited_units()
        if limited:
            logger.info("Restoring frequency range for %d unit(s)", len(limited))
        for uid in limited:
            ok, msg = self.restore(uid)
            if not ok:
                failed += 1
                logger.error("Restore frequency failed for %s: %s", uid, msg)
        return failed
