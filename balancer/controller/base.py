# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
import os
from utils.logger import get_logger
logger = get_logger(__name__)
from typing import Optional, List

class ControllerBase(ABC):
    def __init__(self, cgroup_mount: str):
        """
        :param cgroup_mount: cgroup mount point path (e.g. "/sys/fs/cgroup")
        """
        self.cgroup_mount = cgroup_mount

    @abstractmethod
    def controller_type(self) -> str:
        """Return the controller type string (e.g. 'cpu', 'memory')."""
        pass

    def get_full_path(self, cgroup: str) -> str:
        """Return the absolute cgroup filesystem path for the given cgroup."""
        return os.path.join(self.cgroup_mount, self.controller_type(), cgroup.lstrip('/'))

    def exists(self, cgroup: str) -> bool:
        """Return True if the cgroup directory exists."""
        return os.path.exists(self.get_full_path(cgroup))

    def get_tasks(self, cgroup: str) -> Optional[List[int]]:
        """Return the list of PIDs in the cgroup, or None on error."""
        try:
            with open(os.path.join(self.get_full_path(cgroup), 'cgroup.procs'), 'r') as f:
                return [int(line.strip()) for line in f if line.strip()]
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to get tasks: {e}")
            return None

    @abstractmethod
    def set_parameter(self, cgroup: str, param: str, value: str) -> bool:
        """Set a controller-specific cgroup parameter."""
        pass

    def get_parameter(self, cgroup: str, param: str) -> Optional[str]:
        """Read and return a controller parameter, or None on error."""
        try:
            with open(os.path.join(self.get_full_path(cgroup), param), 'r') as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to get parameter {param}: {e}")
            return None
