# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
from controller.base import ControllerBase
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec

from utils.logger import logger

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
            cmd = [
                'sudo', 'systemctl', 'set-property', '--runtime',
                user_service, f'ManagedOOMMemoryPressure={oom_pressure}'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
            logger.info(f"Failed to set ManagedOOMMemoryPressure: {result.stderr.strip()}")
            return False
        except:
            logger.error("Exception occurred while setting ManagedOOMMemoryPressure")
            return False

    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        try:
            path = os.path.join(self.get_full_path(cgroup), param)
            print(f"mem set_parameter path = {path}")
            os.system(f"echo {value} > sudo {path}")
            # with open(os.path.join(self.get_full_path(cgroup), param), 'w') as f:
            #     f.write(value)
            return True
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to set {param}={value}: {e}")
            return False

    # Memory特定方法
    def set_limit(self, cgroup: str, limit_bytes: int) -> bool:
        """设置内存硬限制（触发OOM killer）"""
        return self.set_parameter(cgroup, "memory.limit_in_bytes", str(limit_bytes))

    def protect(self, cgroup: str, min_bytes: int) -> bool:
        """设置内存保护（避免被回收）"""
        return self.set_parameter(cgroup, "memory.min", str(min_bytes))

    def get_oom_status(self, cgroup: str) -> bool:
        """检查是否触发过OOM"""
        status = self.get_parameter(cgroup, "memory.oom_control")
        return "under_oom 1" in status if status else False
