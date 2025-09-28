import os
import requests
import subprocess
import shutil
from getpass import getuser
from pwd import getpwnam
from datetime import datetime

from utils.logger import logger
from db.DatabaseModel import AIAppPriority
from typing import Optional, Dict, Any

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


def get_controlled_apps():
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        return [{
            "app_name": app.name,
            "app_id": app.app_id,
            "controlled": app.controlled,
            "priority": app.priority
        } for app in controlled_apps] if controlled_apps else None

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)
        return None


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

