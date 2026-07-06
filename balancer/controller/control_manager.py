# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from concurrent.futures import ThreadPoolExecutor

from utils.logger import logger
from monitor.monitor_api import SystemPressureMonitor

from controller.controller import Controller
from controller.cpu import CPUController
from controller.memory import MemoryController
from controller.governor import GovernorController
from config.config import b_config


class ControlManager:
    def __init__(self):
        self.config = b_config
        self.system_pressure_monitor = SystemPressureMonitor(self.config)
        self.res = self.system_pressure_monitor.res

        self.controller = Controller()
        self.cpu = CPUController(self.config.cgroup_mount)
        self.memory = MemoryController(self.config.cgroup_mount)
        self.governor = GovernorController()

        self._executor = ThreadPoolExecutor(max_workers=1)

    def register_critical_state_listener(self, callback) -> None:
        """Register a callback invoked when system pressure enters or leaves critical.

        Forwarded to the underlying SystemPressureMonitor.  See
        SystemPressureMonitor.register_critical_state_listener for details.
        """
        self.system_pressure_monitor.register_critical_state_listener(callback)

    def set_limited_app_dominant(self, is_dominant: bool):
        """ Set whether the limited app is dominant. """
        self.system_pressure_monitor.set_limited_app_dominant(is_dominant)

    def get_current_pressure_level(self) -> tuple:
        """ Get system pressure level, score, disk pressure status, and PSI data. """
        return self.system_pressure_monitor.get_current_pressure_level()

    def consume_peak_pressure_level(self) -> tuple:
        """ Return the highest pressure level seen since the last call and reset the peak.
        Used by the balancer loop to avoid missing transient critical spikes. """
        return self.system_pressure_monitor.consume_peak_pressure_level()

    def update_network_pressure_level(self, network_data):
        """ Get network pressure level based on network data. """
        return self.system_pressure_monitor.update_network_pressure_level(network_data)

    def adjust_resources(self, app_id: str, policy: str, **resource_kwargs):
        """Adjust resources with optional parameters"""
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
        """Critical pressure adjustments."""
        logger.info("Performing critical pressure adjustments for app_id=%s", app_id)
        cpu_quota = kwargs.get('cpu_quota', None)
        mem_high = kwargs.get('mem_high', None)
        io_weight = kwargs.get('io_weight', None)

        return all([
            self.governor.set_performance(),
            self.controller.set_all_resources(
                app_id,
                cpu_quota=int(cpu_quota) if cpu_quota is not None else None,
                mem_high=int(mem_high) if mem_high is not None else None,
                io_weight=int(io_weight) if io_weight is not None else None,
                is_restore=False
            )
        ])

    def __del__(self):
        """Clean up the thread pool."""
        self._executor.shutdown(wait=False)
