from bcc import BPF
import os
import signal
import subprocess
import time
from typing import List, Set, Dict, Any
from gi.repository import Gio
import psutil

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
        self.monitored_apps: Set[str] = set()
        self.processes_to_relaunch: Dict[int, Dict[str, Any]] = {}  # 存储需要重启的进程信息
        self.handled_processes: Set[int] = set()  # 初始化已处理进程集合
        self.app_db = self.build_app_database()
        self.relaunch_apps = {}

    def build_app_database(self) -> Dict[str, Dict[str, str]]:
        """构建桌面应用数据库"""
        db = {}
        for app in Gio.AppInfo.get_all():
            desktop_id = app.get_id()
            if desktop_id.endswith('.desktop'):
                db[app.get_name().lower()] = {
                    'desktop_id': desktop_id,
                    'command': app.get_commandline() or ''
                }
        return db

    def trace_print(self) -> None:
        self.bpf.trace_print()

    def get_main_process(self, filename: str) -> (bool, str):
        """检查是否是主进程"""
        filename_lower = filename.lower()
        app_flag = [(app, app.lower() in filename_lower) for app in self.monitored_apps]
        special_flag = [x in filename_lower for x in ['/bin/', '/usr/bin/', '/snap/bin/']]
        main_app = [app[0] for app in app_flag if app[1]]
        if any(special_flag) and any(app_flag[1] for app_flag in app_flag):
            return True, main_app[0]
        return False, ""

    def print_event(self, cpu: int, data: Any, size: int) -> None:
        event = self.bpf["events"].event(data)
        filename = event.filename.decode('utf-8', 'ignore')
        comm = event.comm.decode('utf-8', 'ignore')
        pid = event.pid

        # 调试信息
        print(f"*** Event: PID={pid}, COMM={comm}, FILENAME={filename} ***")

        # 检测是否是有效的应用启动事件
        for app_name in self.monitored_apps:
            app_name_lower = app_name.lower()
            is_main_process = (
                app_name_lower in filename.lower() and
                any(x in filename.lower() for x in ['/bin/', '/usr/bin/', '/snap/bin/'])
            )

            print(f"app_name_lower: {app_name_lower}, is_main_process: {is_main_process}")

            if is_main_process:
                # 防止重复处理同一个进程树
                if not self.is_process_handled(pid):
                    desktop_id = self.app_db.get(app_name_lower, {}).get('desktop_id', '')
                    self.handle_monitored_app(pid, comm, filename, app_name, desktop_id)
                    self.mark_process_handled(pid)
                break
        # is_main_process, app_name = self.get_main_process(filename)
        # print(f"Is this filename main process? {is_main_process}, app_name={app_name}")
        # if is_main_process:
        #     # 防止重复处理同一个进程树
        #     if not self.is_process_handled(pid):
        #         desktop_id = self.app_db.get(app_name.lower(), {}).get('desktop_id', '')
        #         self.handle_monitored_app(pid, comm, filename, app_name, desktop_id)
        #         self.mark_process_handled(pid)

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

    def is_self_relaunched_process(self, app_name: str, desktop_id: str) -> bool:
        """

        :param app_name:
        :return:
        """
        return app_name in self.relaunch_apps or desktop_id in self.relaunch_apps

    def handle_monitored_app(self, pid: int, comm: str, filename: str, app_name: str, desktop_id: str) -> None:
        print(f"Detected monitored app '{app_name}' (PID: {pid}, COMM: {comm}, FILE: {filename}, desktop_id: {desktop_id})")

        try:
            # 检查是否是我们自己relaunch触发的进程
            if self.is_self_relaunched_process(app_name, desktop_id):
                print(f"Ignoring self-relaunched process: {app_name}: {desktop_id}")
                del self.relaunch_apps[desktop_id or app_name]  # 清理已处理的重启记录
                return

            os.kill(pid, signal.SIGSTOP)
            # 更温和的终止方式
            #self.graceful_terminate(pid, timeout=3)

            # 检查系统资源
            if self.check_system_resources():
                # 存储进程信息以便重启
                self.processes_to_relaunch[pid] = {
                    'desktop_id': desktop_id,
                    'comm': comm,
                    'filename': filename,
                    'detection_time': time.time(),
                    'app_name': app_name
                }
                # 延迟重启，避免频繁操作
                # time.sleep(1)
                os.kill(pid, signal.SIGCONT)
                #self.relaunch(desktop_id or app_name)
            else:
                print(f"System resources busy, skipping relaunch of {app_name}")

        except Exception as e:
            print(f"Error handling {app_name} (PID: {pid}): {str(e)}")

    def add_to_monitorlist(self, app_name: str) -> None:
        """添加应用到监控列表"""
        if app_name.lower() not in (name.lower() for name in self.monitored_apps):
            self.monitored_apps.add(app_name)
            print(f"Added '{app_name}' to monitoring list")
        else:
            print(f"'{app_name}' is already in monitoring list")

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

    def graceful_terminate(self, pid: int, timeout: int = 3) -> None:
        """更温和的进程终止方式"""
        try:
            process = psutil.Process(pid)
            # 先尝试发送SIGTERM
            process.terminate()

            # 等待进程结束
            try:
                process.wait(timeout=timeout)
            except psutil.TimeoutExpired:
                # 如果进程未响应，则发送SIGKILL
                process.kill()
                print(f"Force killed PID {pid} after timeout")

        except psutil.NoSuchProcess:
            print(f"Process {pid} already terminated")
        except Exception as e:
            print(f"Error terminating process {pid}: {str(e)}")

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


    def relaunch(self, app_name: str) -> bool:
        """通用的应用程序启动方式"""
        print(f"Attempting to relaunch: {app_name}")

        def try_launch(command, method_name):
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True
                )
                pid = proc.pid
                print(f"Attempted launch via {method_name} (PID: {pid})")
                self.relaunch_apps[app_name] = pid  # 记录重启的进程ID
                print(f"relaunch done: {self.relaunch_apps}")
                return True
            except FileNotFoundError:
                return False
            except Exception as e:
                print(f"Error with {method_name}: {str(e)}")
                return False

        def try_launch_by_system(command, method_name):
            try:
                print(f"command: {command}")
                os.system(command)
                pid = os.getpid()
                print(f"Attempted launch via {method_name} (PID: {pid})")
                self.relaunch_apps[app_name] = pid  # 记录重启的进程ID
                print(f"relaunch done: {self.relaunch_apps}")
                return True
            except FileNotFoundError:
                return False
            except Exception as e:
                print(f"Error with {method_name}: {str(e)}")
                return False

        try:
            # 1. 优先尝试gtk-launch (适用于.desktop文件)
            if app_name.endswith('.desktop'):
                if try_launch(["gtk-launch", app_name], "gtk-launch"):
                    print("gtk-launch successful")
                    return True

            # 2. 尝试直接执行
            if try_launch_by_system(app_name, "direct execution"):
                print("Direct execution successful")
                return True

            # 3. 尝试xdg-open
            if try_launch(["xdg-open", app_name], "xdg-open"):
                print("xdg-open successful")
                return True

            print(f"All launch methods failed for {app_name}")
            return False

        except Exception as e:
            print(f"Critical error relaunching {app_name}: {str(e)}")
            return False


# if __name__ == "__main__":
#     # 初始化BPF
#     bpf_monitor = AppIntercept()
#
#     # 添加应用到监控列表
#     bpf_monitor.add_to_monitorlist("firefox")
#     bpf_monitor.add_to_monitorlist("Calculator")
#
#     # 打开性能缓冲区
#     bpf_monitor.bpf["events"].open_perf_buffer(bpf_monitor.print_event)
#     print(f"Monitoring execve() for: {', '.join(bpf_monitor.get_monitored_apps())}")
#     print("Ctrl+C to exit")
#
#     while True:
#         try:
#             # 同时处理trace打印和事件
#             bpf_monitor.bpf.perf_buffer_poll(timeout=100)
#         except KeyboardInterrupt:
#             print("\nExiting...")
#             break
#         except Exception as e:
#             print(f"Error: {e}")
#             break
