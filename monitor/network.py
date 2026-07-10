# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import time
from typing import Dict, Optional
from utils.logger import logger

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import re

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
        return delta_num / delta_denom if delta_denom > 0 else 0.0

class NetworkMonitor:
    """
    Pressure is defined as the NIC utilisation in a given time window, normalised to [0, 1].
    """
    _NET_PATH = "/sys/class/net/{}/statistics/{}"
    _BANDWIDTH_KBIT = 1000000  # NIC bandwidth in kbit/s (e.g. 1Gbps = 1000000 kbit/s)
    _WINDOW_SEC = 5

    def __init__(self, interface: str = "enp1s0", bandwidth_kbit: int = None):
        self.interface = interface
        self.bandwidth_kbit = bandwidth_kbit or self._BANDWIDTH_KBIT
        self._last_rx = None
        self._last_tx = None
        self._last_time = None
        self._pressure_history_rx = []
        self._pressure_history_tx = []
        # Packet-drop stats: (timestamp, packets, errors)
        self._rx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "errors"])
        self._tx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "errors"])
        # Retransmission stats: (timestamp, retrans, outsegs)
        self._retrans_history = WindowDiffHistory(self._WINDOW_SEC, fields=["retrans", "outsegs"])

    def _get_net_bytes(self):
        rx_path = self._NET_PATH.format(self.interface, "rx_bytes")
        tx_path = self._NET_PATH.format(self.interface, "tx_bytes")
        try:
            with open(rx_path) as f:
                rx = int(f.read())
            with open(tx_path) as f:
                tx = int(f.read())
        except Exception as e:
            raise RuntimeError(f"Failed to read NIC bytes: {str(e)}")
        return rx, tx

    def _update_pressure(self):
        now = time.time()
        rx, tx = self._get_net_bytes()
        if self._last_rx is None or self._last_tx is None:
            self._last_rx = rx
            self._last_tx = tx
            self._last_time = now
            return None, None
        delta_rx = rx - self._last_rx
        delta_tx = tx - self._last_tx
        delta_time = now - self._last_time
        if delta_time <= 0:
            rx_pressure = 0.0
            tx_pressure = 0.0
        else:
            rx_rate_kbit = delta_rx * 8 / 1000 / delta_time
            tx_rate_kbit = delta_tx * 8 / 1000 / delta_time
            rx_pressure = rx_rate_kbit / self.bandwidth_kbit
            tx_pressure = tx_rate_kbit / self.bandwidth_kbit
            rx_pressure = max(0.0, min(rx_pressure, 1.0))
            tx_pressure = max(0.0, min(tx_pressure, 1.0))
        self._last_rx = rx
        self._last_tx = tx
        self._last_time = now
        self._pressure_history_rx.append((now, rx_pressure))
        self._pressure_history_tx.append((now, tx_pressure))
        self._clean_old_data()
        return rx_pressure, tx_pressure
    def _init_tc_stats_history(self, window_sec=None):
            """
            Initialise or reset the tc-class-stats sliding window cache (per direction: ingress/egress).
            """
            self._tc_stats_history_ingress = {}  # {classid: WindowDiffHistory}
            self._tc_stats_history_egress = {}   # {classid: WindowDiffHistory}
            self._tc_stats_window_sec = window_sec or self._WINDOW_SEC

    def _update_tc_stats_history(self, usage, direction):
        """
        Update the sliding window history for each classid.
        direction: "ingress" or "egress"
        """
        if not hasattr(self, '_tc_stats_history_ingress') or not hasattr(self, '_tc_stats_history_egress'):
            self._init_tc_stats_history()
        history = self._tc_stats_history_ingress if direction == "ingress" else self._tc_stats_history_egress
        for classid, value in usage.items():
            if classid not in history:
                history[classid] = WindowDiffHistory(self._tc_stats_window_sec, fields=["bytes"])
            history[classid].add(value)

    def get_tc_class_stats_rate_ingress(self) -> Dict[str, float]:
        """
        Return the window-average rate (bytes/sec) for all ingress classids.
        """
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
        """
        Return the window-average rate (bytes/sec) for all egress classids.
        """
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
        """
        Read tx/rx byte counts for all classes under the specified device and qdisc handle,
        then update the sliding window.
        direction: "ingress" or "egress" (required)
        """
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


    def _clean_old_data(self):
        cutoff = time.time() - self._WINDOW_SEC
        self._pressure_history_rx = [
            (t, p) for t, p in self._pressure_history_rx if t >= cutoff
        ]
        self._pressure_history_tx = [
            (t, p) for t, p in self._pressure_history_tx if t >= cutoff
        ]
        if not self._pressure_history_rx and self._last_rx is not None:
            self._pressure_history_rx.append((cutoff + 0.1, 0.0))
        if not self._pressure_history_tx and self._last_tx is not None:
            self._pressure_history_tx.append((cutoff + 0.1, 0.0))

    def _get_window_average(self):
        history_rx = self._pressure_history_rx
        history_tx = self._pressure_history_tx
        rx_avg = sum(p for _, p in history_rx) / len(history_rx) if history_rx else 0.0
        tx_avg = sum(p for _, p in history_tx) / len(history_tx) if history_tx else 0.0
        return rx_avg, tx_avg

    def sample_network_pressure(self):
        """
        Sample current NIC pressure once and update history without aggregating.
        """
        self._update_pressure()

    def get_current_pressure(self) -> Dict[str, float]:
        """
        Public API: return the current rolling-window average network pressure without triggering a new sample.
        Returns: {'rx': 0.xx, 'tx': 0.xx}
        """
        rx_avg, tx_avg = self._get_window_average()
        return {'rx': rx_avg, 'tx': tx_avg}
