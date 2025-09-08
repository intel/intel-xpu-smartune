import os
import requests
import subprocess
import shutil
from getpass import getuser
from pwd import getpwnam

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

    def send_callback_notification(self, data: Dict[str, Any]) -> bool:
        """发送回调通知（线程安全）"""
        if not self._registered_url:
            raise ValueError("No callback URL registered")

        try:
            response = requests.post(
                self._registered_url,
                json=data,
                timeout=5
            )
            return response.status_code == 200 and response.json().get("status") == "ok"
        except Exception as e:
            raise ConnectionError(f"Callback failed: {str(e)}")


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
