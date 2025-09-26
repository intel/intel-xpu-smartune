import requests

from utils.logger import logger
from monitor.psi import PSIMonitor
from monitor.cgroup import CgroupMonitor
from monitor.pressure import PressureAnalyzer

from controller.controller import Controller
from controller.io import IOController
from controller.cpu import CPUController
from controller.memory import MemoryController
from controller.governor import GovernorController
from config.config import Config  # Assuming Config is defined in a module named `config`


class ControlManager:
    def __init__(self, config_file="config/config.yaml"):
        self.config = Config.from_file(config_file)
        self.psi = PSIMonitor()
        self.cgroup = CgroupMonitor(self.config.cgroup_mount)
        self.analyzer = PressureAnalyzer(self.config)

        self.controller = Controller(self.config.cgroup_mount)
        self.io = IOController(self.config.cgroup_mount)
        self.cpu = CPUController(self.config.cgroup_mount)
        self.memory = MemoryController(self.config.cgroup_mount)
        self.governor = GovernorController(self.config.cgroup_mount)
        self.balance_url = f"{self.config.balance_service['url']}:{self.config.balance_service['port']}"

    def _get_current_pressure_level(self) -> str:
        """Get the current system pressure level."""
        try:
            psi_data = self.psi.get_current_pressure()
            score = self.analyzer.calculate_pressure_score(psi_data)
            level = self.analyzer.get_pressure_level(score)
            # level = "critical" # debug
            logger.debug("Current PSI level: %s (score: %.2f)", level, score)
            return level
        except Exception as e:
            logger.error("Failed to get current pressure level: %s", str(e))
            return "unknown"

    def adjust_resources(self, app_id: str, policy: str, **resource_kwargs):
        """Adjust resources with optional parameters (保持原接口兼容)"""
        try:
            if policy == "restore":
                return self.controller.set_all_resources(app_id, is_restore=True)

            logger.info(f"Adjusting resources for app_id={app_id} with policy={policy} and resource_kwargs={resource_kwargs}")
            adjustments = {
                'low': self._low_pressure_adjustment,
                'medium': self._medium_pressure_adjustment,
                'high': self._high_pressure_adjustment,
                'critical': lambda: self._critical_pressure_adjustment(app_id, **resource_kwargs),
            }
            return adjustments.get(policy, lambda: None)()
        except Exception as e:
            logger.error("Adjust failed: %s", str(e))
            return False

    def _low_pressure_adjustment(self, app_id: str):
        """Low pressure adjustments."""
        results = [
            self.governor.set_powersave(),
            self.controller.restore_cpu_throttle()
        ]

        return all(results)

    def _medium_pressure_adjustment(self, app_id: str):
        """Medium pressure adjustments."""
        results = [
            self.governor.set_powersave(),
            self.controller.restore_cpu_throttle()
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
            # self.io.set_weight("best-effort", 10),
            # self.memory.set_limit("best-effort", "high", "20%"),
            # self.io.set_limit("best-effort", "max", "1000"),
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
