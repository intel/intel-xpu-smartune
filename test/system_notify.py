import os
import subprocess
from getpass import getuser
from pwd import getpwnam

def send_notification(title, message, icon="dialog-information"):
    try:
        # 方法1：优先尝试原生notify-send
        user = os.getenv("SUDO_USER") or getuser()
        print(f"Sending notification as user: {user}")

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

# 使用示例
send_notification("资源警告", "已暂停应用启动，请前往控制中心操作", "dialog-warning")
