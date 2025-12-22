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


import os
from controller.base import ControllerBase
import shutil
from typing import Optional
from utils.logger import logger

# Reserved
class MemoryController(ControllerBase):
    def __init__(self, cgroup_mount: str):
        super().__init__(cgroup_mount)
    def controller_type(self) -> str:
        return "memory"

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

