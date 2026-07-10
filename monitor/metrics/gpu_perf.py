# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""GPU usage monitoring via gpu_monitor.py.

Uses the GPUMonitor class directly (no external process) to collect
engine utilization, frequency, RC6 residency, and power data for all
Intel GPUs (i915 / Xe).

Public API:
    get_gpu_usage_output()  -> Dict with available/parsed/error
    shutdown_gpu_usage()    -> Tear down the GPU monitor
"""

import threading
import time
from typing import Any, Dict, List, Optional

from utils.logger import logger

# ---------------------------------------------------------------------------
#  Engine name mapping: gpu_monitor display names -> short keys (rcs/bcs/…)
# ---------------------------------------------------------------------------

_DISPLAY_TO_SHORT = {
    "render/3d": "rcs",
    "blitter": "bcs",
    "video": "vcs",
    "videoenhance": "vecs",
    "compute": "ccs",
}


def _engine_display_to_short(display_name: str) -> Optional[str]:
    """Map gpu_monitor engine display names to short engine keys.

    gpu_monitor produces names like:
      i915:  "Render/3D/0", "Blitter/0", "Compute/0"
      xe:    "Render/3D", "Compute", "Blitter"  (fdinfo mode, class-level)
           or "GT:0 Render/3D/0" (PMU mode, per-instance)

    We need to return: rcs, bcs, vcs, vecs, ccs.
    """
    lowered = display_name.strip().lower()
    # Strip "gt:N " prefix from Xe PMU mode
    if lowered.startswith("gt:"):
        parts = lowered.split(" ", 1)
        if len(parts) > 1:
            lowered = parts[1]

    for display_key, short in _DISPLAY_TO_SHORT.items():
        if lowered == display_key or lowered.startswith(display_key + "/"):
            return short

    return None


# ---------------------------------------------------------------------------
#  GPUMonitor singleton
# ---------------------------------------------------------------------------

_MONITOR_STATE: Dict[str, Any] = {
    "monitor": None,
    "last_result": None,
    "last_error": None,
    "init_attempted": False,
}
_MONITOR_LOCK = threading.Lock()
_SHUTDOWN = False


def _get_or_init_monitor():
    """Lazily initialize the GPUMonitor singleton.

    Tries the default mode first (sysfs/fdinfo for Xe, PMU for i915).
    Returns the monitor instance or None on failure.
    """
    global _SHUTDOWN
    if _SHUTDOWN:
        return None

    monitor = _MONITOR_STATE.get("monitor")
    if monitor is not None:
        return monitor

    if _MONITOR_STATE.get("init_attempted"):
        return None

    _MONITOR_STATE["init_attempted"] = True

    try:
        from monitor.gpu_monitor import GPUMonitor
        monitor = GPUMonitor(xe_pmu=False)
        monitor.start_sampling()
        # Sleep past one hwmon energy-counter refresh cycle (~50-100ms on xe
        # dGPU) before returning, so the caller's first sample_delta() gets
        # dt ≈ 0.2s against this baseline instead of a microsecond-gap delta,
        # which would amplify a single counter tick into 1000s of watts.
        time.sleep(0.2)
        _MONITOR_STATE["monitor"] = monitor
        logger.info("GPUMonitor initialized successfully (sysfs/fdinfo mode)")
        return monitor
    except Exception as exc:
        _MONITOR_STATE["last_error"] = str(exc)
        logger.warning("GPUMonitor init failed: %s", exc)
        return None


def _convert_monitor_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert GPUMonitor.compute() output to the standard GPU usage format."""
    devices: List[Dict[str, Any]] = []

    for r in results:
        driver = r.get("driver", "")
        pci_slot = r.get("pci_slot", "")

        # --- Frequency ---
        freq_data = r.get("frequency") or {}
        freqs: List[Dict[str, Any]] = []
        for gt_name in sorted(freq_data.keys()):
            f = freq_data[gt_name]
            freqs.append({
                "name": gt_name,
                "min_mhz": f.get("min_mhz"),
                "cur_mhz": f.get("req_mhz"),
                "act_mhz": f.get("act_mhz"),
                "max_mhz": f.get("max_mhz"),
                "rc6_pct": f.get("rc6_pct"),
                "throttled": False,
                "throttle_reasons": [],
            })

        # --- Power ---
        power = r.get("power") or {}
        power_w = {
            "gpu": power.get("gpu_w"),
            "pkg": power.get("pkg_w"),
        }
        if "card_w" in power:
            power_w["card"] = power["card_w"]

        # --- Engine utilization ---
        # Accumulate per-instance values, then average by engine type.
        # e.g. Video/0=80%, Video/1=20% -> vcs=50%
        engines_data = r.get("engines") or {}
        engine_accum: Dict[str, List[float]] = {}
        engine_util: Dict[str, Optional[float]] = {}
        engine_names: List[str] = []

        for display_name, eng_data in engines_data.items():
            short_name = _engine_display_to_short(display_name)
            if short_name is None:
                continue
            busy_pct = eng_data.get("busy_pct")
            if busy_pct is not None:
                engine_accum.setdefault(short_name, []).append(busy_pct)

        for key, values in engine_accum.items():
            engine_util[key] = round(sum(values) / len(values), 2)

        # Ensure standard engine order
        for key in ("rcs", "bcs", "vcs", "vecs", "ccs"):
            if key in engine_util and key not in engine_names:
                engine_names.append(key)

        is_integrated = r.get("is_integrated", False)
        dev_type = "integrated" if is_integrated else "discrete"

        devices.append({
            "pci_dev": pci_slot if pci_slot != "unknown" else None,
            "dev_type": dev_type,
            "drv_name": driver,
            "engines": engine_names,
            "freqs": freqs,
            "power_w": power_w,
            "engine_util": engine_util,
        })

    return {
        "timestamp": time.time(),
        "version": "gpu_monitor",
        "devices": devices,
    }


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def get_gpu_usage_output() -> Dict[str, Any]:
    """Sample the GPU monitor and return results.

    Returns dict with keys: available, raw, parsed, error.
    """
    with _MONITOR_LOCK:
        monitor = _get_or_init_monitor()

    if monitor is None:
        error = _MONITOR_STATE.get("last_error") or "gpu_monitor not available"
        cached = _MONITOR_STATE.get("last_result")
        if cached:
            return {
                "available": True,
                "raw": None,
                "parsed": cached,
                "error": f"gpu_monitor unavailable, using cached: {error}",
            }
        return {"available": False, "raw": None, "parsed": None, "error": error}

    try:
        results = monitor.sample_delta()
        if not results:
            cached = _MONITOR_STATE.get("last_result")
            return {
                "available": True,
                "raw": None,
                "parsed": cached,
                "error": "gpu_monitor returned empty results",
            }

        parsed = _convert_monitor_results(results)
        _MONITOR_STATE["last_result"] = parsed
        _MONITOR_STATE["last_error"] = None
        return {"available": True, "raw": None, "parsed": parsed, "error": None}

    except Exception as exc:
        logger.debug("gpu_monitor sample_delta failed: %s", exc)
        _MONITOR_STATE["last_error"] = str(exc)
        cached = _MONITOR_STATE.get("last_result")
        if cached:
            return {
                "available": True,
                "raw": None,
                "parsed": cached,
                "error": f"gpu_monitor sample failed, using cached: {exc}",
            }
        return {"available": False, "raw": None, "parsed": None, "error": str(exc)}


def shutdown_gpu_usage() -> None:
    """Shut down the GPU monitor and release resources."""
    global _SHUTDOWN
    _SHUTDOWN = True

    with _MONITOR_LOCK:
        monitor = _MONITOR_STATE.get("monitor")
        if monitor is not None:
            try:
                monitor.close()
            except Exception as exc:
                logger.debug("GPUMonitor close failed: %s", exc)
            _MONITOR_STATE["monitor"] = None
        _MONITOR_STATE["last_result"] = None
        _MONITOR_STATE["last_error"] = None
        _MONITOR_STATE["init_attempted"] = False
