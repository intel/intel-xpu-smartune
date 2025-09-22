#!/usr/bin/env python3
import os
import psutil
import subprocess
import re
import logging
from gi.repository import Gio
from collections import defaultdict
from time import sleep
from utils.logger import logger

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

    def _get_top_processes(self, n=3):
        """获取资源占用最高的n个进程"""
        processes = []

        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline',
                                         'cpu_percent', 'memory_percent',
                                         'memory_info', 'io_counters']):
            try:
                info = proc.info
                if not info.get('cmdline'):
                    continue

                # 跳过黑名单进程
                if any(b in info.get('name', '') for b in self.config['blacklist']):
                    continue

                # 计算综合评分
                cpu_score = info['cpu_percent'] / self.cpu_cores
                mem_score = info['memory_percent']
                io_score = sum(info['io_counters'][:2]) / self.config['io_threshold'] if info.get('io_counters') else 0

                score = (
                        cpu_score * self.config['weights']['cpu'] +
                        mem_score * self.config['weights']['memory'] +
                        io_score * self.config['weights']['io']
                )

                processes.append({
                    'pid': info['pid'],
                    'name': info['name'],
                    'exe': info.get('exe', ''),
                    'cmdline': ' '.join(info['cmdline']),
                    'cpu_percent': info['cpu_percent'],
                    'memory_mb': info['memory_info'].rss / (1024 ** 2) if info.get('memory_info') else 0,
                    'io_bytes': sum(info['io_counters'][:2]) if info.get('io_counters') else 0,
                    'score': score
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError) as e:
                logger.debug(f"Skipping process: {str(e)}")
                continue

        # 按评分排序并返回前n个
        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

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
        processes = self._get_top_processes(n=3)
        results = []

        for process in processes:
            app_info = self._try_match_app(process)
            results.append({
                'process': {
                    'pid': process['pid'],
                    'name': process['name'],
                    'cmdline': process['cmdline'],
                    'cpu': round(process['cpu_percent'], 1),
                    'memory_mb': round(process['memory_mb'], 1),
                    'score': round(process['score'], 3)
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