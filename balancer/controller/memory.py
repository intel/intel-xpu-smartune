# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
from controller.base import ControllerBase
import subprocess

from utils.logger import logger
from utils.app_utils import write_cgroup_file
from config.config import b_config

# Reserved
class MemoryController(ControllerBase):
    def __init__(self, cgroup_mount: str):
        super().__init__(cgroup_mount)
        self.set_managed_oom_pressure()

    def controller_type(self) -> str:
        return "memory"

    def set_managed_oom_pressure(self, user_service: str = "user@1000.service", oom_pressure: str = "auto") -> bool:
        """Control of systemd OOM will take over in the Balancer, so set the default value from kill to auto"""
        try:
            # smartune runs as root, so call systemctl directly (no sudo prefix).
            cmd = [
                'systemctl', 'set-property', '--runtime',
                user_service, f'ManagedOOMMemoryPressure={oom_pressure}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
            logger.info(f"Failed to set ManagedOOMMemoryPressure: {result.stderr.strip()}")
            return False
        except Exception:
            logger.error("Exception occurred while setting ManagedOOMMemoryPressure")
            return False

    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        try:
            path = os.path.join(self.get_full_path(cgroup), param)
            logger.debug(f"mem set_parameter path = {path}")
            write_cgroup_file(value, path)
            return True
        except OSError as e:
            logger.error(f"Failed to set {param}={value}: {e}")
            return False

    # Memory-specific methods
    def set_limit(self, cgroup: str, limit_bytes: int) -> bool:
        """Set the hard memory limit (triggers OOM killer when exceeded)."""
        return self.set_parameter(cgroup, "memory.limit_in_bytes", str(limit_bytes))

    def protect(self, cgroup: str, min_bytes: int) -> bool:
        """Set the memory protection floor (prevents reclamation below this value)."""
        return self.set_parameter(cgroup, "memory.min", str(min_bytes))

    def get_oom_status(self, cgroup: str) -> bool:
        """Return True if an OOM event has been triggered for this cgroup."""
        status = self.get_parameter(cgroup, "memory.oom_control")
        return "under_oom 1" in status if status else False
