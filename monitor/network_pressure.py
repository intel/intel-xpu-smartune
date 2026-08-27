# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Network-pressure publishing + UI aggregation.

The scoring model lives in :mod:`monitor.network` (``NetworkMonitor``): sampling, EMA,
utilisation/congestion modelling, and the fused per-direction [0, 1] score. This module is
the layer *above* that: it takes the fused scores the control loop already computed and

  * publishes an immutable cross-thread snapshot (``publish_network_pressure_snapshot`` /
    ``_get_network_pressure_snapshot``) so the dashboard reads the exact values used for
    shaping instead of standing up a second monitor that would diverge on EMA/counter
    windows, and
  * transforms that snapshot into UI DTOs -- percent-scaled fields, LOW/MEDIUM/HIGH/CRITICAL
    display levels, plain-language ``reason`` text, and the System Overview gauge rollup.

Nothing here samples the NIC or holds live monitor state; it depends only on ``b_config``
thresholds. Kept separate from the model so the "how do we score" and "how do we present"
concerns evolve independently.
"""

import os
import threading
import time
from typing import Any, Dict, Optional

from config.config import b_config
from monitor.network import NetworkMonitor
from utils.logger import logger


# --- pressure snapshot: shared buffer + UI aggregation --------------------------
# The controller owns the live NetworkMonitor state; the dashboard must not create a
# second monitor (it would diverge on EMA/counter windows). Instead the controller
# publishes its fused scores here and the dashboard reads an immutable copy.

_NETWORK_PRESSURE_SNAPSHOT_LOCK = threading.Lock()
_NETWORK_PRESSURE_SNAPSHOT: Dict[str, Dict[str, float]] = {}

# Background sampler used by monitor-only mode so network pressure stays available even
# without the balancer/controller network loop.
_NETWORK_PRESSURE_REFRESH_INTERVAL_SEC: float = 1.0
_network_pressure_refresh_started = False
_network_pressure_refresh_lock = threading.Lock()
_network_pressure_stop_event = threading.Event()
_network_pressure_collector_thread = None

# get_current_pressure() diagnostic fields kept alongside the fused rx/tx score so a UI
# card can show *why* a direction is under pressure (0..1 fractions / raw ratios).
_NETWORK_PRESSURE_DIAG_KEYS = (
    "rx_util", "tx_util", "rx_distress", "tx_distress",
    "rx_collective_harm", "tx_collective_harm",
    "rx_capacity_exhausted",
    "rx_distress_drop", "rx_distress_hw",
    "rx_distress_softnet_squeeze", "rx_distress_softnet_drop",
    "tx_distress_drop", "tx_distress_fifo",
    "rx_drop_ratio", "rx_hw_ratio",
    "rx_softnet_squeeze_ratio", "rx_softnet_drop_ratio",
    "tx_drop_ratio", "tx_fifo_ratio",
    "rx_pps", "tx_pps",
)


def publish_network_pressure_snapshot(interfaces: Dict[str, Dict[str, Any]]) -> None:
    """Publish the control loop's latest fused score for every monitored NIC, so the
    dashboard reads the exact values used for shaping instead of re-sampling."""
    snapshot: Dict[str, Dict[str, float]] = {}
    for name, pressure in interfaces.items():
        if not isinstance(pressure, dict):
            continue
        try:
            rx = min(1.0, max(0.0, float(pressure.get("rx", 0.0))))
            tx = min(1.0, max(0.0, float(pressure.get("tx", 0.0))))
        except (TypeError, ValueError):
            continue
        entry: Dict[str, float] = {"rx": rx, "tx": tx}
        for key in _NETWORK_PRESSURE_DIAG_KEYS:
            value = pressure.get(key)
            if key == "rx_capacity_exhausted" and isinstance(value, bool):
                entry[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[key] = float(value)
        snapshot[str(name)] = entry
    with _NETWORK_PRESSURE_SNAPSHOT_LOCK:
        _NETWORK_PRESSURE_SNAPSHOT.clear()
        _NETWORK_PRESSURE_SNAPSHOT.update(snapshot)


def _get_network_pressure_snapshot() -> Dict[str, Dict[str, float]]:
    with _NETWORK_PRESSURE_SNAPSHOT_LOCK:
        return {name: dict(pressure) for name, pressure in _NETWORK_PRESSURE_SNAPSHOT.items()}


def _is_network_interface_candidate(name: str) -> bool:
    lower = (name or "").lower()
    if not lower or lower == "lo":
        return False
    if lower.startswith("docker") or lower.startswith("veth"):
        return False
    if lower.startswith("br-") or lower.startswith("virbr") or lower.startswith("lxc"):
        return False
    return True


def _detect_link_speed_kbit(iface: str) -> int:
    try:
        with open(f"/sys/class/net/{iface}/speed") as f:
            mbit = int(f.read().strip())
        if mbit > 0:
            return mbit * 1000
    except (OSError, ValueError):
        pass
    return 0


def _build_network_monitors() -> Dict[str, NetworkMonitor]:
    base = "/sys/class/net"
    monitors: Dict[str, NetworkMonitor] = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return monitors

    for iface in names:
        if not _is_network_interface_candidate(iface):
            continue
        iface_path = os.path.join(base, iface)
        if not os.path.exists(iface_path):
            continue
        # Bond/team/bridge slaves should be attributed to their master interface.
        if os.path.islink(os.path.join(iface_path, "master")):
            continue
        bw = _detect_link_speed_kbit(iface)
        monitors[iface] = NetworkMonitor(iface, bw)
    return monitors


def _network_pressure_collector_loop() -> None:
    monitors = _build_network_monitors()
    while not _network_pressure_stop_event.is_set():
        loop_start = time.time()
        try:
            if not monitors:
                monitors = _build_network_monitors()

            snapshot: Dict[str, Dict[str, Any]] = {}
            stale_ifaces = []
            for iface, monitor in monitors.items():
                if not os.path.exists(f"/sys/class/net/{iface}"):
                    stale_ifaces.append(iface)
                    continue
                try:
                    monitor.sample_network_pressure()
                    snapshot[iface] = monitor.get_current_pressure()
                except Exception as exc:
                    logger.debug("Network pressure sampling failed for '%s': %s", iface, exc)

            for iface in stale_ifaces:
                monitors.pop(iface, None)

            publish_network_pressure_snapshot(snapshot)
        except Exception as exc:
            logger.debug("network pressure collector error: %s", exc)

        elapsed = time.time() - loop_start
        _network_pressure_stop_event.wait(
            max(0.1, _NETWORK_PRESSURE_REFRESH_INTERVAL_SEC - elapsed)
        )


def start_network_pressure_collector() -> None:
    """Start monitor-side network pressure sampling (idempotent)."""
    global _network_pressure_refresh_started, _network_pressure_collector_thread
    with _network_pressure_refresh_lock:
        if _network_pressure_refresh_started:
            return
        _network_pressure_refresh_started = True
        _network_pressure_stop_event.clear()
        _network_pressure_collector_thread = threading.Thread(
            target=_network_pressure_collector_loop,
            daemon=True,
            name="network-pressure-collector",
        )
        _network_pressure_collector_thread.start()


def stop_network_pressure_collector() -> None:
    """Stop monitor-side network pressure sampling thread if running."""
    global _network_pressure_refresh_started
    with _network_pressure_refresh_lock:
        if not _network_pressure_refresh_started:
            return
        _network_pressure_refresh_started = False
    _network_pressure_stop_event.set()
    if _network_pressure_collector_thread is not None:
        _network_pressure_collector_thread.join(timeout=2)


def _network_pressure_level(score: float, critical_eligible: bool = True) -> str:
    thresholds = getattr(b_config, "network_thresholds", None) or {}
    if critical_eligible and score >= thresholds.get("critical", 0.9):
        return "CRITICAL"
    if score >= thresholds.get("high", 0.7):
        return "HIGH"
    if score >= thresholds.get("medium", 0.5):
        return "MEDIUM"
    return "LOW"


def _compute_fused_network_pressure(interfaces: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    """Aggregate per-NIC fused pressure for the System Overview gauge.

    Severity is the worst NIC/direction (never diluted by idle NICs); breadth is the
    NICs whose fused score reached the configured medium band.
    """
    if not interfaces:
        return {
            "busy_nics": [], "total_nics": 0, "busy_ratio": None,
            "busy_pct": None, "level": "NO DATA", "pressure_pct": None,
            "worst_nic": None, "worst_direction": None,
        }

    medium = (getattr(b_config, "network_thresholds", None) or {}).get("medium", 0.5)
    per_nic = []
    for name, pressure in interfaces.items():
        rx = pressure["rx"]
        tx = pressure["tx"]
        rx_score = rx
        critical = (getattr(b_config, "network_thresholds", None) or {}).get("critical", 0.9)
        if not pressure.get("rx_capacity_exhausted", False):
            rx_score = min(rx_score, max(0.0, float(critical) - 0.000001))
        score = max(rx_score, tx)
        direction = "RX" if rx_score > tx else "TX"
        per_nic.append((score, name, direction))

    per_nic.sort(key=lambda item: (-item[0], item[1], item[2]))
    worst_score, worst_nic, worst_direction = per_nic[0]
    busy_nics = [name for score, name, _ in per_nic if score >= medium]
    total_nics = len(per_nic)
    busy_ratio = len(busy_nics) / total_nics

    return {
        "busy_nics": busy_nics,
        "total_nics": total_nics,
        "busy_ratio": round(busy_ratio, 4),
        "busy_pct": round(busy_ratio * 100.0, 2),
        "level": _network_pressure_level(worst_score),
        "pressure_pct": round(worst_score * 100.0, 2),
        "worst_nic": worst_nic,
        "worst_direction": worst_direction,
    }


def _network_pressure_reason(direction: str, score: float, diag: Dict[str, float]) -> Optional[str]:
    """Short, user-facing explanation of why a direction reached high/critical (``None``
    below the ``high`` band): the dominant driver in plain language plus the raw rate."""
    thresholds = getattr(b_config, "network_thresholds", None) or {}
    if score < thresholds.get("high", 0.7):
        return None

    util = float(diag.get(f"{direction}_util", 0.0) or 0.0)
    distress = float(diag.get(f"{direction}_distress", 0.0) or 0.0)

    # Utilisation-driven: link genuinely near line rate, little/no loss.
    if util >= 0.90 and distress < 0.5:
        return f"near max bandwidth ({util * 100:.0f}% used)"

    # Congestion-driven: name the worst per-source distress component.
    if direction == "rx":
        candidates = [
            (diag.get("rx_distress_drop", 0.0), "packet loss", diag.get("rx_drop_ratio", 0.0)),
            (diag.get("rx_distress_hw", 0.0), "receive buffer overflow", diag.get("rx_hw_ratio", 0.0)),
            (diag.get("rx_distress_softnet_squeeze", 0.0), "system overloaded",
             diag.get("rx_softnet_squeeze_ratio", 0.0)),
            (diag.get("rx_distress_softnet_drop", 0.0), "system overloaded",
             diag.get("rx_softnet_drop_ratio", 0.0)),
        ]
    else:
        candidates = [
            (diag.get("tx_distress_drop", 0.0), "packet loss", diag.get("tx_drop_ratio", 0.0)),
            (diag.get("tx_distress_fifo", 0.0), "send buffer overflow", diag.get("tx_fifo_ratio", 0.0)),
        ]

    worst_component, label, ratio = max(candidates, key=lambda item: float(item[0] or 0.0))
    if float(worst_component or 0.0) <= 0.0:
        # Saturated but no identifiable component: fall back to util.
        return f"near max bandwidth ({util * 100:.0f}% used)"
    ratio = float(ratio or 0.0)
    if ratio > 0.0:
        # UI copy should never show impossible percentages, even if kernel counters are noisy.
        ratio_pct = max(0.0, min(ratio * 100.0, 100.0))
        return f"{label} {ratio_pct:.2f}%"
    return label


def _build_network_interface_pressure(interfaces: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
    """Per-NIC, per-direction diagnostics for the Network card: a
    ``{nic: {rx: {...}, tx: {...}}}`` map of percent-scaled fields plus a per-direction
    ``level`` and ``reason``."""
    result: Dict[str, Any] = {}
    for name, diag in interfaces.items():
        if not isinstance(diag, dict):
            continue

        def _pct(key: str) -> float:
            value = diag.get(key, 0.0)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return 0.0
            # Clamp to [0, 100]% so a counter anomaly cannot show an impossible ratio.
            return round(min(1.0, max(0.0, float(value))) * 100.0, 4)

        def _loss_pct(key: str, pps_key: str) -> Optional[float]:
            pps = diag.get(pps_key)
            raw_ratio = diag.get(key)
            if isinstance(raw_ratio, (int, float)) and not isinstance(raw_ratio, bool) and float(raw_ratio) <= 0:
                return 0.0
            if pps is None:
                # Older snapshots predate the activity metadata; keep their stored value.
                return _pct(key)
            model = getattr(b_config, "network_pressure_model", None) or {}
            min_pps = model.get("min_pps_activity", 2000.0)
            if not isinstance(pps, (int, float)) or isinstance(pps, bool) or float(pps) < float(min_pps):
                # Low packet-rate windows are too noisy for meaningful loss ratio semantics.
                # For display, keep this deterministic as "no observable loss" instead of N/A.
                return 0.0
            return _pct(key)

        rx_score = float(diag.get("rx", 0.0) or 0.0)
        tx_score = float(diag.get("tx", 0.0) or 0.0)
        result[str(name)] = {
            "rx": {
                "util_pct": _pct("rx_util"),
                "distress_pct": _pct("rx_distress"),
                "score_pct": round(rx_score * 100.0, 2),
                "drop_ratio_pct": _loss_pct("rx_drop_ratio", "rx_pps"),
                "hw_overflow_ratio_pct": _pct("rx_hw_ratio"),
                "softnet_squeeze_ratio_pct": _pct("rx_softnet_squeeze_ratio"),
                "softnet_drop_ratio_pct": _pct("rx_softnet_drop_ratio"),
                "collective_harm_pct": _pct("rx_collective_harm"),
                "level": _network_pressure_level(
                    rx_score, critical_eligible=bool(diag.get("rx_capacity_exhausted", False))
                ),
                "reason": _network_pressure_reason("rx", rx_score, diag),
            },
            "tx": {
                "util_pct": _pct("tx_util"),
                "distress_pct": _pct("tx_distress"),
                "score_pct": round(tx_score * 100.0, 2),
                "drop_ratio_pct": _loss_pct("tx_drop_ratio", "tx_pps"),
                "fifo_ratio_pct": _pct("tx_fifo_ratio"),
                "collective_harm_pct": _pct("tx_collective_harm"),
                "level": _network_pressure_level(tx_score),
                "reason": _network_pressure_reason("tx", tx_score, diag),
            },
        }
    return result
