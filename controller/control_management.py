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


class ControlManagement:
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
            logger.debug("Current PSI level: %s (score: %.2f)", level, score)
            return level
        except Exception as e:
            logger.error("Failed to get current pressure level: %s", str(e))
            return "unknown"

    def adjust_resources(self, pressure_level: str):
        """Adjust resources based on the pressure level."""
        try:
            adjustments = {
                'low': self._low_pressure_adjustment,
                'medium': self._medium_pressure_adjustment,
                'high': self._high_pressure_adjustment,
                'critical': self._critical_pressure_adjustment
            }
            adjustment_method = adjustments.get(pressure_level, lambda: None)
            adjustment_method()
        except Exception as e:
            logger.error("Failed to adjust resources: %s", str(e))

    def _low_pressure_adjustment(self):
        """Low pressure adjustments."""
        self.governor.set_powersave()
        self.controller.restore_cpu_throttle()

    def _medium_pressure_adjustment(self):
        """Medium pressure adjustments."""
        self.governor.set_powersave()
        self.controller.restore_cpu_throttle()

    def _high_pressure_adjustment(self):
        """High pressure adjustments."""
        self.governor.set_performance()
        self.controller.high_cpu_throttle()

    def _critical_pressure_adjustment(self):
        """Critical pressure adjustments."""
        self.governor.set_performance()
        self.controller.critical_cpu_throttle()
        self.io.set_weight("best-effort", 10)
        self.memory.set_limit("best-effort", "high", "20%")
        self.io.set_limit("best-effort", "max", "1000")
        self.cpu.set_weight("critical", 500)
        self.memory.protect("critical", "min", "4G")

    def get_app_priority(self, app_id: str = "", app_name: str = "") -> int:
        """Get the priority of an application."""
        resp = requests.post(
            f"{self.balance_url}/{self.config.balance_service['get_priority']}",
            json={"app_id": app_id, "app_name": app_name},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["data"].get("priority", 0)
        else:
            logger.error("Failed to get app priority: %s", resp.text)
            return 0
