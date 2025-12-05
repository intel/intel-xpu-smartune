import os
import re
import requests
import subprocess
import shutil
import psutil
from getpass import getuser
from pwd import getpwnam
from datetime import datetime

from utils.logger import logger
from db.DatabaseModel import AIAppPriority
from typing import Optional, Dict, Any
from config.config import b_config
from gi.repository import Gio

_original_oom_scores: dict[str, str] = {}

class ClientCallbackManager:
    """管理客户端回调的全局状态和操作"""
    _instance = None
    _registered_url: Optional[str] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def callback_url(self) -> Optional[str]:
        return self._registered_url

    def register_callback_url(self, url: str) -> None:
        """注册全局回调地址"""
        self._registered_url = url

    def send_callback_notification(self, data: Dict[str, Any], store=False) -> bool:
        """发送回调通知（线程安全）"""
        if not self._registered_url:
            print("No callback URL registered.")
            return False

        if store:
            try:
                result = AIAppPriority.update_record(
                    id=data['app_id'].replace('.desktop', ''),
                    status=data['status'],
                    up_time=datetime.now()
                )
                if not result:
                    print(f"Warning: Failed to update database record for {data['app_id']}")
            except Exception as db_error:
                print(f"Database update error: {db_error}")

        try:
            response = requests.post(
                self._registered_url,
                json=data,
                timeout=5
            )
            return response.status_code == 200 and response.json().get("status") == "ok"
        except Exception as e:
            print(f"Callback notification failed: {str(e)}")
            return False


# 单例实例
callback_manager = ClientCallbackManager()

def get_cgroup_path_by_pid(pid):
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 3:
                    # cgroup v2: 0::<path>
                    return parts[2]
    except Exception:
        pass
    return None
def get_controlled_apps_config(apps_dict=None):
    if apps_dict is None:
        apps_dict = {}
    # 配置文件 controlled_apps，补充数据库没有的项
    if hasattr(b_config, 'testing_network_app') and b_config.testing_network_app:
        for app in b_config.testing_network_app:
            app_name = app.get("app_name")
            app_id = app.get("app_cgroup")
            priority = app.get("priority")
            try:
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    if app_name and app_name.lower() in proc.name().lower():
                        cg_path = get_cgroup_path_by_pid(proc.pid)
                        if cg_path and app_id in cg_path:
                            if app_id not in apps_dict:
                                apps_dict[app_id] = {
                                    "app_name": app_name,
                                    "app_id": app_id,
                                    "priority": priority,
                                    "pid": proc.pid,
                                    "cgroup_path": cg_path,
                                }
                            break
            except Exception as e:
                logger.error(f"Error processing app {app_name}: {str(e)}", exc_info=True)
                continue

def get_controlled_apps_net():
    apps_dict = {}
    # 1. 先查数据库 controlled_apps，优先使用数据库
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        for app in controlled_apps:
            app_name = getattr(app, "name", None)
            app_id = getattr(app, "app_id", None)
            priority = getattr(app, "priority", None)
            cmdline = getattr(app, "cmdline", None)
            pid = None
            cgroup_path = None
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                if app_name and app_name.lower() in proc.name().lower():
                    cg_path = get_cgroup_path_by_pid(proc.pid)
                    if cg_path and app_id in cg_path:
                        apps_dict[app_id] = {
                            "app_name": app_name,
                            "app_id": app_id,
                            "priority": priority,
                            "pid": proc.pid,
                            "cgroup_path": cg_path,
                        }
                        break
    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)

    get_controlled_apps_config(apps_dict)
    # 3. 返回合并后的列表
    return list(apps_dict.values()) if apps_dict else None

def get_controlled_apps():
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        return [{
            "app_name": app.name,
            "app_id": app.app_id,
            "controlled": app.controlled,
            "priority": app.priority,
            "cmdline": app.cmdline,
        } for app in controlled_apps] if controlled_apps else None

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)
        return None

def _get_executable_name(app_name, app_cmdline):
    if not app_cmdline:
        return app_name.lower()

    # 1. Handle Snap apps (e.g., "/snap/bin/firefox %u")
    if "/snap/bin/" in app_cmdline:
        for part in app_cmdline.split():
            if "/snap/bin/" in part:
                return os.path.basename(part)  # "firefox"

    # 2. Handle Flatpak apps (e.g., "flatpak run --command=missioncenter ...")
    if "flatpak run" in app_cmdline:
        match = re.search(r"--command=([^\s]+)", app_cmdline)
        if match:
            return match.group(1).lower()  # "missioncenter"
        last_part = app_cmdline.split()[-1]
        if "." in last_part:
            return last_part.split(".")[-1].lower()

    # 3. Generic cases (e.g., "/usr/bin/foo")
    for part in app_cmdline.split():
        # Skip flags, env vars, and placeholders
        if part.startswith(("-", "%", "env")):
            continue

        if "/" in part:
            return os.path.basename(part)
        # If no path (e.g., "firefox"), use as-is
        return part.lower()

    return app_name.lower()


def adjust_oom_priority(
    app_id: str,
    app_name: str,
    priority: str,
    app_cmdline: str,
    restore: bool = False,
) -> None:
    """
    调整或恢复应用的 OOM 优先级（oom_score_adj）, 主要目的是保活一些特殊的critical的应用
    :param app_id:
    :param app_name:
    :param priority: 仅当为 "critical" 时生效
    :param app_cmdline: 用于 pgrep 匹配
    :param restore: 若为 True，则恢复原始值；否则根据 priority 设置
    :return:
    """
    if not restore and priority.lower() != "critical":
        return  # 非 critical 应用且不强制恢复时跳过

    target_value = 0
    try:
        exe_name = _get_executable_name(app_name, app_cmdline)
        logger.debug(f"Target executable: {exe_name}")

        pgrep_result = subprocess.run(
            ["pgrep", "-f", exe_name],
            capture_output=True,
            text=True,
        )
        if pgrep_result.returncode != 0:
            logger.debug(f"App {app_name} is not running and no OOM adjustment needed.")
            return

        pids = [pid for pid in pgrep_result.stdout.strip().split("\n") if pid]
        for pid in pids:
            oom_file = f"/proc/{pid}/oom_score_adj"

            if restore:
                if pid not in _original_oom_scores:
                    logger.warning(f"No original OOM score recorded for PID {pid}. Skipping.")
                    continue
                target_value = _original_oom_scores.pop(pid)
                action = "Restoring"
            else:
                # 记录app的默认值
                if pid not in _original_oom_scores:
                    with open(oom_file, "r") as f:
                        _original_oom_scores[pid] = f.read().strip()
                target_value = "-1000"
                action = "Setting"

            # 修改 oom_score_adj
            logger.debug(f"{action} OOM priority for PID {pid} to {target_value}")
            base_cmd = ["tee", oom_file]
            cmd = ["sudo", *base_cmd] if getattr(b_config, "vendor", "") == "generic" else base_cmd
            subprocess.run(
                cmd,
                input=target_value,
                text=True,
                check=True,
            )

        _update_app_oom_score_adj(app_id, int(target_value))
        logger.info(f"OOM priority updated for {app_name} (PID(s): {', '.join(pids)})")

    except Exception as e:
        logger.error(f"Failed to adjust OOM priority for {app_name}: {e}")


def _update_app_oom_score_adj(app_id: str, score: int) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id.replace('.desktop', ''),
            oom_score=score
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"oom_score_adj updated - ID: {app_id}, New score: {score}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def update_app_status(app_id: str, status: str) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id.replace('.desktop', ''),
            status=status
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"Status updated - ID: {app_id}, New status: {status}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def get_app_resource_usage(app_id: str, app_name: str) -> dict:
    """查询特定桌面应用程序的实际CPU、内存和IO使用情况

    Args:
        app_id: 应用程序的.desktop ID (如 org.gnome.Calculator.desktop)
        app_name: 应用程序的名称 (如 Calculator)

    Returns:
        包含资源使用情况的字典，格式为:
        {
            'pids': list,          # 进程ID列表
            'name': str,           # 进程名称
            'cpu_percent': float,  # CPU使用百分比
            'mem_bytes': int,      # 内存使用字节数
            'io_read_bytes': int,  # IO读取字节数
            'io_write_bytes': int  # IO写入字节数
        }
    """
    try:
        # 获取所有进程信息
        processes = []
        pids = []
        proc_name = None
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info', 'io_counters']):
            try:
                # 检查进程是否匹配应用程序名称或.desktop文件
                if app_name.lower() in proc.name().lower():
                    processes.append(proc)
                    pids.append(proc.pid)
                    proc_name = proc.name()
                else:
                    # 检查命令行是否包含.desktop文件信息
                    cmdline = " ".join(proc.cmdline())
                    if app_id.lower() in cmdline.lower():
                        processes.append(proc)
                        pids.append(proc.pid)
                        proc_name = proc.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not processes:
            print(f"No processes found for app {app_name} (ID: {app_id})")
            return {}

        # 计算总资源使用量
        cpu_percent = sum(proc.cpu_percent() for proc in processes)
        mem_bytes = sum(proc.memory_info().rss for proc in processes)
        io_read_bytes = sum(proc.io_counters().read_bytes for proc in processes if proc.io_counters() is not None)
        io_write_bytes = sum(proc.io_counters().write_bytes for proc in processes if proc.io_counters() is not None)

        return {
            'pids': pids,
            'name': proc_name if proc_name else app_name,
            'cpu_percent': cpu_percent,
            'mem_bytes': mem_bytes,
            'io_read_bytes': io_read_bytes,
            'io_write_bytes': io_write_bytes
        }
    except Exception as e:
        print(f"Error getting resource usage for {app_name} (ID: {app_id}): {e}")
        return {}


def safe_notify(title, message, icon="dialog-information"):
    try:
        # 方法1：优先尝试原生notify-send
        user = os.getenv("SUDO_USER") or getuser()

        user_uid = getpwnam(user).pw_uid

        # 构建正确的DBus地址
        dbus_address = f'unix:path=/run/user/{user_uid}/bus'

        # 使用sudo -u切换用户身份执行
        subprocess.run([
            'sudo', '-u', user,
            f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
            'DISPLAY=:0',
            'notify-send',
            f'--icon={icon}',
            title,
            message
        ], check=True)

    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            # 方法2：使用zenity作为后备方案
            subprocess.run(
                ["zenity", "--info", "--text", f"{title}\n{message}", "--title", "系统通知"],
                check=True
            )
        except:
            print(f"\a⚠️ {title}: {message}")


def get_dbus_address():
    """动态获取当前用户的DBus地址"""
    uid = os.getuid()

    # 方法1：检查标准路径
    standard_path = f"/run/user/{uid}/bus"
    if os.path.exists(standard_path):
        return f"unix:path={standard_path}"

    # 方法2：从进程环境获取
    try:
        import psutil
        for proc in psutil.process_iter(['environ']):
            try:
                env = proc.environ()
                if 'DBUS_SESSION_BUS_ADDRESS' in env:
                    return env['DBUS_SESSION_BUS_ADDRESS']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass

    # 方法3：通过loginctl获取
    try:
        cmd = ["loginctl", "show-user", str(uid), "--property=Display"]
        display = subprocess.check_output(cmd).decode().strip()
        if display:
            return f"unix:path=/run/user/{uid}/bus"
    except:
        pass

    return None


def fetch_all_apps():
    app_list = []
    if hasattr(b_config, 'all_apps'):
        apps = b_config.all_apps
        for app in apps:
            app_data = {
                "name": app["name"],
                "app_id": app["id"],
                "cmdline": app["commandline"],
                "display_name": app["name"]
            }
            app_list.append(app_data)
    else:
        apps = Gio.AppInfo.get_all()
        for app in apps:
            app_data = {
                "name": app.get_name(),  # Calculator
                "app_id": app.get_id(),  # org.gnome.Calculator.desktop
                "cmdline": app.get_commandline() or "",  # gnome-calculator
                "display_name": app.get_display_name()
            }
            app_list.append(app_data)
    return app_list
