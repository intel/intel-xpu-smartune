# sudo apt install libgirepository-2.0-dev libcairo2-dev
# conda: pip install PyGObject

from gi.repository import Gio
import os
import re

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
        # Case 1: Extract from --command=missioncenter
        match = re.search(r"--command=([^\s]+)", app_cmdline)
        if match:
            return match.group(1).lower()  # "missioncenter"

        # Case 2: Extract from Flatpak ID (e.g., "io.missioncenter.MissionCenter")
        last_part = app_cmdline.split()[-1]
        if "." in last_part:
            return last_part.split(".")[-1].lower()  # "missioncenter"

    # 3. Generic cases (e.g., "/usr/bin/gnome-calculator" or "firefox")
    for part in app_cmdline.split():
        # Skip flags, env vars, and placeholders
        if part.startswith(("-", "%", "env")):
            continue
        # Extract basename if path exists (e.g., "/usr/bin/foo" -> "foo")
        if "/" in part:
            return os.path.basename(part)
        # If no path (e.g., "firefox"), use as-is
        return part.lower()

    # 4. Fallback to app name (lowercase)
    return app_name.lower()


if __name__ == "__main__":
    # Example test cases for _get_executable_name
    test_cases = []
    apps = Gio.AppInfo.get_all()
    for app in apps:
        name = app.get_name() or "N/A"
        app_id = app.get_id() or "N/A"
        commandline = app.get_commandline() or "N/A"
        test_cases.append((name, commandline))
        print(f"{name:<30}   |  {app_id:<30}   | {commandline:<30}")

    for app_name, cmdline in test_cases:
        exe_name = _get_executable_name(app_name, cmdline)
        print(f"App Name: {app_name}, Cmdline: '{cmdline}' => Executable Name: '{exe_name}'")
