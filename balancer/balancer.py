import time
from dataclasses import dataclass
from typing import Dict, Optional, List

from monitor.psi import PSIMonitor
from monitor.cgroup import CgroupMonitor
from monitor.pressure import PressureAnalyzer

from controller.controller import Controller
from controller.io import IOController
from controller.cpu import CPUController
from controller.memory import MemoryController
from controller.governor import GovernorController

from balancer.baseArch import BaseBalancer
from utils.logger import logger
from config import Config


@dataclass
class WorkloadGroup:
    name: str
    priority: int
    cpu_weight: int
    memory_min: int = 0
    io_weight: int = 100


@dataclass
class WorkloadTask:
    workload: WorkloadGroup
    params: Dict
    pid: Optional[int] = None
    task_id: str = ""


class DynamicBalancer(BaseBalancer):
    def __init__(self, config_file=None):
        self.config = Config.from_file(config_file)
        self.psi = PSIMonitor()
        self.cgroups = CgroupMonitor(self.config.cgroup_mount)
        self.analyzer = PressureAnalyzer(self.config)
        self.controller = Controller(self.config.cgroup_mount)
        self.cpu = CPUController(self.config.cgroup_mount)
        self.memory = MemoryController(self.config.cgroup_mount)
        self.io = IOController(self.config.cgroup_mount)
        self.governor = GovernorController(self.config.cgroup_mount)

        # 资源管理
        self.workload_groups = {}  # 注册的workload类型
        self.running_tasks = {}  # pid -> WorkloadTask
        self.known_pids = set()  # 已识别的PID集合

        self._init_default_workloads()


    def _init_default_workloads(self):
        default_groups = [
            WorkloadGroup("critical", 100, 300, 2<<30, 500),
            WorkloadGroup("high", 80, 200, 1<<30, 300),
            WorkloadGroup("normal", 50, 100, 0, 200),
            WorkloadGroup("best-effort", 20, 50, 0, 100)
        ]
        for group in default_groups:
            self.register_workload_group(group)


    def _process_task(self, task: Dict) -> Dict:
        """重写基类方法处理具体任务"""
        try:
            if task["type"] == "new_app":
                return self._handle_new_app(task)
        except Exception as e:
            logger.error(f"Task processing failed: {str(e)}")
            return {"status": "error", "message": str(e)}
        return {"status": "ignored"}


    def _handle_new_app(self, task: Dict) -> Dict:
        """处理新应用启动请求"""
        logger.info("Handling new app request for group: %s", task["group"])

        # 校验任务组
        if task["group"] not in self.workload_groups:
            return {"status": "error", "reason": "unknown_workload"}

        # 创建任务实例
        workload = self.workload_groups[task["group"]]
        new_task = WorkloadTask(
            workload=workload,
            params=task.get("params", {}),
            task_id=task.get("task_id", ""),
            pid=task.get("params", {}).get("pid")
        )

        pressure_level = self._get_current_pressure_level()
        self._execute_task(new_task, pressure_level)

        return {"status": "success" if new_task.pid else "queued"}

    def _get_current_pressure_level(self) -> str:
        """获取当前系统压力级别"""
        psi_data = self.psi.get_current_pressure()
        score = self.analyzer.calculate_pressure_score(psi_data)
        level = self.analyzer.get_pressure_level(score)
        logger.debug("Current PSI level: %s (score: %.2f)", level, score)
        return level

    def _execute_task(self, task: WorkloadTask, pressure_level: str) -> bool:
        """执行任务"""
        try:
            if task.pid:
                self.running_tasks[task.pid] = task
                logger.info("Task %s registered (PID: %d)", task.workload.name, task.pid)

                self.adjust_resources(pressure_level)
                return True
            return False
        except Exception as e:
            logger.error("Task registration failed: %s", str(e))
            return False


    def adjust_resources(self, pressure_level: str):
        """Adjust resources based on pressure level"""
        adjustments = {
            'low': self._low_pressure_adjustment,
            'medium': self._medium_pressure_adjustment,
            'high': self._high_pressure_adjustment,
            'critical': self._critical_pressure_adjustment
        }
        adjustments.get(pressure_level, lambda: None)()

    def _low_pressure_adjustment(self):
        """Low pressure adjustments"""
        self.governor.set_powersave()

        # release CPU/MEM throttling
        self.controller.restore_cpu_throttle()

    def _medium_pressure_adjustment(self):
        """Medium pressure adjustments"""
        self.governor.set_powersave()

        # release CPU/MEM throttling
        self.controller.restore_cpu_throttle()

    def _high_pressure_adjustment(self):
        """High pressure adjustments"""
        self.governor.set_performance()

        # throttle CPU/MEM
        self.controller.high_cpu_throttle()

    def _critical_pressure_adjustment(self):
        """Critical pressure adjustments"""
        self.governor.set_performance()

        # throttle CPU/MEM
        self.controller.critical_cpu_throttle()

        # Throttle best-effort workloads heavily
        self.cpu.set_weight("best-effort", 10)
        self.memory.set_limit("best-effort", "high", "20%")
        self.io.set_limit("best-effort", "max", "1000")

        # Boost critical workloads
        self.cpu.set_weight("critical", 500)
        self.memory.protect("critical", "min", "4G")


    def register_workload_group(self, group: WorkloadGroup):
        """注册可用的workload类型"""
        with self._lock:
            self.workload_groups[group.name] = group
            logger.info(f"Registered workload group: {group.name}")

    def add_workload(self, group_name: str, params: Dict = None) -> bool:
        """添加具体任务到队列"""
        if group_name not in self.workload_groups:
            logger.error(f"Unknown workload group: {group_name}")
            return False

        task = {
            "type": "new_app",
            "group": group_name,
            "params": params or {},
            "task_id": f"wl_{time.time_ns()}"
        }
        self.push_task(task)
        print(f"add workload to task: {task}")
        return True

    def shutdown(self):
        """安全关闭服务"""
        super().stop()

    #
    # def _detect_new_apps(self):
    #     """检测新启动的应用进程"""
    #     current_pids = self.cgroups.get_all_pids()
    #     new_pids = set(current_pids) - self.known_pids
    #
    #     for pid in new_pids:
    #         app_type = self._classify_app(pid)
    #         if app_type in self.workload_groups:
    #             task = WorkloadTask(
    #                 workload=self.workload_groups[app_type],
    #                 params={"pid": pid},
    #                 pid=pid,
    #                 task_id=f"app_{pid}"
    #             )
    #             self._add_task_to_queue(task)
    #             self.known_pids.add(pid)
    #
    # def _auto_balance(self):
    #     """基于系统压力的自动资源平衡"""
    #     psi_data = self.psi.get_current_pressure()
    #     score = self.analyzer.calculate_pressure_score(psi_data)
    #     level = self.analyzer.get_pressure_level(score)
    #
    #     adjustments = {
    #         'low': self._low_pressure_adjustment,
    #         'medium': self._medium_pressure_adjustment,
    #         'high': self._high_pressure_adjustment,
    #         'critical': self._critical_pressure_adjustment
    #     }
    #     adjustments.get(level, lambda: None)()
    #
    #
    # def _add_task_to_queue(self, task: WorkloadTask) -> bool:
    #     """实际队列添加逻辑"""
    #     try:
    #         task_dict = {
    #             "type": "new_app",
    #             "group": task.workload.name,
    #             "params": task.params,
    #             "task_id": task.task_id
    #         }
    #
    #         with self._lock:
    #             self.task_queue.put(task_dict)
    #             logger.debug(f"Added task {task.task_id} to queue")
    #             return True
    #     except Exception as e:
    #         logger.error(f"Task submission failed: {e}")
    #         return False
