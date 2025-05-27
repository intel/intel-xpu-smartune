import time
from typing import Dict
from dataclasses import dataclass

from monitor.psi import PSIMonitor
from monitor.cgroup import CgroupMonitor
from monitor.pressure import PressureAnalyzer

from controller.controller import Controller
from controller.io import IOController
from controller.cpu import CPUController
from controller.memory import MemoryController
from controller.governor import GovernorController

@dataclass
class WorkloadGroup:
    name: str
    priority: int
    cpu_weight: int
    memory_min: int = 0
    io_weight: int = 100

class DynamicBalancer:
    def __init__(self, logging, config):
        self.logging = logging
        self.config = config
        self.psi = PSIMonitor()
        self.cgroups = CgroupMonitor(config.cgroup_mount)
        self.analyzer = PressureAnalyzer(config)
        self.controller = Controller(config.cgroup_mount)
        self.cpu = CPUController(config.cgroup_mount)
        self.memory = MemoryController(config.cgroup_mount)
        self.io = IOController(config.cgroup_mount)
        self.governor = GovernorController(logging, config.cgroup_mount)

        # Define workload groups
        self.workloads = [
            WorkloadGroup("critical", 100, 300, 2<<30, 500),
            WorkloadGroup("high", 80, 200, 1<<30, 300),
            WorkloadGroup("normal", 50, 100, 0, 200),
            WorkloadGroup("best-effort", 20, 50, 0, 100)
        ]

    def balance(self):
        """Main balancing loop"""
        while True:
            # Collect metrics
            psi_data = self.psi.get_current_pressure()
            score = self.analyzer.calculate_pressure_score(psi_data)
            level = self.analyzer.get_pressure_level(score)
            # print("psi data: ", psi_data, ", score: ", score, ", level: ", level)
            self.logging.info("psi data: %s, score: %f, level: %s", psi_data, score, level)

            # Adjust resources based on pressure
            self.adjust_resources(level)

            # Sleep until next interval
            time.sleep(self.config.psi_interval)

    def adjust_resources(self, pressure_level: str):
        """Adjust resources based on pressure level"""
        adjustments = {
            'low': self._low_pressure_adjustment,
            'medium': self._medium_pressure_adjustment,
            'high': self._high_pressure_adjustment,
            'critical': self._critical_pressure_adjustment
        }
        adjustments.get(pressure_level, lambda: None)()

    def _low_pressure_adjustment(self):
        """Low pressure adjustments"""
        self.governor.set_powersave()

        # release CPU/MEM throttling
        self.controller.restore_cpu_throttle()


    def _medium_pressure_adjustment(self):
        """Medium pressure adjustments"""
        self.governor.set_powersave()

        # release CPU/MEM throttling
        self.controller.restore_cpu_throttle()

    def _high_pressure_adjustment(self):
        """High pressure adjustments"""
        self.governor.set_performance()

        # throttle CPU/MEM
        self.controller.high_cpu_throttle()

    def _critical_pressure_adjustment(self):
        """Critical pressure adjustments"""
        self.governor.set_performance()

        # throttle CPU/MEM
        self.controller.critical_cpu_throttle()

        # Throttle best-effort workloads heavily
        self.cpu.set_weight("best-effort", 10)
        self.memory.set_limit("best-effort", "high", "20%")
        self.io.set_limit("best-effort", "max", "1000")

        # Boost critical workloads
        self.cpu.set_weight("critical", 500)
        self.memory.protect("critical", "min", "4G")
