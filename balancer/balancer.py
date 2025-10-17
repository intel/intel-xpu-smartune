import os, signal, time
from dataclasses import dataclass
from typing import Dict, Optional

from controller.controlManager import ControlManager
from monitor.appIntercept import AppIntercept
from monitor.res_monitor import ResourceMonitor

from utils.logger import logger
from utils import app_utils
from config.config import Config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq

g_limited_apps = {}  # 记录被限制的应用

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
        self.resource_monitor = ResourceMonitor()

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

        self.monitor_thread = threading.Thread(target=self._run_monitor_resource_loop, daemon=True)
        self.monitor_thread.start()

        self.handle_thread = threading.Thread(target=self._run_handle_loop, daemon=True)
        self.handle_thread.start()

        self.app_intercept_thread = threading.Thread(target=self._run_app_intercept_loop, daemon=True)
        self.app_intercept_thread.start()

        print("服务已启动，线程已开始运行")

    def _run_monitor_resource_loop(self):
        logger.info("Monitor resource service started")
        global g_limited_apps
        idle_check_interval = 10  # 2分钟（单位：秒）
        last_check_time = 0
        top_consume_apps = []  # 保存获取到的top应用列表
        restore_pending = False  # 标记是否有待恢复的应用
        low_pressure_start_time = None  # 记录首次进入low状态的时间
        STABLE_PERIOD = 1800  # 30分钟的稳定期（秒）
        def reset_state():
            nonlocal top_consume_apps, idle_check_interval, low_pressure_start_time
            top_consume_apps = []
            idle_check_interval = 10
            low_pressure_start_time = None  # 重置计时器

        while self.is_running:
            try:
                current_time = time.time()

                # 当队列不为空时立即处理，为空时每2分钟检查一次
                if not self.app_priority_queue.empty() or (current_time - last_check_time) >= idle_check_interval:
                    pressure = self.controlManager._get_current_pressure_level()
                    last_check_time = current_time

                    if pressure == "critical":
                        # 重置low状态计时器
                        low_pressure_start_time = None
                        # 如果是第一次检测到critical状态，获取top应用列表
                        restore_pending = False
                        if not top_consume_apps:
                            top_consume_apps = self.resource_monitor.get_top_resource_consumers()
                            print(f"Top resource consumers(currently = 1): {top_consume_apps}")
                            """
                              "Top resource consumers": [
                                {
                                  "process": {
                                    "pid": 1790698,
                                    "name": "python",
                                    "cmdline": "python MetaSearch_agent.py",
                                    "cpu": 0.0,
                                    "memory_mb": 2682.0,
                                    "score": 1.281
                                  },
                                  "app": {
                                    "type": "systemd",
                                    "id": "vte-spawn-89f79f2f-8e3f-4995-b0ea-1f56ed046e33.scope",
                                    "name": "Systemd Scope: vte-spawn-89f79f2f-8e3f-4995-b0ea-1f56ed046e33.scope"
                                  }
                                },
                                {
                                  "process": {
                                    "pid": 3748,
                                    ...
                                    "score": 0.996
                                  },
                                  "app": {
                                    "type": "desktop",
                                    "id": "org.gnome.Shell.desktop",
                                    "name": "GNOME Shell"
                                  }
                                },
                                ...
                              ]                       
                            """

                        if top_consume_apps:
                            # 调用独立的处理函数
                            should_adjust, is_controlled, app_id, limit_rate = self._handle_critical_pressure(top_consume_apps)

                            if should_adjust and app_id and app_id not in g_limited_apps:
                                # 执行资源调整
                                target = top_consume_apps[0]
                                app_name = target.get('process', {}).get('name') or ''
                                logger.info(f"Adjusting resources for app: {app_id}")
                                auto_limit = self.controlManager.adjust_resources(
                                    app_id,
                                    "critical",
                                    cpu_quota=int(target['process']['cpu_avg'] * limit_rate),  # 直接计算
                                    mem_high=int(target['process']['mem_rss'] / 1024 / 1024 * limit_rate),
                                    io_weight=max(200, int(target['process']['io_read_rate'] / 1024 / 1024 * 10)),
                                    is_restore=False,
                                )
                                if auto_limit:
                                    # 记录已限制的应用
                                    g_limited_apps[app_id] = app_name
                                    if is_controlled:
                                        app_utils.update_app_status(app_id, "limited")
                                    app_utils.callback_manager.send_callback_notification({
                                        'app_id': app_id,
                                        'app_name': app_name,
                                        'status': "limited",
                                        'purpose': "app"
                                    }, False)

                            # 无论是否处理，都移除已检查的app
                            top_consume_apps.pop(0)
                            idle_check_interval = 5  # critical下，缩短检测时间
                        else:
                            reset_state()
                    elif not self.app_priority_queue.empty():
                        # 处理队列中的应用
                        app_data, priority = self.app_priority_queue.get()
                        logger.info(
                            f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
                        os.kill(app_data['pid'], signal.SIGCONT)
                        app_utils.update_app_status(app_data['app_id'], "running")
                        app_utils.callback_manager.send_callback_notification({
                            'app_id': app_data['app_id'],
                            'app_name': app_data['app_name'],
                            'status': "running",
                            'purpose': "app"
                        }, True)
                        # 处理完队列后重置top应用状态
                        reset_state()
                    else:
                        # 非 critical 状态：每次恢复一个应用
                        # 可能的bug： 需要一段时间后，或者可以判断资源<high后在恢复，不然可能刚限制又恢复了
                        # 或者渐进式恢复？
                        if pressure == "low" and g_limited_apps and not restore_pending:
                            if low_pressure_start_time is None:
                                # 第一次进入low状态，开始计时
                                low_pressure_start_time = current_time
                                logger.info(
                                    f"Low pressure detected, starting {STABLE_PERIOD} sec countdown for restore")
                            elif current_time - low_pressure_start_time >= STABLE_PERIOD:
                                # 稳定期已过，执行恢复
                                app_id, app_name = g_limited_apps.popitem()
                                logger.info(f"Restoring CPU quota for app: {app_id}, name: {app_name}")
                                restore_pending = True
                                if self.controlManager.adjust_resources(app_id, "restore"):
                                    app_utils.update_app_status(app_id, "running")
                                    app_utils.callback_manager.send_callback_notification({
                                        'app_id': app_id,
                                        'app_name': app_name,
                                        'status': "running",
                                        'purpose': "app"
                                    }, False)
                                restore_pending = False
                                low_pressure_start_time = None  # 重置计时器
                        else:
                            reset_state()

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                reset_state()
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

    def _handle_critical_pressure(self, top_consumers):
        """处理资源压力 (单次执行只处理一个app)"""
        if not top_consumers:
            return False, False, None, None

        # 记录连续critical次数 (类成员变量)
        if not hasattr(self, '_critical_counter'):
            self._critical_counter = 0

        # 每次取第一个app处理
        app_info = top_consumers[0]
        if not app_info or not app_info.get('app'):
            return False, False, None, None

        app_id = app_info['app'].get('id')
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        # 获取管控应用数据
        controlled_apps = app_utils.get_controlled_apps() or []
        controlled_map = {
            app['app_id']: app for app in controlled_apps if app.get('app_id')
        }
        name_map = {
            app['app_name'].lower(): app for app in controlled_apps if app.get('app_name')
        }

        # 判断逻辑
        is_controlled = (app_id in controlled_map) or (app_name in name_map)
        priority = None

        if is_controlled:
            # 优先用ID匹配，其次用名称匹配
            controlled_data = controlled_map.get(app_id) or name_map.get(app_name)
            priority = controlled_data.get('priority') if controlled_data else None

        # 情况1：非管控应用 -> 直接调整
        if not is_controlled:
            self._critical_counter = 0  # 重置计数器
            return True, is_controlled, app_id, 0.3

        # 情况2：管控但非critical -> 直接调整
        if priority != 'critical':
            self._critical_counter = 0  # 重置计数器
            return True, is_controlled, app_id, self.get_priority_value(priority)

        # 情况3：critical管控 -> 不处理，增加计数器
        self._critical_counter += 1

        # 检查是否连续三次critical
        if self._critical_counter >= 1:
            app_utils.callback_manager.send_callback_notification({
                'app_id': "",
                'app_name': "",
                'status': "manual_app_limit_by_user",
                'purpose': "notify"
            }, False)

            self._critical_counter = 0  # 重置计数器

        return False, False, None, None


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

    def set_resource_limit0(self, app_id: str, app_name: str) -> bool:
        """ 根据 app_id 设置资源限制 """

        # 将app放入限制列表中，等待监控线程处理，适时自动恢复
        g_limited_apps[app_id] = app_name
        app_utils.update_app_status(app_id, "limited")
        return self.controlManager.adjust_resources(
            app_id,
            "critical",
            cpu_quota=100,
            mem_high=20000,
            io_weight=120,
            is_restore=False,
        )

    def get_priority_value(self, priority):
        if priority.lower() == "critical":
            return 0.7
        elif priority.lower() == "high":
            return 0.6
        elif priority.lower() == "medium":
            return 0.5
        else:
            return 0.4

    def set_resource_limit(self, app_id: str, app_name: str, priority: str) -> bool:
        """ 根据 app_id 设置资源限制 """

        limit_rate = self.get_priority_value(priority)
        # 获取应用程序的实际资源使用情况
        usage = app_utils.get_app_resource_usage(app_id, app_name)

        if usage is None:
            print(f"Warning: Could not get resource usage for {app_name} (ID: {app_id}), using default limits")
            # 默认值
            cpu_quota = None
            mem_high = None
            io_weight = None
        else:
            # 计算限制值（使用实际值的50%，如果没查到就先不限制）
            cpu_quota = int(usage['cpu_percent'] * limit_rate) if usage['cpu_percent'] > 0 else None
            mem_high = int((usage['mem_bytes'] / (1024 * 1024)) * limit_rate) if usage['mem_bytes'] > 0 else None

            # IOWeight的计算
            io_activity = (usage['io_read_bytes'] + usage['io_write_bytes']) / (1024 * 1024)
            io_weight = int(io_activity * limit_rate) if io_activity >= 100 else None

            print("--------------------APP RESOURCE USAGE--------------------")
            print(f" Setting limits for {app_name} (ID: {app_id}) based on actual usage:")
            print(f"  CPU: {usage['cpu_percent']:.1f}% -> limit to {cpu_quota}%")
            print(f"  Memory: {usage['mem_bytes'] / 1024 / 1024:.1f}MB -> limit to {mem_high}MB")
            print(f"  IO activity: {io_activity:.1f}MB -> weight {io_weight}")

        # 将app放入限制列表中，等待监控线程处理，适时自动恢复
        g_limited_apps[app_id] = app_name
        app_utils.update_app_status(app_id, "limited")

        return self.controlManager.adjust_resources(
            app_id,
            "critical",
            cpu_quota=cpu_quota,
            mem_high=mem_high,
            io_weight=io_weight,
            is_restore=False,
        )

    def set_restore_resource(self, app_id: str, app_name: str) -> bool:
        """ 根据 app_id 恢复资源限制 """

        # 从限制列表中移除（无论是否存在都尝试移除）
        g_limited_apps.pop(app_id, None)
        app_utils.update_app_status(app_id, "running")
        return self.controlManager.adjust_resources(app_id, "restore")

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

        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1)  # 等待线程结束
        if hasattr(self, "handle_thread"):
            self.handle_thread.join(timeout=1)
        if hasattr(self, "app_intercept_thread"):
            self.app_intercept_thread.join(timeout=1)
        print("服务已停止，线程已结束")
