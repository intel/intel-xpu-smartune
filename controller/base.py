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


from abc import ABC, abstractmethod
import os
import shutil
from utils.logger import logger
from typing import Optional, List

class ControllerBase(ABC):
    def __init__(self, cgroup_mount: str):
        """
        :param cgroup_mount: cgroup挂载点路径 (e.g. "/sys/fs/cgroup")
        """
        self.cgroup_mount = cgroup_mount

    @abstractmethod
    def controller_type(self) -> str:
        """返回控制器类型 (e.g. 'cpu', 'memory')"""
        pass

    def get_full_path(self, cgroup: str) -> str:
        """获取cgroup的完整路径"""
        return os.path.join(self.cgroup_mount, self.controller_type(), cgroup.lstrip('/'))

    def exists(self, cgroup: str) -> bool:
        """检查cgroup是否存在"""
        return os.path.exists(self.get_full_path(cgroup))

    def get_tasks(self, cgroup: str) -> Optional[List[int]]:
        """获取cgroup内的所有进程PID"""
        try:
            with open(os.path.join(self.get_full_path(cgroup), 'cgroup.procs'), 'r') as f:
                return [int(line.strip()) for line in f if line.strip()]
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to get tasks: {e}")
            return None

    @abstractmethod
    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        """设置控制器特定参数"""
        pass

    def get_parameter(self, cgroup: str, param: str) -> Optional[str]:
        """读取控制器参数"""
        try:
            with open(os.path.join(self.get_full_path(cgroup), param), 'r') as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to get parameter {param}: {e}")
            return None
