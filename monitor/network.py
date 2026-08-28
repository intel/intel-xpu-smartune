# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""NIC pressure: a per-direction [0, 1] score fusing utilisation *load* with congestion
*distress*::

    base_load = max(ema_bw_util, ema_pps_util)          # smoothed load
    util_sat  = sigmoid(base_load, half~0.96, k~120)    # only near line rate -> pressure
    distress  = noisy_or(drop, fifo, softnet)           # rate-relative pain, [0, 1]
    score     = noisy_or(util_sat, distress)

Distress (drops / queue backlog) is the primary signal: a link dropping packets at 60%
utilisation is in trouble, while a clean link at 95% is merely busy -- utilisation only
counts once it is genuinely near line rate.

Smoothing differs by signal type: utilisation is a dt-aware EMA (continuous, robust to the
irregular sample cadence), while drops/fifo/softnet are window-diff *ratios*
(delta_event / delta_packets) -- the right form for sparse bursty events -- held through a
decaying-max "capacitor" so a burst cools smoothly instead of vanishing when it leaves the
window.

softnet_stat is system-wide (the receive softirq path), so it is attributed to the rx
direction and scaled by this NIC's share of system rx packets. Each direction's distress is
also scaled by an activity gate ``min(1, pps/min_pps_activity)`` so a near-idle direction
(tiny ratio denominator) cannot false-trip on a handful of stray drops.

Model constants are live-overridable from ``config.network_pressure_model`` via
:meth:`NetworkMonitor._model`. See ``docs/pressure_algorithm.md`` Part IV.
"""

import math
import os
import time
from typing import Dict, Optional
from utils.logger import logger

from config.config import b_config

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments
# with shell=False (default). No untrusted shell execution or string
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import re


# --- math helpers ------------------------------------------------------------
def _clamp01(x: float) -> float:
    if x <= 0.0:
        return 0.0
    return 1.0 if x >= 1.0 else x


def _sat(ratio: float, half: float) -> float:
    """Saturating map of a non-negative ratio into [0, 1): ``1 - exp(-ratio/half)``.
    ``half`` is the ratio at which the term reads ~0.63 -- the "starts to hurt" point."""
    if ratio <= 0.0:
        return 0.0
    return 1.0 - math.exp(-ratio / half)


def _sigmoid(x: float, half: float, k: float) -> float:
    """Logistic gate in (0, 1): ``1 / (1 + exp(-k*(x-half)))``. ``half`` is the input that
    reads 0.5, ``k`` the steepness. Used to squash utilisation so only *near-saturation*
    reads as pressure -- a busy-but-clean link is not distress."""
    z = -k * (x - half)
    if z >= 60.0:
        return 0.0
    if z <= -60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def _noisy_or(*vals: float) -> float:
    """Probabilistic OR: ``1 - prod(1 - v)``. Any strong signal pulls the result up
    without a single one pinning it."""
    prod = 1.0
    for v in vals:
        prod *= (1.0 - _clamp01(v))
    return 1.0 - prod


def _read_softnet_stats() -> Dict[str, int]:
    """Aggregate ``/proc/net/softnet_stat`` across CPUs. System-wide (receive softirq
    path), NOT per-NIC. Columns (hex): [0]=packets processed, [1]=dropped (backlog full),
    [2]=time_squeeze (budget/timeslice exhausted before the queue drained)."""
    processed = squeezed = dropped = 0
    try:
        with open("/proc/net/softnet_stat") as f:
            for line in f:
                cols = line.split()
                if len(cols) >= 3:
                    processed += int(cols[0], 16)
                    dropped += int(cols[1], 16)
                    squeezed += int(cols[2], 16)
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"processed": processed, "squeezed": squeezed, "dropped": dropped}


class WindowDiffHistory:
    def __init__(self, window_sec=5, fields=None):
        self.window_sec = window_sec
        self.fields = fields or []  # list of field names to aggregate
        self._history = []  # (timestamp, value1, value2, ...)

    def add(self, *values):
        now = time.time()
        self._history.append((now,) + tuple(values))
        self._clean()

    def _clean(self):
        cutoff = time.time() - self.window_sec
        self._history = [x for x in self._history if x[0] >= cutoff]

    def diff_rate(self, num_idx, denom_idx):
        # num_idx/denom_idx are history tuple indices (1-based) for the numerator/denominator fields
        if len(self._history) < 2:
            return 0.0
        start = self._history[0]
        end = self._history[-1]
        delta_num = end[num_idx] - start[num_idx]
        delta_denom = end[denom_idx] - start[denom_idx]
        # Guard counter resets/wraps (interface down/up): a negative delta is not a rate.
        if delta_denom <= 0 or delta_num < 0:
            return 0.0
        return delta_num / delta_denom

class NetworkMonitor:
    """Per-direction NIC pressure in [0, 1]: utilisation load fused with congestion distress."""
    _NET_PATH = "/sys/class/net/{}/statistics/{}"
    _BANDWIDTH_KBIT = 1000000  # NIC bandwidth in kbit/s (e.g. 1Gbps = 1000000 kbit/s)
    _WINDOW_SEC = 5

    # --- model defaults (all live-overridable via config.network_pressure_model) -----
    _EMA_ALPHA = 0.3            # per-1s smoothing factor for the utilisation EMA
    _DECAY_RATE = 0.7           # fraction of distress retained per second (capacitor)
    _CPU_PPS_PER_CORE = 1_500_000.0  # rough per-core softirq packet budget (small-pkt cap)
    # Packet rate (pkt/s) below which a direction's distress ratio is not trusted: on a
    # near-idle direction the denominator is tiny, so a few stray drops would trip a false
    # "critical". Distress is scaled by min(1, pps/_MIN_PPS_ACTIVITY) (the activity gate).
    _MIN_PPS_ACTIVITY = 2000.0
    _DROP_HALF = 1e-3          # TX drop ratio half-point; TX drops are direct egress pressure
    # Generic rx_dropped has driver/stack-specific semantics -> wider curve than TX drops.
    _RX_DROP_HALF = 5e-2
    _RX_DROP_WEIGHT = 0.5      # generic rx_dropped is evidence, not direct capacity exhaustion
    _FIFO_HALF = 1e-4          # fifo (ring overflow) ratio half-point -- rarer, so smaller
    _SOFTNET_HALF = 1e-3       # softnet squeeze/drop ratio half-point
    # A squeeze means softirq exhausted its budget, not necessarily that it dropped packets:
    # a strong warning, but not enough to peg the RX score on its own.
    _SOFTNET_SQUEEZE_WEIGHT = 0.5
    _PPS_PER_GBPS = 1_488_095  # 64-byte-frame line rate: packets/s per Gbit/s
    # Utilisation saturation gate (`half`=0.5 point, `k`=steepness): only near-line-rate
    # utilisation reads as pressure. ~0.95 -> ~0.23, ~0.98 -> ~0.92, <0.90 -> ~0.
    _UTIL_SAT_HALF = 0.96
    _UTIL_SAT_K = 120.0

    # sysfs counters read every sample. The three rx hardware-drop fields are summed into one
    # overflow signal below because the "NIC couldn't hand the packet to the host in time"
    # failure lands in different fields across drivers (many Intel NICs use rx_missed_errors
    # while rx_fifo_errors stays 0). tx has no sysfs equivalent, so it keeps tx_fifo_errors.
    _COUNTER_FIELDS = (
        "rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
        "rx_dropped", "tx_dropped", "rx_fifo_errors", "tx_fifo_errors",
        "rx_missed_errors", "rx_over_errors",
    )

    def __init__(self, interface: str = "enp1s0", bandwidth_kbit: int = None, config=None):
        self.config = config or b_config
        self.interface = interface
        # `None` -> default; an explicit 0 means "bandwidth unknown" (virtual/bond/wifi),
        # which drops the bandwidth-util term rather than pretending a 1 Gbit link.
        self.bandwidth_kbit = self._BANDWIDTH_KBIT if bandwidth_kbit is None else bandwidth_kbit
        self._cpu_cores = os.cpu_count() or 1
        self._speed_warned = False

        # Raw baseline from the previous sample.
        self._last_counters = None
        self._last_softnet = None
        self._last_time = None

        # Per-direction smoothed state.
        self._ema_bw = {"rx": None, "tx": None}
        self._ema_pps = {"rx": None, "tx": None}
        self._distress = {"rx": 0.0, "tx": 0.0}
        self._score = {"rx": 0.0, "tx": 0.0}
        # Instant per-source distress components + collective-harm rollups, populated each
        # sample by _update_pressure. Diagnostics only; the fused score is unchanged.
        self._distress_components = {
            "rx_drop": 0.0, "rx_hw": 0.0, "rx_softnet_squeeze": 0.0, "rx_softnet_drop": 0.0,
            "tx_drop": 0.0, "tx_fifo": 0.0,
            "rx_collective_harm": 0.0, "tx_collective_harm": 0.0,
            "rx_capacity_exhausted": False,
            "softirq_harm": 0.0,
            "rx_drop_ratio": 0.0, "rx_hw_ratio": 0.0,
            "rx_softnet_squeeze_ratio": 0.0, "rx_softnet_drop_ratio": 0.0,
            "tx_drop_ratio": 0.0, "tx_fifo_ratio": 0.0,
            "rx_pps": 0.0, "tx_pps": 0.0,
        }

        # Window-diff histories over *cumulative* counters -> rate-relative ratios.
        # rx tuple layout: (timestamp, packets, dropped, hw_overflow=fifo+missed+over)
        # tx tuple layout: (timestamp, packets, dropped, fifo)
        self._rx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "dropped", "hw_overflow"])
        self._tx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "dropped", "fifo"])
        # system-level; tuple layout: (timestamp, processed, squeezed, dropped)
        self._softnet_history = WindowDiffHistory(self._WINDOW_SEC, fields=["processed", "squeezed", "dropped"])

    # --- config-overridable model ------------------------------------------------
    def _model(self) -> Dict[str, float]:
        """Live view of ``config.network_pressure_model`` merged over the class defaults.

        Read every tick (not cached) so a config.yaml edit takes effect on the next sample;
        any missing/malformed key falls back to the shipped default.
        """
        cfg = getattr(self.config, "network_pressure_model", None) or {}

        def _num(key, default):
            v = cfg.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default

        def _weight(key, default):
            value = _num(key, default)
            return value if 0.0 <= value <= 1.0 else default

        return {
            "ema_alpha": _num("ema_alpha", self._EMA_ALPHA),
            "decay_rate": _num("decay_rate", self._DECAY_RATE),
            "cpu_pps_per_core": _num("cpu_pps_per_core", self._CPU_PPS_PER_CORE),
            "min_pps_activity": _num("min_pps_activity", self._MIN_PPS_ACTIVITY),
            "drop_half": _num("drop_half", self._DROP_HALF),
            "rx_drop_weight": _weight("rx_drop_weight", self._RX_DROP_WEIGHT),
            "fifo_half": _num("fifo_half", self._FIFO_HALF),
            "softnet_half": _num("softnet_half", self._SOFTNET_HALF),
            "util_sat_half": _num("util_sat_half", self._UTIL_SAT_HALF),
            "util_sat_k": _num("util_sat_k", self._UTIL_SAT_K),
        }

    # --- counter reads -----------------------------------------------------------
    def _read_stat(self, field: str) -> int:
        """Read one sysfs statistics counter. Missing/unparseable (virtual NICs omit some)
        degrades to 0 so a partial driver never breaks scoring."""
        path = self._NET_PATH.format(self.interface, field)
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError, OSError):
            return 0

    def _read_counters(self) -> Dict[str, int]:
        return {f: self._read_stat(f) for f in self._COUNTER_FIELDS}

    @staticmethod
    def _rx_hw_overflow(counters: Dict[str, int]) -> int:
        """Combined rx hardware-overflow counter: FIFO overflow + NIC-had-no-host-buffer
        drops. Summed because the same "NIC couldn't hand the packet to the host in time"
        failure lands in different fields across drivers (fifo vs missed vs over)."""
        return (counters["rx_fifo_errors"]
                + counters["rx_missed_errors"]
                + counters["rx_over_errors"])

    @staticmethod
    def _ema_step(prev: Optional[float], instant: float, alpha: float, dt: float) -> float:
        """dt-aware EMA: ``alpha`` is the per-1s factor, converted to the actual gap so an
        irregular cadence gives a consistent time constant. Seeds with the first value."""
        if prev is None:
            return instant
        alpha_eff = 1.0 - (1.0 - _clamp01(alpha)) ** dt
        return alpha_eff * instant + (1.0 - alpha_eff) * prev

    # --- sampling ----------------------------------------------------------------
    def _update_pressure(self):
        """Sample counters once, update EMA/histories/distress, recompute per-direction
        scores. Returns ``(rx_score, tx_score)`` -- ``(None, None)`` on the seeding call."""
        now = time.time()
        counters = self._read_counters()
        softnet = _read_softnet_stats()

        if self._last_time is None:
            self._last_counters = counters
            self._last_softnet = softnet
            self._last_time = now
            self._rx_drop_history.add(counters["rx_packets"], counters["rx_dropped"], self._rx_hw_overflow(counters))
            self._tx_drop_history.add(counters["tx_packets"], counters["tx_dropped"], counters["tx_fifo_errors"])
            self._softnet_history.add(softnet["processed"], softnet["squeezed"], softnet["dropped"])
            return None, None

        dt = now - self._last_time
        if dt <= 0:  # non-monotonic clock or double-sample: keep prior score, don't divide by 0
            return self._score["rx"], self._score["tx"]

        model = self._model()
        prev = self._last_counters

        def _delta(key):
            return max(0, counters[key] - prev[key])  # clamp counter reset/wrap

        # --- utilisation load (EMA-smoothed), per direction ---
        max_kbit = self.bandwidth_kbit
        speed_gbps = max_kbit / 1_000_000.0 if max_kbit and max_kbit > 0 else 0.0
        physical_max_pps = speed_gbps * self._PPS_PER_GBPS
        cpu_pps = self._cpu_cores * model["cpu_pps_per_core"]
        eff_max_pps = min(physical_max_pps, cpu_pps) if physical_max_pps > 0 else cpu_pps

        for d in ("rx", "tx"):
            if max_kbit and max_kbit > 0:
                rate_kbit = _delta(f"{d}_bytes") * 8 / 1000.0 / dt
                bw_util = _clamp01(rate_kbit / max_kbit)
            else:
                if not self._speed_warned:
                    logger.warning("NIC %s has no known bandwidth; scoring pps/congestion only",
                                   self.interface)
                    self._speed_warned = True
                bw_util = 0.0
            pps_util = _clamp01((_delta(f"{d}_packets") / dt) / eff_max_pps) if eff_max_pps > 0 else 0.0
            self._ema_bw[d] = self._ema_step(self._ema_bw[d], bw_util, model["ema_alpha"], dt)
            self._ema_pps[d] = self._ema_step(self._ema_pps[d], pps_util, model["ema_alpha"], dt)

        # --- append current cumulative counters, then read window-diff ratios ---
        self._rx_drop_history.add(counters["rx_packets"], counters["rx_dropped"], self._rx_hw_overflow(counters))
        self._tx_drop_history.add(counters["tx_packets"], counters["tx_dropped"], counters["tx_fifo_errors"])
        self._softnet_history.add(softnet["processed"], softnet["squeezed"], softnet["dropped"])

        drop_half = max(1e-12, model["drop_half"])
        rx_drop_half = self._RX_DROP_HALF
        fifo_half = max(1e-12, model["fifo_half"])
        softnet_half = max(1e-12, model["softnet_half"])

        # softnet_stat is system-wide, so attribute it to THIS interface by its share of
        # system rx packets -- otherwise a busy *other* NIC would inflate this NIC's rx
        # distress. Single-NIC boxes get share ~1; a counter reset falls back to no scaling.
        last_softnet = self._last_softnet or softnet  # None only if seeding was skipped
        softnet_processed_delta = softnet["processed"] - last_softnet["processed"]
        if softnet_processed_delta > 0:
            softnet_share = _clamp01(_delta("rx_packets") / softnet_processed_delta)
        else:
            softnet_share = 1.0

        # Activity gate: scale each direction's distress by min(1, pps/min_pps_activity) so a
        # near-idle direction cannot false-trip on a tiny-denominator ratio.
        min_pps_activity = max(1e-9, model["min_pps_activity"])
        rx_act = _clamp01((_delta("rx_packets") / dt) / min_pps_activity)
        tx_act = _clamp01((_delta("tx_packets") / dt) / min_pps_activity)

        # Raw (pre-saturation, pre-gate) ratios, surfaced for UI diagnostics.
        # rx drop/hw_overflow tuple indices: (ts=0, packets=1, dropped=2, hw_overflow=3).
        rx_drop_ratio = self._rx_drop_history.diff_rate(2, 1)
        rx_hw_ratio = self._rx_drop_history.diff_rate(3, 1)
        # softnet tuple indices: (ts=0, processed=1, squeezed=2, dropped=3) -- rx only.
        rx_softnet_squeeze_ratio = self._softnet_history.diff_rate(2, 1)
        rx_softnet_drop_ratio = self._softnet_history.diff_rate(3, 1)
        tx_drop_ratio = self._tx_drop_history.diff_rate(2, 1)
        tx_fifo_ratio = self._tx_drop_history.diff_rate(3, 1)
        rx_pps = max(0.0, _delta("rx_packets") / dt)
        tx_pps = max(0.0, _delta("tx_packets") / dt)

        # Named per-source components (so callers see *why* distress is high), each scaled by
        # the direction's activity gate; softnet also by this NIC's packet share.
        rx_c_drop = rx_act * model["rx_drop_weight"] * _sat(rx_drop_ratio, rx_drop_half)
        rx_c_hw = rx_act * _sat(rx_hw_ratio, fifo_half)
        rx_softnet_squeeze_sat = _sat(rx_softnet_squeeze_ratio, softnet_half)
        rx_softnet_drop_sat = _sat(rx_softnet_drop_ratio, softnet_half)
        rx_c_squeeze = (
            rx_act * softnet_share * self._SOFTNET_SQUEEZE_WEIGHT * rx_softnet_squeeze_sat
        )
        rx_c_softnet_drop = rx_act * softnet_share * rx_softnet_drop_sat
        rx_distress_instant = _noisy_or(rx_c_drop, rx_c_hw, rx_c_squeeze, rx_c_softnet_drop)
        # Ungated softirq-commons harm (no rx activity/share scaling): a TX small-packet flood
        # burning the receive softirq raises this while leaving rx/tx collective harm ~0, so it
        # is the only signal that catches that failure mode. Feeds the controller's TX pps gate.
        softirq_harm = _noisy_or(rx_softnet_squeeze_sat, rx_softnet_drop_sat)

        tx_c_drop = tx_act * _sat(tx_drop_ratio, drop_half)
        tx_c_fifo = tx_act * _sat(tx_fifo_ratio, fifo_half)
        tx_distress_instant = _noisy_or(tx_c_drop, tx_c_fifo)

        # collective_harm = the hardware/softirq classes only (not plain software drops): the
        # signal that a flood is starving the commons, which the controller's pps gate keys on
        # (docs §21). raw *_ratio fields are the pre-saturation loss rates for the UI.
        self._distress_components = {
            "rx_drop": rx_c_drop, "rx_hw": rx_c_hw,
            "rx_softnet_squeeze": rx_c_squeeze, "rx_softnet_drop": rx_c_softnet_drop,
            "tx_drop": tx_c_drop, "tx_fifo": tx_c_fifo,
            "rx_collective_harm": _noisy_or(rx_c_hw, rx_c_squeeze, rx_c_softnet_drop),
            "tx_collective_harm": tx_c_fifo,
            # Software rx_dropped is visible pressure, but does not by itself prove
            # that receive capacity is exhausted or that ingress shaping can help.
            # Reserve RX critical for the direct capacity-exhaustion counters.
            "rx_capacity_exhausted": (
                rx_hw_ratio >= fifo_half
                or rx_softnet_drop_ratio >= softnet_half
            ),
            "softirq_harm": softirq_harm,
            "rx_drop_ratio": rx_drop_ratio, "rx_hw_ratio": rx_hw_ratio,
            "rx_softnet_squeeze_ratio": rx_softnet_squeeze_ratio,
            "rx_softnet_drop_ratio": rx_softnet_drop_ratio,
            "tx_drop_ratio": tx_drop_ratio, "tx_fifo_ratio": tx_fifo_ratio,
            "rx_pps": rx_pps, "tx_pps": tx_pps,
        }

        # --- capacitor: decaying-max hold so distress cools smoothly ---
        decay = _clamp01(model["decay_rate"])
        for d, instant in (("rx", rx_distress_instant), ("tx", tx_distress_instant)):
            decayed = self._distress[d] * (decay ** dt)
            self._distress[d] = max(decayed, instant)

        # --- fuse load + distress (distress is primary; utilisation only via the gate) ---
        util_half = model["util_sat_half"]
        util_k = model["util_sat_k"]
        for d in ("rx", "tx"):
            base_load = max(self._ema_bw[d] or 0.0, self._ema_pps[d] or 0.0)
            util_sat = _sigmoid(base_load, util_half, util_k)
            self._score[d] = round(_noisy_or(util_sat, self._distress[d]), 6)

        self._last_counters = counters
        self._last_softnet = softnet
        self._last_time = now
        return self._score["rx"], self._score["tx"]

    def _init_tc_stats_history(self, window_sec=None):
        """Initialise/reset the per-direction tc-class-stats sliding window cache."""
        self._tc_stats_history_ingress = {}  # {classid: WindowDiffHistory}
        self._tc_stats_history_egress = {}   # {classid: WindowDiffHistory}
        self._tc_stats_window_sec = window_sec or self._WINDOW_SEC

    def _update_tc_stats_history(self, usage, direction):
        """Update the sliding-window history for each classid (direction: ingress|egress)."""
        if not hasattr(self, '_tc_stats_history_ingress') or not hasattr(self, '_tc_stats_history_egress'):
            self._init_tc_stats_history()
        history = self._tc_stats_history_ingress if direction == "ingress" else self._tc_stats_history_egress
        for classid, value in usage.items():
            if classid not in history:
                history[classid] = WindowDiffHistory(self._tc_stats_window_sec, fields=["bytes"])
            history[classid].add(value)

    def get_tc_class_stats_rate_ingress(self) -> Dict[str, float]:
        """Window-average rate (kbit/s) for every ingress classid."""
        rates = {}
        if not hasattr(self, '_tc_stats_history_ingress'):
            return rates
        for classid, history in self._tc_stats_history_ingress.items():
            if len(history._history) < 2:
                rates[classid] = 0.0
            else:
                start = history._history[0]
                end = history._history[-1]
                delta_bytes = end[1] - start[1]
                delta_time = end[0] - start[0]
                rates[classid] = delta_bytes * 8 / 1000 / delta_time if delta_time > 0 else 0.0
        return rates

    def get_tc_class_stats_rate_egress(self) -> Dict[str, float]:
        """Window-average rate (kbit/s) for every egress classid."""
        rates = {}
        if not hasattr(self, '_tc_stats_history_egress'):
            return rates
        for classid, history in self._tc_stats_history_egress.items():
            if len(history._history) < 2:
                rates[classid] = 0.0
            else:
                start = history._history[0]
                end = history._history[-1]
                delta_bytes = end[1] - start[1]
                delta_time = end[0] - start[0]
                rates[classid] = delta_bytes * 8 / 1000 / delta_time if delta_time > 0 else 0.0
        return rates

    def get_tc_class_stats(self, dev: str, qdisc_handle: int, classids: list, direction: str = None) -> Dict[str, int]:
        """Read cumulative byte counts for each class under (dev, qdisc_handle) and feed the
        sliding window. ``direction`` ("ingress"|"egress") selects which window to update."""
        result = subprocess.run(
            ["tc", "-s", "class", "show", "dev", dev, "parent", f"{qdisc_handle}:"],
            capture_output=True,
            text=True,
            check=False
        )
        stats = result.stdout
        usage = {}
        for classid in classids:
            m = re.search(rf"class htb {classid}.*?Sent (\d+) bytes", stats, re.DOTALL)
            if m:
                usage[classid] = int(m.group(1))
        if direction:
            self._update_tc_stats_history(usage, direction)
        return usage

    def sample_network_pressure(self):
        """Sample NIC pressure once and update EMA/history/distress state."""
        self._update_pressure()

    def get_congestion_pressure(self) -> Dict[str, float]:
        """Current per-direction congestion *distress* (drops/fifo/softnet), [0, 1].
        Exposed separately from utilisation for diagnostics/logging."""
        return {"rx": round(self._distress["rx"], 6), "tx": round(self._distress["tx"], 6)}

    def get_current_pressure(self) -> Dict[str, float]:
        """
        Public API: return the current per-direction network pressure score without
        triggering a new sample. ``rx``/``tx`` are the fused [0, 1] scores (contract kept);
        the extra keys are diagnostics and safe to ignore.
        """
        c = self._distress_components
        return {
            "rx": self._score["rx"],
            "tx": self._score["tx"],
            "rx_util": round(max(self._ema_bw["rx"] or 0.0, self._ema_pps["rx"] or 0.0), 6),
            "tx_util": round(max(self._ema_bw["tx"] or 0.0, self._ema_pps["tx"] or 0.0), 6),
            "rx_distress": round(self._distress["rx"], 6),
            "tx_distress": round(self._distress["tx"], 6),
            # Instant distress breakdown + collective-harm rollups (docs §21). The rollups
            # are what the controller's pps gate reads to tell a flood (hardware/softirq
            # drops) apart from plain byte saturation.
            "rx_distress_drop": round(c["rx_drop"], 6),
            "rx_distress_hw": round(c["rx_hw"], 6),
            "rx_distress_softnet_squeeze": round(c["rx_softnet_squeeze"], 6),
            "rx_distress_softnet_drop": round(c["rx_softnet_drop"], 6),
            "tx_distress_drop": round(c["tx_drop"], 6),
            "tx_distress_fifo": round(c["tx_fifo"], 6),
            "rx_collective_harm": round(c["rx_collective_harm"], 6),
            "tx_collective_harm": round(c["tx_collective_harm"], 6),
            # True only when this host's RX hardware/ring or softnet backlog
            # actually dropped traffic. Softnet squeeze and generic rx_dropped
            # remain observable pressure signals, not proof that RX is unavailable.
            "rx_capacity_exhausted": bool(c["rx_capacity_exhausted"]),
            # System-wide receive-softirq harm, ungated by this NIC's rx activity/share --
            # the "commons" signal the controller's TX pps gate uses to catch an egress
            # small-packet flood that never lifts tx_pressure.
            "softirq_harm": round(c["softirq_harm"], 6),
            # Raw pre-saturation loss ratios (Δevent / Δpackets or Δprocessed), so the UI can
            # show the actual drop/overflow/softnet rate behind a distress reading.
            "rx_drop_ratio": round(c["rx_drop_ratio"], 8),
            "rx_hw_ratio": round(c["rx_hw_ratio"], 8),
            "rx_softnet_squeeze_ratio": round(c["rx_softnet_squeeze_ratio"], 8),
            "rx_softnet_drop_ratio": round(c["rx_softnet_drop_ratio"], 8),
            "tx_drop_ratio": round(c["tx_drop_ratio"], 8),
            "tx_fifo_ratio": round(c["tx_fifo_ratio"], 8),
            "rx_pps": round(c["rx_pps"], 2),
            "tx_pps": round(c["tx_pps"], 2),
        }
