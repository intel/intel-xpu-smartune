#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor

from utils.logger import logger
from monitor.psi import PSIMonitor
from monitor.res_monitor import ResourceMonitor
from monitor.cgroup import CgroupMonitor
from monitor.pressure import PressureAnalyzer

from controller.controller import Controller
from controller.io import IOController
from controller.cpu import CPUController
from controller.memory import MemoryController
from controller.governor import GovernorController
from config.config import b_config


class ControlManager:
    def __init__(self):
        self.config = b_config
        self.psi = PSIMonitor()
        self.res = ResourceMonitor()
        self.cgroup = CgroupMonitor(self.config.cgroup_mount)
        self.analyzer = PressureAnalyzer(self.config)

        self.controller = Controller()
        self.cpu = CPUController(self.config.cgroup_mount)
        self.memory = MemoryController(self.config.cgroup_mount)
        self.governor = GovernorController()
        self.balance_url = f"{self.config.balance_service['url']}:{self.config.balance_service['port']}"

        self._current_level = None
        self.is_current_disk_io_stressed = False
        self.score = 0.0
        self._last_update_time = 0
        self._CACHE_TTL = self.config.regular_update_sys_pressure_time
        self._is_limited_app_dominant = False
        self._update_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1)

        self._start_auto_refresh_update_system_pressure()

    def set_limited_app_dominant(self, is_dominant: bool):
        """设置受限应用是否占主导状态"""
        if self._is_limited_app_dominant != is_dominant:
            self._is_limited_app_dominant = is_dominant

    def _start_auto_refresh_update_system_pressure(self):
        """启动定时更新system压力状态"""
        def refresh_loop():
            while True:
                time.sleep(self._CACHE_TTL * 0.9)
                self._safe_update()

        threading.Thread(target=refresh_loop, daemon=True).start()

    def _safe_update(self):
        """线程安全的更新操作"""
        if self._update_lock.acquire(blocking=False):
            try:
                self._current_level, self.score, self.is_current_disk_io_stressed = self._update_pressure_level()
            finally:
                self._update_lock.release()

    def get_current_pressure_level(self) -> tuple[str, bool]:
        """获取当前压力等级（无需参数）"""
        logger.debug("Current PSI level: %s (pressure: %.2f), disk io stressed: %s", self._current_level, self.score,
                     self.is_current_disk_io_stressed)
        return self._current_level, self.is_current_disk_io_stressed

    def _update_pressure_level(self) -> tuple[str, float, bool]:
        """更新压力等级（使用内部状态）"""
        try:
            psi_data = self.psi.get_current_pressure()
            usage_data = self.res.get_resource_usage()
            disk_io = self.res.is_disk_io_stressed()
            score = self.analyzer.calculate_pressure_score(
                psi_data,
                usage_data,
                self._is_limited_app_dominant
            )
            logger.debug(f"disk_io={disk_io}")
            level = self.analyzer.get_pressure_level(score, self.config.thresholds)
            # logger.debug("Updated PSI level: %s (pressure: %.2f)", level, score)
            self._last_update_time = time.time()
            return level, score, disk_io.get("is_stressed", False)
        except Exception as e:
            logger.error("Failed to update pressure level: %s", str(e))
            return "unknown", 0.0, False

    def update_network_pressure_level(self, network_data):
        """
        单独更新网络压力等级
        返回: (tx_level, rx_level)
        """
        try:
            tx_level = self.analyzer.get_pressure_level(network_data['tx'], self.config.network_thresholds)
            rx_level = self.analyzer.get_pressure_level(network_data['rx'], self.config.network_thresholds)
            return tx_level, rx_level
        except Exception as e:
            logger.error("Failed to update network pressure level: %s", str(e))
            return ("unknown", "unknown")

    def adjust_resources(self, app_id: str, policy: str, **resource_kwargs):
        """Adjust resources with optional parameters (保持原接口兼容)"""
        try:
            logger.info(
                f"Adjusting resources for app_id={app_id} with policy={policy} and resource_kwargs={resource_kwargs}")
            adjustments = {
                'low': lambda: self._low_pressure_adjustment(app_id),
                'medium': lambda: self._medium_pressure_adjustment(app_id, **resource_kwargs),
                'high': lambda: self._high_pressure_adjustment(app_id),
                'critical': lambda: self._critical_pressure_adjustment(app_id, **resource_kwargs),
            }
            adjustment_func = adjustments.get(policy, lambda: None)
            return adjustment_func()
        except Exception as e:
            logger.error("Adjust failed: %s", str(e))
            return False

    def _low_pressure_adjustment(self, app_id: str):
        """Low pressure adjustments."""
        logger.info("Performing low pressure adjustments for app_id=%s", app_id)
        results = [
            self.governor.set_powersave(),
            self.controller.set_all_resources(app_id, is_restore=True)
        ]

        return all(results)

    def _medium_pressure_adjustment(self, app_id: str, **kwargs):
        """Medium pressure adjustments."""
        logger.info("Performing medium pressure adjustments for app_id=%s", app_id)
        cpu_quota = kwargs.get('cpu_quota', None)
        mem_high = kwargs.get('mem_high', None)
        io_weight = kwargs.get('io_weight', None)

        results = [
            self.governor.set_performance(),
            self.controller.set_all_resources(
                app_id,
                cpu_quota=int(cpu_quota) if cpu_quota is not None else None,
                mem_high=int(mem_high) if mem_high is not None else None,
                io_weight=int(io_weight) if io_weight is not None else None,
                is_restore=False
            )
        ]

        return all(results)

    def _high_pressure_adjustment(self, app_id: str):
        """High pressure adjustments."""
        results = [
            self.governor.set_performance(),
            self.controller.high_cpu_throttle()
        ]

        return all(results)

    def _critical_pressure_adjustment(self, app_id: str, **kwargs):
        """Critical调整"""
        logger.info("Performing critical pressure adjustments for app_id=%s", app_id)
        cpu_quota = kwargs.get('cpu_quota', None)
        mem_high = kwargs.get('mem_high', None)
        io_weight = kwargs.get('io_weight', None)

        return all([
            self.governor.set_performance(),
            # TODO: 分别控制各组件，根据不同的config配置
            self.controller.set_all_resources(
                app_id,
                cpu_quota=int(cpu_quota) if cpu_quota is not None else None,
                mem_high=int(mem_high) if mem_high is not None else None,
                io_weight=int(io_weight) if io_weight is not None else None,
                is_restore=False
            )
            # self.cpu.set_weight("critical", 500),
            # self.memory.protect("critical", "min", "4G")
        ])

    def get_app_priority(self, app_id: str = "", app_name: str = "") -> str:
        """Get the priority of an application."""
        resp = requests.post(
            f"{self.balance_url}/{self.config.balance_service['get_priority']}",
            json={"app_id": app_id, "app_name": app_name},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["data"].get("priority", "low")
        else:
            logger.error("Failed to get app priority: %s", resp.text)
            return "low"

    def get_priority_value(self, priority_str: str = "") -> int:
        """
        :param priority_str: e.g. critical
        :return: 100
        """
        priority = priority_str.lower()
        print(f"Getting priority value for: {priority}, self.config.app_priority: {self.config.app_priority}")
        if priority not in self.config.app_priority:
            raise ValueError(f"Invalid priority: {priority_str}")
        return self.config.app_priority[priority]

    def __del__(self):
        """清理线程池资源"""
        self._executor.shutdown(wait=False)
