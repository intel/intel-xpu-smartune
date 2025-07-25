# sudo apt install libgirepository-2.0-dev libcairo2-dev
# conda: pip install PyGObject

from gi.repository import Gio

apps = Gio.AppInfo.get_all()
for app in apps:
    print(f"{app.get_name():<30}   |  {app.get_id():<30}   | {app.get_commandline():<30}")