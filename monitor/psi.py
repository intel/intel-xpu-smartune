import os
import select
import time
from collections import defaultdict
from typing import Dict, Optional


class PSIMonitor:
    """单例模式的PSI监控类，提供当前系统压力数据接口"""
    # 单例实例
    _instance: Optional['PSIMonitor'] = None
    # PSI文件路径
    _PRESSURE_FILES = {
        'cpu': "/proc/pressure/cpu",
        'memory': "/proc/pressure/memory",
        'io': "/proc/pressure/io"
    }
    # 触发配置: (some阈值(ms), 窗口(sec))
    _TRIGGER_CONFIG = {
        'cpu': (100, 5),
        'memory': (1, 5),
        'io': (100, 5)
    }

    def __new__(cls):
        """单例模式：确保全局只有一个实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # 初始化资源（仅首次实例化执行）
            cls._instance._fds = {}
            cls._instance._last_total = {}
            cls._instance._pressure_history = defaultdict(list)
            cls._instance._last_pressure = {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}
            cls._instance._window_sec = 5
            # 初始化文件描述符和触发器
            cls._instance._setup_resources()
        return cls._instance

    def _setup_resources(self):
        """初始化PSI文件描述符和触发条件（单例初始化时执行）"""
        try:
            # 打开PSI文件（读写+非阻塞）
            for resource, path in self._PRESSURE_FILES.items():
                self._fds[resource] = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            # 设置触发条件
            for resource, fd in self._fds.items():
                self._setup_trigger(fd, resource)
        except OSError as e:
            raise RuntimeError(f"PSI资源初始化失败: {str(e)}") from e

    def _setup_trigger(self, fd: int, resource: str):
        """设置指定资源的PSI触发条件"""
        some_ms, window_sec = self._TRIGGER_CONFIG[resource]
        # 触发格式：some <阈值(微秒)> <窗口(微秒)>
        trigger = f"some {some_ms * 1000} {window_sec * 1000000}\n"
        os.write(fd, trigger.encode())
        os.lseek(fd, 0, os.SEEK_SET)  # 重置文件指针

    def _parse_total(self, data: str) -> int:
        """从PSI数据中提取total累计压力时间（微秒）"""
        for line in data.split('\n'):
            if line.startswith('some'):
                return int(line.split('total=')[-1])
        return 0

    def _get_resource_pressure(self, resource: str) -> float:
        """计算单个资源的当前压力值（0-1范围）"""
        fd = self._fds[resource]
        now = time.time()
        os.lseek(fd, 0, os.SEEK_SET)

        try:
            data = os.read(fd, 1024).decode()
        except OSError as e:
            raise RuntimeError(f"{resource} PSI数据读取失败: {str(e)}") from e

        current_total = self._parse_total(data)
        # 首次读取：初始化历史记录，返回0
        if resource not in self._last_total:
            self._last_total[resource] = (now, current_total)
            return 0.0

        # 计算压力值：(当前total - 历史total) / 时间差（转换为秒）
        last_time, last_total = self._last_total[resource]
        time_delta = now - last_time
        total_delta = current_total - last_total

        if time_delta <= 0:
            pressure = 0.0
        else:
            pressure = (total_delta / 1_000_000) / time_delta  # 微秒→秒
            pressure = max(0.0, min(pressure, 1.0))  # 限制范围

        # 更新历史记录
        self._last_total[resource] = (now, current_total)
        self._pressure_history[resource].append((now, pressure))
        self._last_pressure[resource] = pressure
        # 清理过期数据（窗口外）
        self._clean_old_data(resource)
        return pressure

    def _clean_old_data(self, resource: str):
        """清理指定资源的窗口外历史数据"""
        cutoff = time.time() - self._window_sec
        self._pressure_history[resource] = [
            (t, p) for t, p in self._pressure_history[resource] if t >= cutoff
        ]
        # 窗口内无数据时，用最后一次压力值填充（避免数据中断）
        if not self._pressure_history[resource] and self._last_pressure[resource] > 0:
            self._pressure_history[resource].append((cutoff + 0.1, self._last_pressure[resource]))

    def _get_window_average(self, resource: str) -> float:
        """获取指定资源的窗口内平均压力值"""
        history = self._pressure_history[resource]
        return sum(p for _, p in history) / len(history) if history else 0.0

    def get_current_pressure(self) -> Dict[str, float]:
        """
        对外接口：返回当前各资源的平均压力数据
        返回格式：{'cpu': 0.xx, 'memory': 0.xx, 'io': 0.xx}
        """
        # 主动更新所有资源的压力数据
        for resource in self._PRESSURE_FILES.keys():
            self._get_resource_pressure(resource)
        # 返回窗口内平均值（与外部calculate_pressure_score逻辑适配）
        return {
            'cpu': round(self._get_window_average('cpu'), 2),
            'memory': round(self._get_window_average('memory'), 2),
            'io': round(self._get_window_average('io'), 2)
        }

    def cleanup(self):
        """资源清理：关闭文件描述符（程序退出时调用）"""
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError as e:
                print(f"PSI文件描述符关闭失败: {str(e)}")
        # 重置单例（可选：用于测试场景）
        PSIMonitor._instance = None

    def __del__(self):
        """析构函数：确保资源清理"""
        self.cleanup()
