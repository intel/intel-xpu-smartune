#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import psutil
import subprocess
import time

def get_disk_utilization(device='nvme0n1', interval=1):
    with open('/proc/diskstats') as f:
        for line in f:
            if device in line:
                fields = line.strip().split()
                io_ticks = int(fields[13])  # 第 14 列：io_ticks（毫秒）
                return io_ticks
    return 0


def disk_utilization(device='nvme0n1', interval=1):
    cnt1 = psutil.disk_io_counters(perdisk=True).get(device)
    time.sleep(interval)
    cnt2 = psutil.disk_io_counters(perdisk=True).get(device)
    if not cnt1 or not cnt2:
        return 0.0
    delta_time = interval * 1000  # 毫秒
    busy_time = (cnt2.read_time - cnt1.read_time) + (cnt2.write_time - cnt1.write_time)
    return min(100.0, 100 * busy_time / delta_time)

def get_system_disk():
    for part in psutil.disk_partitions():
        if part.mountpoint == "/":  # Linux 根目录（Windows 需改为 "C:\\"）
            return part.device.split("/")[-1].rstrip("0123456789")  # 提取设备名（如 nvme0n1）
    return None

def get_physical_disks():
    cmd = "lsblk -d -o NAME,TYPE -n | awk '$2 == \"disk\" {print $1}'"  # 仅 TYPE=disk
    output = subprocess.check_output(cmd, shell=True, text=True).strip()
    return output.splitlines() if output else []


if __name__ == "__main__":
    # 第一次采样
    io1 = get_disk_utilization(device='nvme0n1')
    time1 = time.time()
    time.sleep(1)  # 采样间隔 1 秒
    # 第二次采样
    io2 = get_disk_utilization(device='nvme0n1')
    time2 = time.time()

    delta_io_ticks = io2 - io1
    delta_time_ms = (time2 - time1) * 1000  # 实际时间差（毫秒）
    util_percent = min(100.0, 100 * delta_io_ticks / delta_time_ms)  # 限制到 100%

    print(f"Disk utilization: {util_percent:.1f}%")
    print(f"Disk utilization: {disk_utilization('nvme0n1'):.1f}%")

    disk_devices = psutil.disk_io_counters(perdisk=True).keys()
    print("Available disk devices:", list(disk_devices))

    device = get_system_disk()  # 例如 'nvme0n1'
    print("System disk device:", device)

    physical_disks = get_physical_disks()  # 例如 ['nvme0n1']
    device = physical_disks[0] if physical_disks else None
    print(f"Physical disks: {physical_disks}, using device: {device}")

