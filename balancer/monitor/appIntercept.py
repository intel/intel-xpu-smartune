# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import signal
from multiprocessing import JoinableQueue
from threading import Timer
from typing import Any, List, Set, Union

import psutil
from bcc import BPF
from controller.controlManager import ControlManager
from utils import app_utils
from utils.logger import logger


# 定义与BPF代码中相同的常量
COMM_LEN = 32
PY_MAX_FILE_LEN = 64


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AppIntercept(metaclass=SingletonMeta):
    def __init__(self, c_src_file: str = "bpf_event.c"):
        self.bpf = BPF(src_file=c_src_file, cflags=["-Wno-duplicate-decl-specifier"])
        self.controlManager = ControlManager()
        self.monitored_apps: Set[str] = set()
        self.handled_processes: Set[int] = set()  # 初始化已处理进程集合
        self.controlled_app_map = []
        self._app_map_index = {}  # 把map做成索引形式，方便查找
        self.relaunch_apps = {}
        self.app_pending_queue = JoinableQueue(1000000)
        self.monitored_app_launched = {}  # 当前已经启动的监控app
        self.pending_exit_events = {}  # 待处理的退出事件（PID: Timer）

    def rebuild_controlled_map(self):
        self.controlled_app_map = app_utils.get_controlled_apps()
        self._rebuild_index()

    def _rebuild_index(self):
        self._app_map_index = {
            app["app_name"].lower(): app for app in (self.controlled_app_map or [])
        }

    def trace_print(self) -> None:
        self.bpf.trace_print()

    def get_main_process(self, comm: str, filename: str) -> tuple[bool, str]:
        """检查是否是主进程"""
        filename_lower = filename.lower()
        comm_lower = comm.lower()

        # logger.debug(f"\n[DEBUG] Checking process - comm: {comm}, filename: {filename}")
        # logger.debug(f"[DEBUG] Monitored apps: {self.monitored_apps}")

        # 定义一些特殊的应用可执行文件名映射
        cnf_appname = self.controlManager.config.monitor_apps
        app_executables = {
            item['name']: item['bpf_name'] for item in cnf_appname
        }
        # 先尝试自定义中匹配
        app_flag = []
        for app in self.monitored_apps:
            # 检查是否有预定义的可执行文件名
            executables = app_executables.get(app, [])
            exact_match = any(
                f"/{exe}" in filename_lower or filename_lower.endswith(f"/{exe}")
                for exe in executables
            )

            # 如果没有精确匹配，则使用原来的模糊匹配方式
            if not exact_match:
                exact_match = (
                        app.lower().replace(" ", "-") in filename_lower or
                        app.lower() in filename_lower
                )

            app_flag.append((app, exact_match))

        special_flag = [x in filename_lower for x in ['/bin/', '/usr/bin/', '/snap/bin/']]
        main_app = [app[0] for app in app_flag if app[1]]
        is_bash_launch = (comm_lower == 'bash' and any(app[1] for app in app_flag))

        # logger.debug(f"[DEBUG] app_flag results: {app_flag}")
        # logger.debug(f"[DEBUG] special_flag: {special_flag}")
        # logger.debug(f"[DEBUG] main_app: {main_app}")
        # logger.debug(f"[DEBUG] is_bash_launch: {is_bash_launch}")

        if (any(special_flag) and any(app[1] for app in app_flag)) or is_bash_launch:
            result = True, main_app[0] if main_app else os.path.basename(filename)
            # logger.debug(f"[DEBUG] Returning True: {result}")
            return result

        # logger.debug("[DEBUG] Returning False")
        return False, ""

    def is_process_alive(self, pid):
        try:
            # 检查 /proc/[pid]/status 是否存在
            with open(f"/proc/{pid}/status") as f:
                return True
        except FileNotFoundError:
            return False


    def handle_exit_event(self, pid, app_id, app_name, old_comm, old_filename):
        """延迟检查进程是否真正退出"""
        if self.is_process_alive(pid):
            logger.debug(f"[Delay Check] PID={pid} still alive, not exiting normally.")
            return

        logger.debug(f"Monitored process terminated: PID={pid}, app={app_name}")
        app_utils.callback_manager.send_callback_notification({
            'app_id': app_id,
            'app_name': app_name,
            'status': "stopped",
            'purpose': "app"
        }, True)
        del self.monitored_app_launched[pid]

        # 清理 pending_exit_events
        if pid in self.pending_exit_events:
            del self.pending_exit_events[pid]


    def print_event(self, cpu: int, data: Any, size: int) -> None:
        event = self.bpf["events"].event(data)
        filename = event.filename.decode('utf-8', 'ignore')
        comm = event.comm.decode('utf-8', 'ignore')
        pid = event.pid
        type = event.type

        # logger.debug(f"*** Event: PID={pid}, type={type} COMM={comm}, FILENAME={filename} ***")

        if type == 0: # 启动事件
            is_main_process, app_name = self.get_main_process(comm, filename)
            # logger.debug(f"Is this filename main process? {is_main_process}, app_name={app_name}")
            if is_main_process:
                logger.debug(f"Is this filename main process? {is_main_process}, app_name={app_name}")
                # 防止重复处理同一个进程树
                if not self.is_process_handled(pid):
                    app_data = self._app_map_index.get(app_name.lower())
                    app_id, app_priority = app_data['app_id'], app_data.get('priority', 'low') if app_data else ("", "low")
                    logger.debug(f"launch: app_id={app_id}, app_name={app_name}, comm={comm}, filename={filename}")
                    self.monitored_app_launched[pid] = (app_id, app_name, comm, filename)
                    if app_priority.lower() == "critical":
                        app_utils.adjust_oom_priority(app_id, app_name, app_priority, app_data['cmdline'])
                        app_utils.callback_manager.send_callback_notification({
                            'app_id': app_id,
                            'app_name': app_name,
                            'status': "running",
                            'purpose': "app"
                        }, True)
                    else:
                        self.handle_monitored_app(pid, comm, filename, app_name, app_id)
                    self.mark_process_handled(pid)

        elif type == 1:  # 退出事件
            if pid not in self.monitored_app_launched:
                return

            # 如果已经有待处理的退出事件，取消旧的定时器
            if pid in self.pending_exit_events:
                self.pending_exit_events[pid].cancel()

            app_id, app_name, old_comm, old_filename = self.monitored_app_launched[pid]
            # logger.debug(f"Detected possible exit: PID={pid}, comm={comm}")

            # 延迟 1.5 秒后检查进程是否真正退出
            timer = Timer(1.5, self.handle_exit_event, args=[pid, app_id, app_name, old_comm, old_filename])
            self.pending_exit_events[pid] = timer
            timer.start()


    def is_process_handled(self, pid: int) -> bool:
        """检查该进程是否已经被处理过"""
        # 检查当前进程及其父进程是否已被处理
        try:
            process = psutil.Process(pid)
            for p in [process] + process.parents():
                if p.pid in self.handled_processes:
                    return True
        except psutil.NoSuchProcess:
            pass
        return False

    def mark_process_handled(self, pid: int) -> None:
        """标记进程为已处理"""
        self.handled_processes.add(pid)

    def handle_monitored_app(self, pid: int, comm: str, filename: str, app_name: str, app_id: str) -> None:
        logger.debug(f"Detected monitored app '{app_name}' (PID: {pid}, COMM: {comm}, FILE: {filename}, app_id: {app_id})")

        try:
            os.kill(pid, signal.SIGSTOP)
            # 检查系统资源get_current_pressure_level
            pressure, _ = self.controlManager.get_current_pressure_level()
            logger.debug(f"Current system pressure level: {pressure}")
            if pressure != "critical":
                os.kill(pid, signal.SIGCONT)
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, True)
            else:
                logger.info(f"System resources busy, skipping relaunch of {app_name}")
                app_utils.safe_notify("System resources busy", f"已暂停应用{app_name}启动", icon='dialog-warning')
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "pending",
                    'purpose': "app"
                }, True)
                app_utils.update_app_status(app_id, "pending")
                self.app_pending_queue.put(
                    {"pid": pid, "comm": comm, "filename": filename, "app_name": app_name, "app_id": app_id})

        except Exception as e:
            logger.debug(f"Error handling {app_name} (PID: {pid}): {str(e)}")

    def add_to_monitorlist(self, app_names: Union[str, List[str]]) -> None:
        """添加应用到监控列表（支持批量操作）"""
        if not app_names:
            return

        # 统一转为列表处理
        names = [app_names] if isinstance(app_names, str) else app_names

        # 转换为小写用于比较
        existing_lower = {name.lower() for name in self.monitored_apps}

        added_count = 0
        for name in names:
            if name.lower() not in existing_lower:
                self.monitored_apps.add(name)
                existing_lower.add(name.lower())  # 更新检查集
                added_count += 1
                logger.debug(f"Added '{name}' to monitoring list")

        if added_count == 0 and names:
            app_str = ', '.join(f"'{name}'" for name in names)
            logger.debug(f"All {len(names)} app(s) [{app_str}] already in monitoring list")
        elif added_count > 0:
            logger.debug(f"Successfully added {added_count}/{len(names)} new app(s)")

    def remove_from_monitorlist(self, app_name: str) -> None:
        """从监控列表中移除应用"""
        if app_name in self.monitored_apps:
            self.monitored_apps.remove(app_name)
            logger.debug(f"Removed '{app_name}' from monitoring list")
        else:
            logger.debug(f"'{app_name}' not found in monitoring list")

    def clear_monitorlist(self) -> None:
        """清空监控列表"""
        self.monitored_apps.clear()
        logger.debug("Cleared monitoring list")

    def get_monitored_apps(self) -> List[str]:
        """获取当前监控的应用列表"""
        return list(self.monitored_apps)

    def check_system_resources(self, cpu_threshold: int = 70, mem_threshold: int = 80) -> bool:
        """检查系统资源使用情况"""
        try:
            # 获取CPU使用率
            cpu_percent = psutil.cpu_percent(interval=1)

            # 获取内存使用率
            mem_percent = psutil.virtual_memory().percent

            logger.debug(f"System status - CPU: {cpu_percent}%, Memory: {mem_percent}%")

            # 检查是否低于阈值
            return cpu_percent < cpu_threshold and mem_percent < mem_threshold

        except Exception as e:
            logger.debug(f"Error checking system resources: {str(e)}")
            # 出现错误时默认允许启动
            return True


if __name__ == "__main__":
    # 初始化BPF
    bpf_monitor = AppIntercept()

    # 添加应用到监控列表
    bpf_monitor.add_to_monitorlist("firefox")
    bpf_monitor.add_to_monitorlist("Calculator")

    # 打开性能缓冲区
    bpf_monitor.bpf["events"].open_perf_buffer(bpf_monitor.print_event)
    logger.debug(f"Monitoring execve() for: {', '.join(bpf_monitor.get_monitored_apps())}")
    logger.debug("Ctrl+C to exit")

    while True:
        try:
            # 同时处理trace打印和事件
            bpf_monitor.bpf.perf_buffer_poll(timeout=100)
        except KeyboardInterrupt:
            logger.debug("\nExiting...")
            break
        except Exception as e:
            logger.debug(f"Error: {e}")
            break
