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
from utils.logger import logger

# Reserved
class IOController(ControllerBase):
    def __init__(self, cgroup_mount: str):
        super().__init__(cgroup_mount)
    def controller_type(self) -> str:
        return "blkio"

    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        try:
            path = os.path.join(self.get_full_path(cgroup), param)
            print(f"io set_parameter path = {path}")
            os.system(f"echo {value} > sudo {path}")
            # with open(os.path.join(self.get_full_path(cgroup), param), 'w') as f:
            #     f.write(value)
            return True
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to set {param}={value}: {e}")
            return False

    def set_weight(self, cgroup: str, weight: int) -> bool:
        """设置IO权重，范围10-1000"""
        weight = max(10, min(weight, 1000))
        print(f"io set_weight weight = {weight}")

    def set_limit(self, name: str, cgroup: str, weight: int) -> bool:
        pass

    def set_throttle(self, cgroup: str, op_type: str, bps: int) -> bool:
        """
        设置IO速率限制
        :param op_type: 'read' 或 'write'
        :param bps: 字节/秒
        """
        device = "8:0"  # 默认主设备号，实际使用应检测具体设备
        param = f"blkio.throttle.{op_type}_bps_device"
        return self.set_parameter(cgroup, param, f"{device} {bps}")
