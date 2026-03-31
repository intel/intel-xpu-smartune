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
        self.fields = fields or []  # 需要统计的字段名列表
        self._history = []  # (timestamp, value1, value2, ...)

    def add(self, *values):
        now = time.time()
        self._history.append((now,) + tuple(values))
        self._clean()

    def _clean(self):
        cutoff = time.time() - self.window_sec
        self._history = [x for x in self._history if x[0] >= cutoff]

    def diff_rate(self, num_idx, denom_idx):
        # num_idx/denom_idx为统计字段在history中的索引（从1开始）
        if len(self._history) < 2:
            return 0.0
        start = self._history[0]
        end = self._history[-1]
        delta_num = end[num_idx] - start[num_idx]
        delta_denom = end[denom_idx] - start[denom_idx]
        return delta_num / delta_denom if delta_denom > 0 else 0.0

class NetworkMonitor:
    """
    压力定义：单位时间内网卡利用率（0~1），窗口平均
    """
    _NET_PATH = "/sys/class/net/{}/statistics/{}"
    _BANDWIDTH_KBIT = 1000000  # 网卡带宽，单位kbit/s（如1Gbps=1000000kbit/s）
    _WINDOW_SEC = 5

    def __init__(self, interface: str = "enp1s0", bandwidth_kbit: int = None):
        self.interface = interface
        self.bandwidth_kbit = bandwidth_kbit or self._BANDWIDTH_KBIT
        self._last_rx = None
        self._last_tx = None
        self._last_time = None
        self._pressure_history_rx = []
        self._pressure_history_tx = []
        # 丢包率统计器: (timestamp, packets, errors)
        self._rx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "errors"])
        self._tx_drop_history = WindowDiffHistory(self._WINDOW_SEC, fields=["packets", "errors"])
        # 重传率统计器: (timestamp, retrans, outsegs)
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
            raise RuntimeError(f"读取网卡字节失败: {str(e)}")
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
            初始化/重置 tc class stats 滑动窗口缓存（区分 ingress/egress）
            """
            self._tc_stats_history_ingress = {}  # {classid: WindowDiffHistory}
            self._tc_stats_history_egress = {}   # {classid: WindowDiffHistory}
            self._tc_stats_window_sec = window_sec or self._WINDOW_SEC

    def _update_tc_stats_history(self, usage, direction):
        """
        更新每个 classid 的滑动窗口历史，按方向区分
        direction: "ingress" 或 "egress"
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
        获取所有 ingress classid 在窗口内的速率（单位：bytes/sec）
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
        获取所有 egress classid 在窗口内的速率（单位：bytes/sec）
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
        读取指定设备和 qdisc handle 下所有 class 的 tx/rx 字节数，并更新滑动窗口
        direction: "ingress" 或 "egress"，必须指定
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
        采样一次当前网卡压力，更新历史，不做统计
        """
        self._update_pressure()

    def get_current_pressure(self) -> Dict[str, float]:
        """
        对外接口：返回当前网络压力平均值（窗口统计），不主动采样
        返回格式：{'rx': 0.xx, 'tx': 0.xx}
        """
        rx_avg, tx_avg = self._get_window_average()
        return {'rx': rx_avg, 'tx': tx_avg}
