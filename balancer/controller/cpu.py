# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
from controller.base import ControllerBase
from utils.logger import logger
import shutil

# Reserved
class CPUController(ControllerBase):
    def __init__(self, cgroup_mount: str):
        super().__init__(cgroup_mount)

    def controller_type(self) -> str:
        return "cpu"

    def set_weight(self, cgroup: str, weight: int) -> bool:
        """Set CPU weight for a cgroup"""
        path = os.path.join(self.cgroup_mount, cgroup, "cpu.weight")
        print(f"cpu set_weight path = {path}")
        try:
            with open(path, 'w') as f:
                f.write(str(weight))
            return True
        except (FileNotFoundError, PermissionError):
            return False

    def set_affinity(self, cgroup: str, cpus: str) -> bool:
        """Set CPU affinity for a cgroup"""
        path = os.path.join(self.cgroup_mount, cgroup, "cpuset.cpus")
        print(f"cpu set_affinity path = {path}")
        try:
            with open(path, 'w') as f:
                f.write(cpus)
            return True
        except (FileNotFoundError, PermissionError):
            return False

    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        try:
            path = os.path.join(self.get_full_path(cgroup), param)
            print(f"cpu set_parameter path = {path}")
            os.system(f"echo {value} > sudo {path}")
            # with open(os.path.join(self.get_full_path(cgroup), param), 'w') as f:
            #     f.write(value)
            return True
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to set {param}={value}: {e}")
            return False

    # CPU特定方法
    def set_cpu_quota(self, cgroup: str, quota_us: int, period_us: int = 100000) -> bool:
        """设置CPU时间配额"""
        return (self.set_parameter(cgroup, "cpu.cfs_quota_us", str(quota_us)) and
                self.set_parameter(cgroup, "cpu.cfs_period_us", str(period_us)))
