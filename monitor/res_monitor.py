#!/usr/bin/env python3
import os
import psutil
import subprocess
import re
from gi.repository import Gio
from collections import defaultdict
import time
from time import sleep
from utils.logger import logger
from monitor.psi import PSIMonitor
from config.config import b_config


class ResourceMonitor:
    def __init__(self):
        """初始化资源监视器"""
        self.config = b_config
        self.cpu_cores = os.cpu_count() or 16

        # 桌面应用信息
        try:
            self.desktop_apps = {app.get_id(): app for app in Gio.AppInfo.get_all()}
            logger.info(f"Loaded {len(self.desktop_apps)} desktop applications")
        except Exception as e:
            logger.warning(f"Could not load desktop apps: {str(e)}")
            self.desktop_apps = {}

    def _get_top_processes(self, n=1, samples=5, interval=1.0):
        """返回按 cmdline 分组聚合后的 TOP 进程数据"""
        cumulative = {}

        for _ in range(samples):
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent',
                                             'memory_info', 'memory_percent', 'io_counters',
                                             'create_time']):
                try:
                    info = proc.info
                    if not info.get('cmdline') or any(b in info.get('name', '') for b in self.config.blacklist):
                        continue
                    if time.time() - info['create_time'] < 2:
                        continue

                    # 关键修改：按 cmdline 分组，而非 pid
                    cmdline = ' '.join(info['cmdline'])
                    if cmdline not in cumulative:
                        cumulative[cmdline] = {
                            'cpu_sum': 0,
                            'mem_percent_sum': 0,
                            'mem_rss_sum': 0,
                            'io_read_sum': 0,
                            'count': 0,
                            'name': info['name'],
                            'cmdline': cmdline,
                            'pids': set()  # 记录关联的 pids
                        }

                    # 累计各指标
                    cumulative[cmdline]['cpu_sum'] += info['cpu_percent']
                    cumulative[cmdline]['mem_percent_sum'] += info['memory_percent']
                    cumulative[cmdline]['mem_rss_sum'] += info['memory_info'].rss
                    if info['io_counters']:
                        cumulative[cmdline]['io_read_sum'] += info['io_counters'].read_bytes
                    cumulative[cmdline]['count'] += 1
                    cumulative[cmdline]['pids'].add(info['pid'])

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if _ != samples - 1:
                time.sleep(interval)

        # 计算平均值
        psi_data = PSIMonitor().get_current_pressure()
        dynamic_weights = self._adjust_weights_by_pressure(psi_data)

        processes = []
        for cmdline, data in cumulative.items():
            if data['count'] > 0:
                # if "stress" in cmdline:
                #     logger.debug(f"data count for {cmdline}: {data['count']}, data = {data}")
                avg_cpu = data['cpu_sum'] / samples
                avg_mem_percent = data['mem_percent_sum'] / samples
                avg_mem_rss = data['mem_rss_sum'] / samples
                io_read_rate = data['io_read_sum'] / (samples * interval)

                score = (
                        dynamic_weights['cpu'] * min(avg_cpu, 100) +
                        dynamic_weights['memory'] * min(avg_mem_percent, 100) +
                        dynamic_weights['io'] * min(io_read_rate / 1024 / 1024, 10)
                )

                processes.append({
                    'pids': list(data['pids']),  # 返回所有关联的 pids
                    'name': data['name'],
                    'cmdline': cmdline,
                    'score': round(score, 2),
                    'cpu_avg': round(avg_cpu, 1),
                    'mem_avg': round(avg_mem_percent, 3),
                    'mem_rss': avg_mem_rss,  # 总物理内存字节数（所有子进程之和）
                    'io_read_rate': io_read_rate
                })

        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

    def _adjust_weights_by_pressure(self, psi_data):
        """根据PSI压力动态调整权重"""
        base_weights = self.config.weights_top
        return {
            'cpu': base_weights['cpu'] * (1 + psi_data.get('cpu', 0)),
            'memory': base_weights['memory'] * (1 + psi_data.get('memory', 0)),
            'io': base_weights['io']  # 保留但不再用于进程评分
        }

    def _find_systemd_scope(self, pid):
        """通过systemd-cgls查找进程所属的scope"""
        try:
            result = subprocess.run(
                ['systemd-cgls', '--no-page'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            # 查找包含指定PID的行及其父scope
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if f'─{pid} ' in line:
                    # 向上查找最近的scope
                    for j in range(i, -1, -1):
                        if 'scope' in lines[j]:
                            scope = re.search(r"─(.*?\.scope)", lines[j])
                            if scope:
                                return scope.group(1)
        except Exception as e:
            logger.warning(f"Failed to find systemd scope: {str(e)}")
        return None

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
        # logger.debug(f"process_info: {process_info}")
        # 1. 先尝试匹配桌面应用
        if self.desktop_apps:
            for app_id, app in self.desktop_apps.items():
                try:
                    # 检查应用的可执行文件是否匹配
                    cmd = app.get_commandline()
                    if cmd and process_info['exe'] and process_info['exe'] in cmd:
                        return {
                            'type': 'desktop',
                            'id': app_id,
                            'name': app.get_display_name()
                        }

                    # 检查应用名称是否匹配进程名
                    if app.get_name().lower() in process_info['name'].lower():
                        return {
                            'type': 'desktop',
                            'id': app_id,
                            'name': app.get_display_name()
                        }
                except Exception:
                    continue

        # 2. 尝试通过systemd-cgls查找scope或者service
        unit = self._find_systemd_unit(process_info['pids'][0])  # the first PID
        if unit:
            return {
                'type': 'systemd',
                'id': unit,
                'name': f"Systemd cgroup: {unit}"
            }

        return None

    def get_top_resource_consumers(self):
        """获取资源占用最高的3个进程及其应用信息"""
        results = []
        reach_threshold = True
        processes = self._get_top_processes(n=1)
        logger.debug(f"Top processes: {processes}")

        # Return empty list if top process doesn't meet minimum resource thresholds
        if processes and (processes[0]['cpu_avg'] / self.cpu_cores < 20 # if CPU usage < 20% per core
                          and processes[0]['mem_rss'] < psutil.virtual_memory().total * 0.05):  # 5% of total memory
            logger.info(f"Top process - {processes[0]['name']} does not meet minimum resource thresholds")
            reach_threshold = False

        for process in processes:
            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': process['pids'][0],  # Use the first PID from 'pids'
                    'name': process['name'],
                    'cmdline': process['cmdline'],
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

    def get_resource_usage(self) -> dict:
        """获取系统整体资源使用率和剩余容量"""
        # CPU：核心数、使用率（%）
        cpu_count = psutil.cpu_count(logical=True)
        cpu_usage = psutil.cpu_percent(interval=0.5)  # 0.5秒采样
        cpu_available = 100 - cpu_usage  # 剩余CPU百分比

        # 内存：总容量（GB）、使用率（%）、剩余容量占比
        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 **3)
        mem_usage = mem.percent
        mem_available_ratio = mem.available / mem.total  # 剩余内存占比

        # IO：磁盘使用率（取根目录）
        disk = psutil.disk_usage('/')
        io_usage = disk.percent
        io_available_ratio = 1 - (disk.used / disk.total)

        return {
            'cpu': {
                'count': cpu_count,
                'usage': cpu_usage,
                'available': cpu_available,
                'is_busy': cpu_usage > 90  # 暂定整体使用率 >90% 算busy
            },
            'memory': {
                'total_gb': mem_total_gb,
                'usage': mem_usage,  # %
                'available_ratio': mem_available_ratio,
                'is_busy': mem_usage > 90
            },
            'io': {
                'usage': io_usage,
                'available_ratio': io_available_ratio,
                'is_busy': io_usage > 90
            }
        }

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