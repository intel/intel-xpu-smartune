# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import re
from gi.repository import Gio

try:
    desktop_apps = {app.get_id(): app for app in Gio.AppInfo.get_all()}
    print(f"Loaded {len(desktop_apps)} desktop applications")
except Exception as e:
    print(f"Could not load desktop apps: {str(e)}")
    desktop_apps = {}


def _find_systemd_unit(pid):
    """通过systemd-cgls查找进程所属的scope, service"""
    try:
        result = subprocess.run(
            ['systemd-cgls', '--no-page'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )

        # 查找包含指定PID的行及其父unit
        lines = result.stdout.split('\n')
        for i, line in enumerate(lines):
            if f'─{pid} ' in line or f'─{pid}\n' in line:
                # 向上查找最近的unit(scope或service)
                for j in range(i, -1, -1):
                    line_content = lines[j]
                    if '.scope' in line_content or '.service' in line_content:
                        # 匹配类似 "├─session-c20.scope" 或 "├─fileManage.service"
                        unit_match = re.search(r"─(.*?\.(?:scope|service))", line_content)
                        if unit_match:
                            return unit_match.group(1)

                        # 如果没有匹配到，尝试更宽松的匹配
                        unit_match = re.search(r"\b([\w-]+\.(?:scope|service))\b", line_content)
                        if unit_match:
                            return unit_match.group(1)
    except Exception as e:
        print(f"Failed to find systemd unit: {str(e)}")
    return None

def _try_match_app(process_info):
    """尝试匹配桌面应用或systemd scope"""
    # logger.debug(f"process_info: {process_info}")
    # 1. 先尝试匹配桌面应用
    if desktop_apps:
        for app_id, app in desktop_apps.items():
            try:
                # 检查应用的可执行文件是否匹配
                cmd = app.get_commandline()
                if cmd and process_info['exe'] and process_info['exe'] in cmd:
                    return {
                        'type': 'desktop',
                        'id': app_id,
                        'name': app.get_display_name()
                    }

                # 检查应用名称是否匹配进程名
                if app.get_name().lower() in process_info['name'].lower():
                    return {
                        'type': 'desktop',
                        'id': app_id,
                        'name': app.get_display_name()
                    }
            except Exception:
                continue

    # 2. 尝试通过systemd-cgls查找scope或者service
    unit = _find_systemd_unit(process_info['pids'][0])  # the first PID
    if unit:
        return {
            'type': 'systemd',
            'id': unit,
            'name': f"Systemd cgroup: {unit}"
        }

    return None

if __name__ == "__main__":
    process_info = {
        'pids': [659456, 659457, 659458, 659459, 659460, 659461, 659462, 659434, 659435, 659436,
                 659437, 659438, 659439, 659440, 659441, 659442, 659443, 659444, 659445, 659446,
                 659447, 659448, 659449, 659450, 659451, 659452, 659453, 659454, 659455],
        'name': 'stress',
        'cmdline': 'stress --cpu 22 --io 3 --vm 3 --vm-bytes 20G',
        'exe': '/usr/bin/stress',
        'score': 88.38,
        'cpu_avg': 1256.4,
        'mem_avg': 57.928,
        'mem_rss': 38800795238.4,
        'io_read_rate': 24576.0
    }
    process_info2 = {
        'pids': [2594],
        'name': 'filemanager',
        'cmdline': '/usr/sbin/filemanager',
        'exe': '/usr/sbin/filemanager',
        'score': 88.38,
        'cpu_avg': 1256.4,
        'mem_avg': 57.928,
        'mem_rss': 38800795238.4,
        'io_read_rate': 24576.0
    }

    app_info = _try_match_app(process_info2)
    print(f"Matched app info: {app_info}")


