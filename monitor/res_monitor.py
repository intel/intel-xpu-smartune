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
import re
import subprocess
import time
from collections import defaultdict
from time import sleep

import psutil
from config.config import b_config
from utils.app_utils import fetch_all_apps
from utils.logger import logger

from monitor.psi import PSIMonitor


class ResourceMonitor:
    def __init__(self):
        """初始化资源监视器"""
        self.config = b_config
        self.cpu_cores = os.cpu_count() or 16
        self.prev_io = psutil.disk_io_counters()
        self.prev_time = time.time()
        # 桌面应用信息
        try:
            self.desktop_apps = {app["app_id"]: app for app in fetch_all_apps()}
            logger.info(f"Loaded {len(self.desktop_apps)} desktop applications")
        except Exception as e:
            logger.warning(f"Could not load desktop apps: {str(e)}")
            self.desktop_apps = {}

    def _get_top_processes(self, n=1, samples=5, interval=1.0):
        """获取资源占用最高的应用（基于 cgroup 聚合）"""
        # Step 1: 采样获取候选进程（考虑权重和进程组）
        psi_data = PSIMonitor().get_current_pressure()
        dynamic_weights = self._adjust_weights_by_pressure(psi_data)

        candidate_procs = self._get_candidate_processes(
            num=3,  # 候选，可能占用资源最高的app
            samples=samples,
            interval=interval,
            dynamic_weights=dynamic_weights
        )

        # logger.debug(f"Candidate processes for cgroup aggregation: {candidate_procs}")
        # Step 2: 收集所有不重复的cgroup_path
        cgroup_paths = set()
        for proc in candidate_procs:
            cgroup_path = self._get_process_cgroup(proc['pid'])
            if cgroup_path:
                cgroup_paths.add(cgroup_path)

        # Step 3: 按 cgroup 聚合进程（修改字段名，去掉 avg 逻辑）
        cgroup_data = defaultdict(lambda: {
            'cpu_total': 0,  # 所有进程 CPU 使用率总和（%）
            'mem_percent_total': 0,  # 所有进程内存占用百分比总和（%）
            'mem_rss_total': 0,  # 所有进程 RSS 内存总和（字节）
            'io_read_total': 0,  # 所有进程 IO 读取总和（字节）
            'count': 0,
            'pids': set(),
            'names': set(),  # 修复字段名（原为 'name'）
            'cmdlines': set()  # 修复字段名（原为 'cmdline'）
        })

        # 缓存每个 cgroup 的 pid 列表和 Process 对象
        cgroup_pids = {}
        pid_process_map = {}

        # 第一次遍历：初始化 CPU 计时器，并缓存数据
        for cgroup_path in cgroup_paths:
            pids_in_cgroup = self._get_pids_in_cgroup(cgroup_path)
            cgroup_pids[cgroup_path] = pids_in_cgroup
            for pid in pids_in_cgroup:
                try:
                    p = psutil.Process(pid)
                    p.cpu_percent(interval=None)  # 初始化计时器
                    pid_process_map[pid] = p  # 缓存 Process 对象
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        time.sleep(0.1)  # 全局等待 100ms

        # 第二次遍历：直接使用缓存的数据
        for cgroup_path, pids_in_cgroup in cgroup_pids.items():
            for pid in pids_in_cgroup:
                if pid not in pid_process_map:
                    continue
                p = pid_process_map[pid]
                try:
                    with p.oneshot():
                        cpu_percent = p.cpu_percent(interval=None)
                        mem_info = p.memory_info()
                        mem_percent = p.memory_percent()
                        io_counters = p.io_counters() if p.io_counters() else None

                    cgroup_data[cgroup_path]['cpu_total'] += cpu_percent
                    cgroup_data[cgroup_path]['mem_percent_total'] += mem_percent
                    cgroup_data[cgroup_path]['mem_rss_total'] += mem_info.rss
                    if io_counters:
                        cgroup_data[cgroup_path]['io_read_total'] += io_counters.read_bytes
                    cgroup_data[cgroup_path]['count'] += 1
                    cgroup_data[cgroup_path]['pids'].add(pid)
                    cgroup_data[cgroup_path]['names'].add(p.name())
                    cgroup_data[cgroup_path]['cmdlines'].add(' '.join(p.cmdline()) or p.name())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        # Step 4: 计算总分（CPU 按核心数归一化，其他用总量）
        processes = []
        for cgroup_path, data in cgroup_data.items():
            if data['count'] > 0:
                # CPU 使用率总和按核心数归一化（避免超过 100%）
                cpu_total_normalized = data['cpu_total'] / self.cpu_cores
                # 内存 RSS 转换为 GB
                mem_rss_total_gb = data['mem_rss_total'] / (1024 ** 3)
                # IO 读取速率转换为 MB/s
                io_read_rate_mb = data['io_read_total'] / (samples * interval) / (1024 ** 2)

                score = (
                        dynamic_weights['cpu'] * min(cpu_total_normalized, 100) +
                        dynamic_weights['memory'] * min(data['mem_percent_total'], 100) +
                        dynamic_weights['io'] * min(io_read_rate_mb, 100)
                )

                processes.append({
                    'pids': list(data['pids']),
                    'cgroup': cgroup_path,
                    'score': round(score, 2),
                    'cpu_avg': round(cpu_total_normalized, 1),  # 归一化后的 CPU
                    'mem_avg': round(data['mem_percent_total'], 1),
                    'mem_rss': round(mem_rss_total_gb, 2),  # 单位：GB
                    'io_read_rate': round(io_read_rate_mb, 2),  # 单位：MB/s
                    'names': list(data['names']),
                    'cmdlines': list(data['cmdlines'])
                })

        # logger.debug(f"Aggregated processes by cgroup: {processes}")
        # Step 5: 返回评分最高的进程信息列表
        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

    def _get_candidate_processes(self, num, samples, interval, dynamic_weights):
        """采样获取候选进程（计算score筛选top进程，但只返回pid和name）"""
        candidates = []
        seen_pids = set()  # 用于记录已经处理过的PID

        for _ in range(samples):
            current_sample = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent',
                                             'memory_info', 'memory_percent', 'io_counters',
                                             'create_time']):
                try:
                    info = proc.info
                    pid = info['pid']

                    # 跳过已处理过的PID或黑名单进程
                    if (pid in seen_pids or
                            any(b in info.get('name', '') for b in self.config.blacklist) or
                            time.time() - info['create_time'] < 2):
                        continue

                    seen_pids.add(pid)  # 标记为已处理

                    # 计算带权重的实时评分（但不需要返回这些数据）
                    io_read = info['io_counters'].read_bytes if info['io_counters'] else 0
                    score = (
                            dynamic_weights['cpu'] * min(info['cpu_percent'], 100) +
                            dynamic_weights['memory'] * min(info['memory_percent'], 100) +
                            dynamic_weights['io'] * min(io_read / 1024 / 1024, 10)
                    )

                    current_sample.append({
                        'pid': pid,
                        'name': info['name'],
                        'score': score  # 仅用于排序
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # 按score降序排序，取前num个进程
            current_sample_sorted = sorted(current_sample, key=lambda x: -x['score'])[:num]
            candidates.extend(current_sample_sorted)
            if _ != samples - 1:
                time.sleep(interval)

        return [{'pid': p['pid'], 'name': p['name']} for p in candidates]

    def _get_process_cgroup(self, pid):
        """获取进程的 cgroup 路径（从 /proc/<pid>/cgroup 解析）"""
        try:
            with open(f'/proc/{pid}/cgroup', 'r') as f:
                for line in f:
                    if line.strip():
                        _, _, cgroup_path = line.strip().split(':', 2)
                        if cgroup_path and cgroup_path != '/':
                            return cgroup_path
        except (FileNotFoundError, PermissionError, ValueError):
            pass
        return None

    def _get_pids_in_cgroup(self, cgroup_path):
        """获取指定cgroup下的所有进程PID"""
        try:
            result = subprocess.run(
                ["systemd-cgls", "--no-page", cgroup_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout

            if not output:
                logger.debug(f"No output from systemd-cgls for cgroup {cgroup_path}")
                return []

            pids = re.findall(r"[├└]─(\d+)\s+.+", output)
            filtered_pids = []
            for pid in map(int, pids):
                try:
                    cmdline = psutil.Process(pid).cmdline()
                    if cmdline and cmdline[0] == "bash":
                        continue  # Skip processes with cmdline "bash"
                    filtered_pids.append(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            return filtered_pids

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout while getting PIDs for cgroup {cgroup_path}")
            return []
        except Exception as e:
            logger.error(f"Error getting PIDs for cgroup {cgroup_path}: {str(e)}")
            return []

    def _adjust_weights_by_pressure(self, psi_data):
        """根据PSI压力动态调整权重"""
        base_weights = self.config.weights_top
        return {
            'cpu': base_weights['cpu'] * (1 + psi_data.get('cpu', 0)),
            'memory': base_weights['memory'] * (1 + psi_data.get('memory', 0)),
            'io': base_weights['io']  # 保留但不再用于进程评分
        }

    def _find_systemd_unit(self, pid):
        """通过systemd-cgls查找进程所属的scope, service"""
        try:
            result = subprocess.run(
                ['systemd-cgls', '--no-page'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            # 查找包含指定PID的行及其父unit
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if f'─{pid} ' in line or f'─{pid}\n' in line:
                    # 向上查找最近的unit(scope或service)
                    for j in range(i, -1, -1):
                        line_content = lines[j]
                        if '.scope' in line_content or '.service' in line_content:
                            # 匹配类似 "├─session-c20.scope" 或 "├─fileManage.service"
                            unit_match = re.search(r"─(.*?\.(?:scope|service))", line_content)
                            if unit_match:
                                return unit_match.group(1)

                            # 如果没有匹配到，尝试更宽松的匹配
                            unit_match = re.search(r"\b([\w-]+\.(?:scope|service))\b", line_content)
                            if unit_match:
                                return unit_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to find systemd unit: {str(e)}")
        return None

    def try_match_app(self, process_info):
        """尝试匹配桌面应用或systemd scope"""
        # logger.debug(f"try_match_app process_info: {process_info}")
        # 1. 先判断process_info中是否有cgroup字段，如果有直接拿出来按照正则表达式获取到名字
        cgroup = process_info.get('cgroup')
        if cgroup:
            scope_name = cgroup.rstrip('/').split('/')[-1]
            logger.debug(f"Extracted scope name from cgroup: {scope_name}")
            return {
                'type': 'cgroup',
                'id': scope_name,
                'name': f"CGroup: {scope_name}"
            }

        # 2. 尝试通过systemd-cgls查找scope或者service
        if 'pids' in process_info and process_info['pids']:
            unit = self._find_systemd_unit(process_info['pids'][0])  # the first PID
            if unit:
                logger.debug(f"try_match_app Matched systemd unit: {unit}")
                return {
                    'type': 'systemd',
                    'id': unit,
                    'name': f"Systemd cgroup: {unit}"
                }

        # 3. 尝试匹配桌面应用，最终还需要拿到cgroup路径
        if self.desktop_apps:
            for app_id, app in self.desktop_apps.items():
                try:
                    # 检查应用的可执行文件是否匹配
                    cmd = app["cmdline"]
                    if cmd and process_info['exe'] and process_info['exe'] in cmd:
                        logger.debug(f"try_match_app Matched desktop app by cmdline: {app_id}")
                        return {
                            'type': 'desktop',
                            'id': app_id,
                            'name': app["display_name"]
                        }

                    # 检查应用名称是否匹配进程名
                    if app["name"].lower() in process_info['name'].lower():
                        logger.debug(f"try_match_app Matched desktop app by name: {app_id}")
                        return {
                            'type': 'desktop',
                            'id': app_id,
                            'name': app["display_name"]
                        }
                except Exception as e:
                    logger.warning(f"Catch error: {e}")
                    continue

        return None

    def get_top_resource_consumers(self):
        """获取资源占用最高的1个进程及其应用信息"""
        results = []
        reach_threshold = True
        processes = self._get_top_processes(n=1)
        logger.debug(f"Top processes: {processes}")

        # Return empty list if top process doesn't meet minimum resource thresholds
        # Since the minimum thresholds is 30% for uncontrolled apps, larger for controlled apps,
        # we use 25% here to avoid false negatives.
        if processes and (processes[0]['cpu_avg'] < 25  # if CPU usage < 25% per core
                          and processes[0]['mem_rss'] < psutil.virtual_memory().total * 0.25):  # 25% of total memory
            logger.info(f"Top process - {next(iter(processes[0]['names']), "unknown")} corresponding "
                        f"app does not meet minimum resource thresholds")
            reach_threshold = False

        for process in processes:
            process_name = next(iter(process['names']), "unknown")
            process_cmdline = next(iter(process['cmdlines']), "unknown")

            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': next(iter(process['pids']), None),  # Use the first PID from 'pids'
                    'name': process_name,
                    'cmdline': process_cmdline,
                    'score': round(process['score'], 3),
                    'cpu_avg': process['cpu_avg'],
                    'mem_rss': process['mem_rss'],
                    'io_read_rate': process['io_read_rate']
                },
                'app': app_info
            })

        return results, reach_threshold

    def get_total_memory(self):
        """获取系统物理内存总大小（单位：MB）"""
        mem = psutil.virtual_memory()
        total_memory_mb = round(mem.total / (1024 ** 2), 2)  # 转换为MB并保留2位小数
        return total_memory_mb

    def disk_utilization(self, device='nvme0n1', interval=1):
        """获取单个磁盘的利用率（%）"""
        cnt1 = psutil.disk_io_counters(perdisk=True).get(device)
        time1 = time.time()
        time.sleep(interval)
        cnt2 = psutil.disk_io_counters(perdisk=True).get(device)
        time2 = time.time()
        if not cnt1 or not cnt2:
            return 0.0
        delta_time = (time2 - time1) * 1000  # 毫秒
        busy_time = (cnt2.read_time - cnt1.read_time) + (cnt2.write_time - cnt1.write_time)
        return min(100.0, 100 * busy_time / delta_time)

    def get_physical_disks(self):
        """获取所有物理磁盘设备名"""
        cmd = "lsblk -d -o NAME,TYPE -n | awk '$2 == \"disk\" {print $1}'"
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        return output.splitlines() if output else []

    def get_resource_usage(self) -> dict:
        """获取系统整体资源使用率和剩余容量"""
        # CPU：核心数、使用率（%）
        cpu_count = psutil.cpu_count(logical=True)
        cpu_usage = psutil.cpu_percent(interval=0.5)  # 0.5秒采样
        cpu_available = 100 - cpu_usage  # 剩余CPU百分比

        # 内存：总容量（GB）、使用率（%）、剩余容量占比
        mem = psutil.virtual_memory()
        mem_total_gb = round(mem.total / (1024 **3), 2)
        mem_usage = mem.percent
        mem_available_ratio = round(mem.available / mem.total, 2)  # 剩余内存占比

        return {
            'cpu': {
                'count': cpu_count,
                'usage': cpu_usage,
                'available': cpu_available,
                'is_busy': cpu_usage > self.config.cpu_busy_threshold  # 暂定整体使用率 >90% 算busy
            },
            'memory': {
                'total_gb': mem_total_gb,
                'usage': mem_usage,  # %
                'available_ratio': mem_available_ratio,
                'is_busy': mem_usage > self.config.memory_busy_threshold
            }
        }

    def get_disk_io_usage(self) -> dict:
        # 获取所有物理磁盘的利用率
        disks = self.get_physical_disks()
        disk_utils = {}
        for disk in disks:
            disk_utils[disk] = {
                'utilization': round(self.disk_utilization(disk), 2),
                'is_busy': False  # 将在下面判断
            }

        # 判断磁盘是否繁忙（基于利用率）
        for disk in disk_utils:
            disk_utils[disk]['is_busy'] = disk_utils[disk]['utilization'] > self.config.disk_utilization_threshold

        return {'disk_io': disk_utils}

    def get_disk_io_speed(self) -> dict:
        """获取所有物理磁盘的读写速度（KB/s），基于 prev_time/prev_io 模式"""
        disks = self.get_physical_disks()
        curr_io = psutil.disk_io_counters(perdisk=True)
        curr_time = time.time()
        time_elapsed = curr_time - self.prev_time

        result = {}
        for disk in disks:
            # 初始化 prev_io 如果不存在
            if not hasattr(self, 'prev_io') or disk not in self.prev_io:
                self.prev_io = curr_io
                self.prev_time = curr_time
                result[disk] = {'read_kb_per_sec': 0.0, 'write_kb_per_sec': 0.0}
                continue

            # 计算读写速度
            if time_elapsed > 0:
                read_kb = (curr_io[disk].read_bytes - self.prev_io[disk].read_bytes) / 1024
                write_kb = (curr_io[disk].write_bytes - self.prev_io[disk].write_bytes) / 1024
                read_kb_per_sec = read_kb / time_elapsed
                write_kb_per_sec = write_kb / time_elapsed
            else:
                read_kb_per_sec = write_kb_per_sec = 0.0

            result[disk] = {
                'read_kb_per_sec': round(read_kb_per_sec, 2),
                'write_kb_per_sec': round(write_kb_per_sec, 2)
            }

        # 更新状态
        self.prev_io = curr_io
        self.prev_time = curr_time

        # logger.debug(f"Disk IO speeds: {result}")
        return {'disk_io': result}

    def get_disk_stats(self) -> dict:
        """
        所有磁盘IO利用率和读写速度的统计结果
        返回格式:
        {
            "disk_io": {
                "nvme0n1": {
                    "utilization": 45.2,
                    "is_busy": True,
                    "read_kb_per_sec": 1024 kB/s,
                    "write_kb_per_sec": 512 kB/s,
                },
                ...
            }
        }
        """
        usage_data = self.get_disk_io_usage()['disk_io']
        speed_data = self.get_disk_io_speed()['disk_io']

        # 合并结果
        merged_result = {}
        for disk in usage_data.keys():
            merged_result[disk] = {
                **usage_data[disk],  # 包含 utilization 和 is_busy
                **speed_data.get(disk, {})  # 包含 read/write_kb_per_sec
            }

        return {'disk_io': merged_result}

def main():
    """调试用主函数"""
    logger.info("==== Starting Resource Monitor ====")

    monitor = ResourceMonitor()

    try:
        while True:
            results = monitor.get_top_resource_consumers()
            for i, result in enumerate(results, 1):
                logger.debug(f"\n=== Top Resource Consumer #{i} ===")
                logger.debug(f"Process: {result['process']['name']} (PID: {result['process']['pid']})")
                logger.debug(f"CPU: {result['process']['cpu']}% | Memory: {result['process']['memory_mb']}MB")
                logger.debug(f"Score: {result['process']['score']:.2f}")
                logger.debug(f"Cmd: {result['process']['cmdline'][:100]}...")

                if result['app']:
                    logger.debug(f"\nMatched to: {result['app']['name']} ({result['app']['type']})")
                    logger.debug(f"ID: {result['app']['id']}")
                else:
                    logger.debug("\nNo matching application found")

            sleep(5)

    except KeyboardInterrupt:
        logger.info("\nMonitoring stopped by user")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    main()