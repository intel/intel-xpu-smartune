from bcc import BPF
import os
import signal
import subprocess
import time
from threading import Timer
from typing import List, Set, Dict, Any, Union
from gi.repository import Gio
import psutil
from multiprocessing import JoinableQueue
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
        self.bpf = BPF(src_file=c_src_file)
        self.controlManager = ControlManager()
        self.monitored_apps: Set[str] = set()
        self.handled_processes: Set[int] = set()  # 初始化已处理进程集合
        self.app_db = self.build_app_database()
        self.relaunch_apps = {}
        self.app_pending_queue = JoinableQueue(1000000)
        self.monitored_app_launched = {}  # 当前已经启动的监控app
        self.pending_exit_events = {}  # 待处理的退出事件（PID: Timer）

    def build_app_database(self) -> Dict[str, Dict[str, str]]:
        """构建桌面应用数据库"""
        db = {}
        for app in Gio.AppInfo.get_all():
            app_id = app.get_id()
            if app_id.endswith('.desktop'):
                db[app.get_name().lower()] = {
                    'app_id': app_id,
                    'command': app.get_commandline() or ''
                }
        return db

    def trace_print(self) -> None:
        self.bpf.trace_print()


    def get_main_process(self, comm: str, filename: str) -> (bool, str):
        """检查是否是主进程"""
        filename_lower = filename.lower()
        comm_lower = comm.lower()

        app_flag = [(app, app.lower() in filename_lower) for app in self.monitored_apps]
        special_flag = [x in filename_lower for x in ['/bin/', '/usr/bin/', '/snap/bin/']]
        main_app = [app[0] for app in app_flag if app[1]]
        is_bash_launch = (comm_lower == 'bash' and
                          any(app[1] for app in app_flag))

        if (any(special_flag) and any(app_flag[1] for app_flag in app_flag)) or is_bash_launch:
            return True, main_app[0] if main_app else os.path.basename(filename)

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
            print(f"[Delay Check] PID={pid} still alive, not exiting normally.")
            return

        print(f"Monitored process terminated: PID={pid}, app={app_name}")
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

        # print(f"*** Event: PID={pid}, type={type} COMM={comm}, FILENAME={filename} ***")

        if type == 0: # 启动事件
            is_main_process, app_name = self.get_main_process(comm, filename)
            # print(f"Is this filename main process? {is_main_process}, app_name={app_name}")
            if is_main_process:
                print(f"Is this filename main process? {is_main_process}, app_name={app_name}")
                # 防止重复处理同一个进程树
                if not self.is_process_handled(pid):
                    app_id = self.app_db.get(app_name.lower(), {}).get('app_id', '')
                    print(f"launch: app_id={app_id}, app_name={app_name}, comm={comm}, filename={filename}")
                    self.monitored_app_launched[pid] = (app_id, app_name, comm, filename)
                    self.handle_monitored_app(pid, comm, filename, app_name, app_id)
                    self.mark_process_handled(pid)

        elif type == 1:  # 退出事件
            if pid not in self.monitored_app_launched:
                return

            # 如果已经有待处理的退出事件，取消旧的定时器
            if pid in self.pending_exit_events:
                self.pending_exit_events[pid].cancel()

            app_id, app_name, old_comm, old_filename = self.monitored_app_launched[pid]
            # print(f"Detected possible exit: PID={pid}, comm={comm}")

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
        print(f"Detected monitored app '{app_name}' (PID: {pid}, COMM: {comm}, FILE: {filename}, app_id: {app_id})")

        try:
            os.kill(pid, signal.SIGSTOP)

            # 检查系统资源
            pressure = self.controlManager._get_current_pressure_level()
            print(f"Current system pressure level: {pressure}")
            if pressure != "critical":
                os.kill(pid, signal.SIGCONT)
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, True)
            else:
                print(f"System resources busy, skipping relaunch of {app_name}")
                app_utils.safe_notify("System resources busy", f"已暂停应用{app_name}启动，请前往应用控制中心操作", icon='dialog-warning')
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "pending",
                    'purpose': "app"
                }, True)
                self.app_pending_queue.put(
                    {"pid": pid, "comm": comm, "filename": filename, "app_name": app_name, "app_id": app_id})

        except Exception as e:
            print(f"Error handling {app_name} (PID: {pid}): {str(e)}")

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
                print(f"Added '{name}' to monitoring list")

        if added_count == 0 and names:
            print(f"All {len(names)} app(s) [{', '.join(f"'{name}'" for name in names)}] already in monitoring list")
        elif added_count > 0:
            print(f"Successfully added {added_count}/{len(names)} new app(s)")

    def remove_from_monitorlist(self, app_name: str) -> None:
        """从监控列表中移除应用"""
        if app_name in self.monitored_apps:
            self.monitored_apps.remove(app_name)
            print(f"Removed '{app_name}' from monitoring list")
        else:
            print(f"'{app_name}' not found in monitoring list")

    def clear_monitorlist(self) -> None:
        """清空监控列表"""
        self.monitored_apps.clear()
        print("Cleared monitoring list")

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

            print(f"System status - CPU: {cpu_percent}%, Memory: {mem_percent}%")

            # 检查是否低于阈值
            return cpu_percent < cpu_threshold and mem_percent < mem_threshold

        except Exception as e:
            print(f"Error checking system resources: {str(e)}")
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
    print(f"Monitoring execve() for: {', '.join(bpf_monitor.get_monitored_apps())}")
    print("Ctrl+C to exit")

    while True:
        try:
            # 同时处理trace打印和事件
            bpf_monitor.bpf.perf_buffer_poll(timeout=100)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")
            break
