import os, signal, time
from dataclasses import dataclass
from typing import Dict, Optional

from controller.controlManager import ControlManager
from monitor.appIntercept import AppIntercept

from utils.logger import logger
from utils import app_utils
from config.config import Config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq


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


class MaxPriorityQueue:
    def __init__(self):
        self._queue = queue.PriorityQueue()
        self._index = 0  # 用于处理相同优先级的情况

    def put(self, item):
        # 存储负值实现最大堆，使用三元组 (负优先级, 自增索引, 数据)
        priority = -item[1]
        heapq.heappush(self._queue.queue, (priority, self._index, item))
        self._index += 1

    def get(self):
        # 获取时取出原始数据
        return heapq.heappop(self._queue.queue)[-1]

    def remove_if(self, condition_func):
        """
        删除满足条件的项目（通用方法，不涉及业务逻辑）
        :param condition_func: 接受一个队列项目（元组 (data, priority)），返回 bool
        :return: 被删除的项目列表
        """
        removed_items = []
        new_queue = []

        for priority, idx, item in self._queue.queue:
            if condition_func(item):
                removed_items.append(item)
            else:
                new_queue.append((priority, idx, item))

        self._queue.queue = new_queue
        heapq.heapify(self._queue.queue)  # 重新堆化
        return removed_items

    def empty(self):
        """检查队列是否为空"""
        return len(self._queue.queue) == 0

    def __str__(self):
        # 按优先级降序展示（实际存储是升序）
        items = sorted(((-priority, data) for priority, _, data in self._queue.queue), reverse=True)
        return str([(k, v) for (_, (k, v)) in items])

    def __len__(self):
        """获取队列当前元素数量"""
        return len(self._queue.queue)


class DynamicBalancer:
    def __init__(self):
        self.controlManager = ControlManager()
        self.bpf_monitor = AppIntercept("monitor/bpf_event.c")


        # 资源管理
        self.workload_groups = {}  # 注册的workload类型
        self.running_tasks = {}  # pid -> WorkloadTask
        self.known_pids = set()  # 已识别的PID集合

        self.is_running = False
        self.app_detect_queue = JoinableQueue(1000000)
        self.app_priority_queue = MaxPriorityQueue()

        self._init_default_workloads()

    def _init_default_workloads(self):
        default_groups = [
            WorkloadGroup("critical", 100, 300, 2<<30, 500),
            WorkloadGroup("high", 80, 200, 1<<30, 300),
            WorkloadGroup("normal", 50, 100, 0, 200),
            WorkloadGroup("best-effort", 20, 50, 0, 100)
        ]
        # for group in default_groups:
        #     self.register_workload_group(group)

    def start(self):
        """
        启动服务，包括启动服务线程来处理任务队列中的任务
        """
        self.is_running = True

        self.monitor_thread = threading.Thread(target=self._run_monitor_resource_loop)
        self.monitor_thread.start()

        self.handle_thread = threading.Thread(target=self._run_handle_loop)
        self.handle_thread.start()

        self.app_intercept_thread = threading.Thread(target=self._run_app_intercept_loop)
        self.app_intercept_thread.start()

        print("服务已启动，线程已开始运行")


    def _run_monitor_resource_loop(self):
        logger.info("Monitor resource service started")
        idle_check_interval = 120  # 2分钟（单位：秒）
        last_check_time = 0

        while self.is_running:
            try:
                current_time = time.time()

                # 当队列不为空时立即处理，为空时每2分钟检查一次
                if not self.app_priority_queue.empty() or (current_time - last_check_time) >= idle_check_interval:
                    pressure = self.controlManager._get_current_pressure_level()
                    last_check_time = current_time

                    if pressure == "critical":
                        # adjust app
                        time.sleep(5)
                        continue
                    elif not self.app_priority_queue.empty():
                        # 处理队列中的应用
                        app_data, priority = self.app_priority_queue.get()
                        logger.info(
                            f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                        os.kill(app_data['pid'], signal.SIGCONT)

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                time.sleep(1)

        logger.info("Monitor resource service stopped")

    def _run_handle_loop(self):
        logger.info("Resource handle service is wait for processing")
        while self.is_running:
            try:
                # 从app_detect_queue任务队列中获取任务并处理
                coming_app = self.bpf_monitor.app_pending_queue.get(block=True, timeout=5)
                logger.info(f"_run_handle_loop: Processing app {coming_app}")

                # 从DB中获取coming_app priority,如没有设置，就是low
                # priority = "1000"  # critical
                # priority_value = {"Calculator": 1000, "test2": 1500, "test3": 1300}
                priority = self.controlManager.get_app_priority(app_name=coming_app["app_name"])
                logger.info(f"_run_handle_loop: App {coming_app['app_name']} priority is {priority}")
                # priority = priority_value[coming_app["app_name"]]
                #
                # # 将任务放入待处理队列
                priority_num = self.controlManager.get_priority_value(priority)
                print(f"_run_handle_loop: priority value is {priority_num}")
                self.app_priority_queue.put((coming_app, priority_num))
                logger.info(f"_run_handle_loop: Resource insufficient, {coming_app} app added to pending queue")

            except:
                time.sleep(2)
        print("退出_run_handle_loop")

    def _run_app_intercept_loop(self):
        logger.info("Resource app intercept service is wait for processing")

        # 打开性能缓冲区
        self.bpf_monitor.bpf["events"].open_perf_buffer(self.bpf_monitor.print_event)
        print("Ctrl+C to exit")

        monitor_apps = app_utils.get_controlled_apps()
        if monitor_apps:
            # 将受控应用添加到BPF监控列表
            monitored_names = [app["app_name"] for app in monitor_apps]
            self.bpf_monitor.add_to_monitorlist(monitored_names)
            print(f"Monitoring execve() for: {', '.join(monitored_names)}")
        else:
            logger.warning("No controlled apps to monitor")

        while self.is_running:
            try:
                # 监控启动事件
                self.bpf_monitor.bpf.perf_buffer_poll(timeout=100)
            except KeyboardInterrupt:
                print("\nExiting...")
                break
            except Exception as e:
                logger.error(f"App intercept error: {str(e)}")
                time.sleep(3)
                break


    def cancel_relaunch_by_app_id(self, app_id: str) -> bool:
        """ 根据 app_id 删除队列中的项目，并杀死对应进程 """
        def condition(item):
            data, _ = item
            return data.get('app_id') == app_id

        # 从队列中删除符合条件的项目
        removed_items = self.app_priority_queue.remove_if(condition)

        # 杀死对应的进程
        killed = False
        for item in removed_items:
            data, _ = item
            pid = data.get('pid')
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    pass

        return killed

    def set_resource_limit(self, app_id: str) -> bool:
        """ 根据 app_id 设置资源限制 """
        return self.controlManager.adjust_resources(app_id, "critical")

    def _execute_task(self, task: WorkloadTask, pressure_level: str) -> bool:
        """执行任务"""
        try:
            if task.pid:
                self.running_tasks[task.pid] = task
                logger.info("Task %s registered (PID: %d)", task.workload.name, task.pid)

                self.controlManager.adjust_resources("", pressure_level)
                return True
            return False
        except Exception as e:
            logger.error("Task registration failed: %s", str(e))
            return False


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
        """
        停止服务线程，设置运行标志为False，并等待线程结束，同时确保任务队列中的任务都已处理完成
        """
        print("服务开始停止.............")
        if not self.is_running:
            print("服务已经停止，无需再次操作")
            return
        self.is_running = False

        self.monitor_thread.join()
        self.handle_thread.join()
        self.app_intercept_thread.join()
        print("服务已停止，线程已结束")
