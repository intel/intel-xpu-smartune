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
import select
import time
import datetime
from collections import defaultdict

class WeightedPSIMonitor:
    CPU_PRESSURE_FILE = "/proc/pressure/cpu"
    MEMORY_PRESSURE_FILE = "/proc/pressure/memory"
    IO_PRESSURE_FILE = "/proc/pressure/io"

    # 降低内存触发阈值（更容易检测到压力），IO阈值适当降低
    TRIGGER_CONFIG = {
        'cpu': (100, 5),      # 5秒内累计100ms压力触发（保持不变）
        'memory': (1, 5),    # 降低内存阈值：5秒内累计50ms即触发
        'io': (100, 5)        # 降低IO阈值：5秒内累计200ms即触发
    }

    # 新增状态等级阈值（基于总分）
    STATUS_LEVELS = {
        'low': 0.4,  # <40% 正常
        'medium': 0.6,  # 40%-60% 中等
        'high': 0.8,  # 60%-80% 高
        'critical': 1.0  # >80% 严重
    }

    WEIGHTS = (2, 7, 1)
    WINDOW_SECS = 5  # 窗口保持5秒，但优化数据保留逻辑

    def __init__(self):
        self.fds = {}
        self.last_total = {}  # {资源: (时间戳, total微秒)}
        self.pressure_history = defaultdict(list)  # {资源: [(时间戳, 压力占比)]}
        # 记录最后一次的压力值（用于窗口内无数据时填充）
        self.last_pressure = {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}

    def _setup_trigger(self, fd, resource):
        some_ms, window_sec = self.TRIGGER_CONFIG[resource]
        trigger = f"some {some_ms * 1000} {window_sec * 1000000}\n"  # 单位转换：ms→us，sec→us
        try:
            os.write(fd, trigger.encode())
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as e:
            print(f"设置{resource}触发失败: {e}")

    def setup_polling(self):
        self.fds = {
            'cpu': os.open(self.CPU_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK),
            'memory': os.open(self.MEMORY_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK),
            'io': os.open(self.IO_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK)
        }
        for resource, fd in self.fds.items():
            self._setup_trigger(fd, resource)

    def _parse_total(self, data):
        for line in data.split('\n'):
            if line.startswith('some'):
                return int(line.split('total=')[-1])
        return 0

    def _get_pressure_ratio(self, resource, fd):
        now = time.time()
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            data = os.read(fd, 1024).decode()
            print(f"\n{resource} 数据...: {data.strip()}")
        except OSError as e:
            print(f"\n{resource}读取错误: {e}")
            return 0.0  # 出错时返回0，避免崩溃
        current_total = self._parse_total(data)

        if resource not in self.last_total:
            self.last_total[resource] = (now, current_total)
            return 0.0

        last_time, last_total = self.last_total[resource]
        time_delta = now - last_time
        total_delta = current_total - last_total

        # 计算压力占比（微秒→秒）
        ratio = (total_delta / 1_000_000) / time_delta if time_delta > 0 else 0.0
        ratio = max(0.0, min(ratio, 1.0))  # 限制在0~1之间

        self.last_total[resource] = (now, current_total)
        self.last_pressure[resource] = ratio  # 更新最后一次压力值
        return ratio

    def _clean_old_data(self):
        cutoff = time.time() - self.WINDOW_SECS
        for resource in self.pressure_history:
            # 保留窗口内数据
            self.pressure_history[resource] = [
                (t, p) for t, p in self.pressure_history[resource] if t >= cutoff
            ]
            # 若窗口内无数据，用最后一次压力值填充（避免突然归零）
            if not self.pressure_history[resource] and self.last_pressure[resource] > 0:
                self.pressure_history[resource].append((cutoff + 0.1, self.last_pressure[resource]))

    def _window_average(self, resource):
        history = self.pressure_history[resource]
        return sum(p for _, p in history) / len(history) if history else 0.0

    def calculate_score(self):
        """优化加权计算：提高权重占比，避免总分偏低"""
        cpu_avg = self._window_average('cpu')
        mem_avg = self._window_average('memory')
        io_avg = self._window_average('io')

        # 直接用权重乘积求和（不除以权重和），放大总分范围
        total = (cpu_avg * self.WEIGHTS[0] +
                 mem_avg * self.WEIGHTS[1] +
                 io_avg * self.WEIGHTS[2])

        print(f"total... ={total}, cpu_avg={cpu_avg}, mem_avg={mem_avg}, io_avg={io_avg}")
        # 限制总分在0-1（对应0%-100%）
        return min(total, 1.0)

    def _get_status(self, score):
        """根据总分判断状态等级"""
        if score >= self.STATUS_LEVELS['critical']:
            return 'CRITICAL'
        elif score >= self.STATUS_LEVELS['high']:
            return 'HIGH'
        elif score >= self.STATUS_LEVELS['medium']:
            return 'MEDIUM'
        elif score >= self.STATUS_LEVELS['low']:
            return 'LOW'
        else:
            return 'LOW'

    def run(self):
        poller = select.poll()
        for fd in self.fds.values():
            poller.register(fd, select.POLLPRI)

        try:
            while True:
                events = poller.poll(1000)  # 1秒超时，定期检查
                now = time.time()

                for fd, _ in events:
                    resource = next(k for k, v in self.fds.items() if v == fd)
                    ratio = self._get_pressure_ratio(resource, fd)
                    self.pressure_history[resource].append((now, ratio))
                    print(f"\n{resource.upper()} 压力值: {ratio:.2f}")

                self._clean_old_data()
                score = self.calculate_score()
                status = self._get_status(score)

                print(f"\r{datetime.datetime.now()} 状态: {status} | 总分: {score:.2f} | 压力 - "
                      f"CPU: {self._window_average('cpu'):.2f} | "
                      f"MEM: {self._window_average('memory'):.2f} | "
                      f"IO: {self._window_average('io'):.2f}",
                      )

        except KeyboardInterrupt:
            print("\n退出监控")

    def cleanup(self):
        for fd in self.fds.values():
            os.close(fd)


if __name__ == "__main__":
    monitor = WeightedPSIMonitor()
    try:
        monitor.setup_polling()
        monitor.run()
    finally:
        monitor.cleanup()
