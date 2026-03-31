# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import psutil
import time
from typing import Dict, Any


def get_process_info(pid: int) -> Dict[str, Any]:
    """获取单个进程的详细信息"""
    try:
        p = psutil.Process(pid)
        with p.oneshot():  # 使用oneshot()优化性能
            return {
                "pid": pid,
                "name": p.name(),
                "exe": p.exe(),
                "status": p.status(),
                "cpu_percent": p.cpu_percent(interval=0.1),
                "cpu_times": p.cpu_times(),
                "memory_info": p.memory_info(),
                "memory_percent": p.memory_percent(),
                "io_counters": p.io_counters(),
                "num_threads": p.num_threads(),
                "create_time": p.create_time()
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}


def monitor_processes(top_n: int = 5) -> None:
    """监控系统进程并打印资源使用TOP N"""
    while True:
        # 获取所有进程信息并排序（按CPU使用率）
        processes = []
        for proc in psutil.process_iter(['pid', 'name']):
            info = get_process_info(proc.pid)
            if info:  # 过滤无效进程
                processes.append(info)

        # 按CPU使用率排序
        processes.sort(key=lambda x: x.get('cpu_percent', 0), reverse=True)

        # 清屏并打印表头（Linux/macOS用clear，Windows用cls）
        print("\033c", end="")  # 跨平台清屏
        print(f"{'PID':<8}{'Name':<20}{'CPU%':<8}{'MEM%':<8}{'RSS':<12}{'IO Read':<12}{'IO Write':<12}")
        print("-" * 80)

        # 打印TOP N进程
        for p in processes[:top_n]:
            io = p.get('io_counters', psutil._common.sio(0, 0, 0, 0))
            print(
                f"{p['pid']:<8}"
                f"{p['name'][:20]:<20}"
                f"{p['cpu_percent']:<8.1f}"
                f"{p['memory_percent']:<8.1f}"
                f"{p['memory_info'].rss // 1024 // 1024:<6}MB "
                f"{io.read_bytes // 1024:<8}KB "
                f"{io.write_bytes // 1024:<8}KB"
            )

        time.sleep(2)  # 刷新间隔


if __name__ == "__main__":
    # 测试单个进程信息
    test_pid = psutil.Process().pid  # 获取当前Python进程的PID
    print(f"\n测试获取PID={test_pid}的进程信息:")
    print(get_process_info(test_pid))

    # 开始监控（按Ctrl+C退出）
    print("\n开始监控系统进程（按CPU排序，TOP 5）：")
    try:
        monitor_processes(top_n=5)
    except KeyboardInterrupt:
        print("\n监控已停止")