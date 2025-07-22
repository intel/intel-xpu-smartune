import os
from typing import Dict
from typing import Dict, List, Optional, Tuple
import re

class CgroupMonitor:
    def __init__(self, mount_point: str = "/sys/fs/cgroup"):
        self.mount_point = mount_point
        self.cpuacct_path = os.path.join(mount_point, "cpu,cpuacct")
        self.memory_path = os.path.join(mount_point, "memory")
        self.io_path = os.path.join(mount_point, "blkio")
        self.proc_path = "/proc"

    def get_all_pids(self) -> List[int]:
        """获取系统中所有运行中的进程PID列表"""
        try:
            return [int(pid) for pid in os.listdir("/proc") if pid.isdigit()]
        except (PermissionError, FileNotFoundError) as e:
            print(f"Failed to get pids: {e}")
            return []

    def get_process_info(self, pid: int) -> Dict[str, str]:
        """获取进程详细信息"""
        info = {}
        try:
            # 读取/proc/[pid]/status
            with open(f"{self.proc_path}/{pid}/status") as f:
                for line in f:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        info[key.strip()] = value.strip()

            # 读取进程命令行
            with open(f"{self.proc_path}/{pid}/cmdline") as f:
                cmdline = f.read().replace('\x00', ' ').strip()
                info['Cmdline'] = cmdline

            # 读取进程状态
            with open(f"{self.proc_path}/{pid}/stat") as f:
                stat = f.read().split()
                info['State'] = stat[2]  # 进程状态
                info['PPid'] = stat[3]  # 父进程PID

            return info
        except (FileNotFoundError, PermissionError) as e:
            print(f"Failed to get process {pid} info: {e}")
            return {}

    def get_group_stats(self, cgroup: str) -> Dict[str, Dict]:
        """获取cgroup的综合统计信息"""
        stats = {
            'cpu': self.get_cpu_stats(cgroup),
            'memory': self._get_memory_stats(cgroup),
            'io': self._get_io_stats(cgroup),
            'pids': len(self._get_cgroup_pids(cgroup))
        }
        return stats

    def _get_memory_stats(self, cgroup: str) -> Dict[str, int]:
        path = os.path.join(self.memory_path, cgroup)
        print(f"cgroup _get_memory_stats path = {path}")
        stats = {
            'usage': 0,
            'limit': (1 << 64),  # 默认无限制
            'oom_kills': 0
        }

        # 当前用量
        try:
            with open(os.path.join(path, "memory.current")) as f:  # v2 字段
                stats['usage'] = int(f.read())
        except FileNotFoundError:
            try:
                with open(os.path.join(path, "memory.usage_in_bytes")) as f:  # v1 回退
                    stats['usage'] = int(f.read())
            except FileNotFoundError:
                pass

        # 内存限制
        try:
            with open(os.path.join(path, "memory.max")) as f:  # v2 优先
                raw = f.read().strip()
                stats['limit'] = (1 << 64) if raw == "max" else int(raw)
        except FileNotFoundError:
            try:
                with open(os.path.join(path, "memory.limit_in_bytes")) as f:  # v1 回退
                    stats['limit'] = int(f.read())
            except FileNotFoundError:
                pass

        # OOM 事件
        for event_file in ["memory.events", "memory.oom_control"]:  # v2 和 v1 分别处理
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

        # v2 优先 (io.stat)
        try:
            with open(os.path.join(path, "io.stat")) as f:
                for line in f:
                    if 'rbps=' in line:
                        stats['bps'] += int(line.split('rbps=')[1].split()[0])
                    if 'wbps=' in line:
                        stats['bps'] += int(line.split('wbps=')[1].split()[0])
        except FileNotFoundError:
            pass  # 忽略 v1 的 IO 统计（通常需要额外挂载）

        return stats

    def _get_cgroup_pids(self, cgroup: str) -> List[int]:
        """获取cgroup内的所有进程PID"""
        try:
            cgroup_path = os.path.join(self.mount_point, cgroup)
            procs_path = os.path.join(cgroup_path, "cgroup.procs")

            print(f"cgroup _get_cgroup_pids cgroup_path = {cgroup_path}, procs_path = {procs_path}")

            with open(procs_path) as f:
                return [int(pid) for pid in f.read().split() if pid.strip()]
        except (FileNotFoundError, ValueError) as e:
            print(f"Failed to get pids for {cgroup}: {e}")
            return []

    def get_cpu_stats(self, cgroup: str) -> Dict:
        path = os.path.join(self.mount_point, cgroup, "cpu.stat")
        print(f"cgroup get_cpu_stats path = {path}")
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
        print(f"cgroup get_memory_usage path = {path}")
        try:
            with open(path) as f:
                return int(f.read())
        except FileNotFoundError:
            return 0
