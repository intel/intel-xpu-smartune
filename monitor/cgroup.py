import os
from typing import Dict

class CgroupMonitor:
    def __init__(self, mount_point: str = "/sys/fs/cgroup"):
        self.mount_point = mount_point

    def get_cpu_stats(self, cgroup: str) -> Dict:
        path = os.path.join(self.mount_point, cgroup, "cpu.stat")
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
        try:
            with open(path) as f:
                return int(f.read())
        except FileNotFoundError:
            return 0
