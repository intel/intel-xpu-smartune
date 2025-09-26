#!/usr/bin/env python3
import os
import psutil
import subprocess
import re
import logging
from gi.repository import Gio
from collections import defaultdict
import time
from time import sleep
from utils.logger import logger
from monitor.psi import PSIMonitor


class ResourceMonitor:
    def __init__(self, config=None):
        """初始化资源监视器"""
        self.config = config or {
            'weights': {'cpu': 0.5, 'memory': 0.3, 'io': 0.2},
            'pressure_sensitivity': 0.7,
            'io_threshold': 5e7,  # 50MB
            'blacklist': ['systemd', 'kworker', 'dbus']
        }
        self.cpu_cores = os.cpu_count() or 16

        # 桌面应用信息
        try:
            self.desktop_apps = {app.get_id(): app for app in Gio.AppInfo.get_all()}
            logger.info(f"Loaded {len(self.desktop_apps)} desktop applications")
        except Exception as e:
            logger.warning(f"Could not load desktop apps: {str(e)}")
            self.desktop_apps = {}

    def _get_top_processes(self, n=1, samples=3, interval=1.0):
        """返回带score的TOP进程数据，同时适配mem_high和io_weight参数需求 for adjustment"""
        cumulative = {}

        for _ in range(samples):
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent',
                                             'memory_info', 'memory_percent', 'io_counters',
                                             'create_time']):
                try:
                    info = proc.info
                    if not info.get('cmdline') or any(b in info.get('name', '') for b in self.config['blacklist']):
                        continue
                    if time.time() - info['create_time'] < 2:
                        continue

                    pid = info['pid']
                    if pid not in cumulative:
                        cumulative[pid] = {
                            'cpu_sum': 0,
                            'mem_percent_sum': 0, # 计算score
                            'mem_rss_sum': 0,  # 物理内存字节数（用于mem_high）
                            'io_read_sum': 0,  # 累计读取字节（用于io_weight）
                            'count': 0,
                            'name': info['name'],
                            'cmdline': ' '.join(info['cmdline'])
                        }

                    # 累计各指标
                    cumulative[pid]['cpu_sum'] += info['cpu_percent']
                    cumulative[pid]['mem_percent_sum'] += info['memory_percent']
                    cumulative[pid]['mem_rss_sum'] += info['memory_info'].rss  # 物理内存
                    if info['io_counters']:
                        cumulative[pid]['io_read_sum'] += info['io_counters'].read_bytes
                    cumulative[pid]['count'] += 1

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if _ != samples - 1:
                time.sleep(interval)

        # 计算平均值
        psi_data = PSIMonitor().get_current_pressure()
        dynamic_weights = self._adjust_weights_by_pressure(psi_data)

        processes = []
        for pid, data in cumulative.items():
            if data['count'] > 0:  # 有效采样
                avg_cpu = data['cpu_sum'] / data['count']
                avg_mem_percent = data['mem_percent_sum'] / data['count']
                avg_mem_rss = data['mem_rss_sum'] / data['count']  # 字节单位
                io_read_rate = data['io_read_sum'] / (data['count'] * interval)  # B/s

                # logger.info(f"PID {pid} - avg_mem_percent: {avg_mem_percent}, avg_mem_rss: {avg_mem_rss}, IO Read Rate: {io_read_rate} B/s")

                score = (
                        dynamic_weights['cpu'] * min(avg_cpu, 100) +
                        dynamic_weights['memory'] * min(avg_mem_percent, 100) +
                        dynamic_weights['io'] * min(io_read_rate / 1024 / 1024, 10)
                )

                processes.append({
                    'pid': pid,
                    'name': data['name'],
                    'cmdline': data['cmdline'],
                    'score': round(score, 2),
                    'cpu_avg': round(avg_cpu, 1),
                    'mem_avg': round(avg_mem_percent, 3),
                    'mem_rss': avg_mem_rss,   # 物理内存字节数
                    'io_read_rate': io_read_rate  # 读取速率(B/s)
                })

        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

    def _adjust_weights_by_pressure(self, psi_data):
        """根据PSI压力动态调整权重"""
        base_weights = self.config['weights']
        return {
            'cpu': base_weights['cpu'] * (1 + psi_data.get('cpu', 0)),
            'memory': base_weights['memory'] * (1 + psi_data.get('memory', 0)),
            'io': base_weights['io']  # 保留但不再用于进程评分
        }


    def _find_systemd_scope(self, pid):
        """通过systemd-cgls查找进程所属的scope"""
        try:
            result = subprocess.run(
                ['systemd-cgls', '--no-pager'],
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
                            scope = re.search(r'([\w-]+\.scope)', lines[j])
                            if scope:
                                return scope.group(1)
        except Exception as e:
            logger.warning(f"Failed to find systemd scope: {str(e)}")
        return None

    def _try_match_app(self, process_info):
        """尝试匹配桌面应用或systemd scope"""
        logger.info(f"process_info: {process_info}")
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

        # 2. 尝试通过systemd-cgls查找scope
        scope = self._find_systemd_scope(process_info['pid'])
        if scope:
            return {
                'type': 'systemd',
                'id': scope,
                'name': f"Systemd Scope: {scope}"
            }

        return None

    def get_top_resource_consumers(self):
        """获取资源占用最高的3个进程及其应用信息"""
        processes = self._get_top_processes(n=1)
        results = []

        print(f"Top processes: {processes}")
        for process in processes:
            app_info = self._try_match_app(process)
            results.append({
                'process': {
                    'pid': process['pid'],
                    'name': process['name'],
                    'cmdline': process['cmdline'],
                    'score': round(process['score'], 3),
                    'cpu_avg': process['cpu_avg'],
                    'mem_rss': process['mem_rss'],
                    'io_read_rate': process['io_read_rate']
                },
                'app': app_info
            })

        return results


def main():
    """调试用主函数"""
    logger.info("==== Starting Resource Monitor ====")

    monitor = ResourceMonitor()

    try:
        while True:
            results = monitor.get_top_resource_consumers()
            for i, result in enumerate(results, 1):
                print(f"\n=== Top Resource Consumer #{i} ===")
                print(f"Process: {result['process']['name']} (PID: {result['process']['pid']})")
                print(f"CPU: {result['process']['cpu']}% | Memory: {result['process']['memory_mb']}MB")
                print(f"Score: {result['process']['score']:.2f}")
                print(f"Cmd: {result['process']['cmdline'][:100]}...")

                if result['app']:
                    print(f"\nMatched to: {result['app']['name']} ({result['app']['type']})")
                    print(f"ID: {result['app']['id']}")
                else:
                    print("\nNo matching application found")

            sleep(5)

    except KeyboardInterrupt:
        logger.info("\nMonitoring stopped by user")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    main()