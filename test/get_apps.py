# sudo apt install libgirepository-2.0-dev libcairo2-dev
# conda: pip install PyGObject

from gi.repository import Gio

apps = Gio.AppInfo.get_all()
for app in apps:
    name = app.get_name() or "N/A"
    app_id = app.get_id() or "N/A"
    commandline = app.get_commandline() or "N/A"
    print(f"{name:<30}   |  {app_id:<30}   | {commandline:<30}")