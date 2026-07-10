# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
from typing import Dict, List

from utils.logger import logger

class CgroupMonitor:
    def __init__(self, mount_point: str = "/sys/fs/cgroup"):
        self.mount_point = mount_point
        self.cpuacct_path = os.path.join(mount_point, "cpu,cpuacct")
        self.memory_path = os.path.join(mount_point, "memory")
        self.io_path = os.path.join(mount_point, "blkio")
        self.proc_path = "/proc"

    def get_all_pids(self) -> List[int]:
        """Return a list of all running process PIDs on the system."""
        try:
            return [int(pid) for pid in os.listdir("/proc") if pid.isdigit()]
        except (PermissionError, FileNotFoundError) as e:
            logger.error(f"Failed to get pids: {e}")
            return []

    def get_process_info(self, pid: int) -> Dict[str, str]:
        """Return detailed info for the given process PID."""
        info = {}
        try:
            # Read /proc/[pid]/status
            with open(f"{self.proc_path}/{pid}/status") as f:
                for line in f:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        info[key.strip()] = value.strip()

            # Read process command line
            with open(f"{self.proc_path}/{pid}/cmdline") as f:
                cmdline = f.read().replace('\x00', ' ').strip()
                info['Cmdline'] = cmdline

            # Read process stat
            with open(f"{self.proc_path}/{pid}/stat") as f:
                stat = f.read().split()
                info['State'] = stat[2]  # process state
                info['PPid'] = stat[3]  # parent PID

            return info
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to get process {pid} info: {e}")
            return {}

    def get_group_stats(self, cgroup: str) -> Dict[str, Dict]:
        """Return aggregate CPU, memory, IO, and PID stats for the given cgroup."""
        stats = {
            'cpu': self.get_cpu_stats(cgroup),
            'memory': self._get_memory_stats(cgroup),
            'io': self._get_io_stats(cgroup),
            'pids': len(self._get_cgroup_pids(cgroup))
        }
        return stats

    def _get_memory_stats(self, cgroup: str) -> Dict[str, int]:
        path = os.path.join(self.memory_path, cgroup)
        logger.debug(f"cgroup _get_memory_stats path = {path}")
        stats = {
            'usage': 0,
            'limit': (1 << 64),  # unlimited by default
            'oom_kills': 0
        }

        # Current usage
        try:
            with open(os.path.join(path, "memory.current")) as f:  # cgroup v2
                stats['usage'] = int(f.read())
        except FileNotFoundError:
            try:
                with open(os.path.join(path, "memory.usage_in_bytes")) as f:  # cgroup v1 fallback
                    stats['usage'] = int(f.read())
            except FileNotFoundError:
                pass

        # Memory limit
        try:
            with open(os.path.join(path, "memory.max")) as f:  # cgroup v2 preferred
                raw = f.read().strip()
                stats['limit'] = (1 << 64) if raw == "max" else int(raw)
        except FileNotFoundError:
            try:
                with open(os.path.join(path, "memory.limit_in_bytes")) as f:  # cgroup v1 fallback
                    stats['limit'] = int(f.read())
            except FileNotFoundError:
                pass

        # OOM events
        for event_file in ["memory.events", "memory.oom_control"]:  # v2 and v1 respectively
            try:
                with open(os.path.join(path, event_file)) as f:
                    for line in f:
                        if 'oom_kill' in line or 'oom_kill_disable' in line:
                            stats['oom_kills'] += int(line.split()[1])
                break
            except FileNotFoundError:
                continue

        return stats

    def _get_io_stats(self, cgroup: str) -> Dict[str, int]:
        path = os.path.join(self.io_path, cgroup)
        stats = {'bps': 0, 'iops': 0}

        # cgroup v2 preferred (io.stat)
        try:
            with open(os.path.join(path, "io.stat")) as f:
                for line in f:
                    if 'rbps=' in line:
                        stats['bps'] += int(line.split('rbps=')[1].split()[0])
                    if 'wbps=' in line:
                        stats['bps'] += int(line.split('wbps=')[1].split()[0])
        except FileNotFoundError:
            pass  # cgroup v1 IO stats require extra mounts; skip

        return stats

    def _get_cgroup_pids(self, cgroup: str) -> List[int]:
        """Return all PIDs inside the given cgroup."""
        try:
            cgroup_path = os.path.join(self.mount_point, cgroup)
            procs_path = os.path.join(cgroup_path, "cgroup.procs")

            logger.debug(f"cgroup _get_cgroup_pids cgroup_path = {cgroup_path}, procs_path = {procs_path}")

            with open(procs_path) as f:
                return [int(pid) for pid in f.read().split() if pid.strip()]
        except (FileNotFoundError, ValueError) as e:
            logger.error(f"Failed to get pids for {cgroup}: {e}")
            return []

    def get_cpu_stats(self, cgroup: str) -> Dict:
        path = os.path.join(self.mount_point, cgroup, "cpu.stat")
        logger.debug(f"cgroup get_cpu_stats path = {path}")
        stats = {}
        try:
            with open(path) as f:
                for line in f:
                    key, value = line.strip().split()
                    stats[key] = int(value)
        except FileNotFoundError:
            pass
        return stats

    def get_memory_usage(self, cgroup: str) -> int:
        path = os.path.join(self.mount_point, cgroup, "memory.current")
        logger.debug(f"cgroup get_memory_usage path = {path}")
        try:
            with open(path) as f:
                return int(f.read())
        except FileNotFoundError:
            return 0
