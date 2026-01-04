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


import os
import subprocess
from subprocess import check_output
from typing import Optional, List, Dict
# from config.config import b_config
# from utils.logger import logger

# Reserved
class IOController:
    def __init__(self):
        # self.config = b_config
        self.cgroup_mount = "/sys/fs/cgroup"  # self.config.cgroup_mount
        self.uid = self.get_uid()
        self.enable_io_controller()

    def get_uid(self):
        # command used to get active user slices
        slices_cmd = "systemctl list-units user-*.slice | grep -oE 'user-[^ ]*.slice' || [ $? = 1 ]"

        active_user = check_output(slices_cmd, shell=True, universal_newlines=True).splitlines()
        if active_user:
            uid = active_user[0].strip('user-').strip('.slice')

        return uid

    def _run_cmd(self, cmd: str, check: bool = True) -> bool:
        """执行 shell 命令并返回是否成功"""
        try:
            subprocess.run(cmd, shell=True, check=check, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Command failed: {cmd}\nError: {e.stderr.decode().strip()}")
            return False

    def _check_file_exists(self, path: str) -> bool:
        """检查文件是否存在"""
        return os.path.exists(path)

    def enable_io_controller(self) -> bool:
        """
        启用IO控制器，逐级设置 cgroup.subtree_control
        返回是否全部设置成功
        """
        paths = [
            f"{self.cgroup_mount}/cgroup.subtree_control",
            f"{self.cgroup_mount}/user.slice/cgroup.subtree_control",
            f"{self.cgroup_mount}/user.slice/user-{self.uid}.slice/cgroup.subtree_control",
            f"{self.cgroup_mount}/user.slice/user-{self.uid}.slice/user@{self.uid}.service/cgroup.subtree_control",
            f"{self.cgroup_mount}/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/cgroup.subtree_control"
        ]

        success = True
        for path in paths:
            if not self._check_file_exists(os.path.dirname(path)):
                print(f"Path does not exist: {os.path.dirname(path)}")
                success = False
                continue

            cmd = f"sudo sh -c 'echo \"+io\" > {path}'"
            if not self._run_cmd(cmd):
                success = False

        return success

    def get_disk_id(self, disk_filter: Optional[str] = None) -> List[str]:
        """
        获取系统磁盘ID列表 (排除非物理磁盘)
        :param disk_filter: 可选的磁盘名称过滤器 (如 "nvme" 或 "sda")
        :return: 格式如 ["259:0", "8:0"] 的磁盘ID列表
        """
        try:
            cmd = "lsblk -d -o NAME,TYPE,MAJ:MIN,SIZE,ROTA"
            result = subprocess.run(
                cmd,
                shell=True,
                check=True,
                capture_output=True,
                text=True
            )

            disks = []
            lines = result.stdout.strip().split('\n')
            header = lines[0].split()

            # 解析列索引
            name_idx = header.index("NAME")
            type_idx = header.index("TYPE")
            majmin_idx = header.index("MAJ:MIN")

            for line in lines[1:]:
                if not line.strip():
                    continue

                parts = line.split()
                name = parts[name_idx]
                disk_type = parts[type_idx]
                maj_min = parts[majmin_idx]

                # 精确筛选物理磁盘
                if disk_type != "disk":
                    continue

                # 可选名称过滤
                if disk_filter and disk_filter.lower() not in name.lower():
                    continue

                disks.append(maj_min)

            print(f"Found disks: {disks}")
            return disks

        except subprocess.CalledProcessError as e:
            print(f"Failed to get disk ID: {e.stderr.strip()}")
            return []
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            return []

    def _ensure_io_enabled(self, cgroup_path: str) -> bool:
        """
        确保指定cgroup路径已启用IO控制器

        :param cgroup_path: 例如/sys/fs/cgroup/.../vte-spawn-xxx.scope/io.max
        :return :cgroup_path最终是否可用
        """
        # 如果cgroup_path已经存在，则直接可用
        if os.path.exists(cgroup_path):
            return True

        try:
            # 叶子节点不需要
            target_dir = os.path.dirname(os.path.dirname(cgroup_path))

            # 从cgroup挂载点开始构建路径组件
            components = []
            path = target_dir
            while path != self.cgroup_mount:
                path, component = os.path.split(path)
                components.append(component)
            components.reverse()

            current_path = self.cgroup_mount
            for comp in components:
                current_path = os.path.join(current_path, comp)
                control_file = os.path.join(current_path, "cgroup.subtree_control")

                if not os.path.exists(control_file):
                    continue

                with open(control_file, 'r') as f:
                    if 'io' in f.read().split():
                        continue

                cmd = f"sudo sh -c 'echo \"+io\" > {control_file}'"
                print(f"Enabling IO controller at {control_file}")
                if not self._run_cmd(cmd):
                    print(f"Failed to enable IO at {control_file}")
                    return False

            return os.path.exists(cgroup_path)

        except Exception as e:
            print(f"Error ensuring IO enabled: {str(e)}")
            return False

    def _get_full_cgroup_path(self, cgroup_id: str, file: str) -> Optional[str]:
        """
        查找 cgroup 路径
        """
        try:
            result = subprocess.run(
                ["find", self.cgroup_mount, "-name", cgroup_id, "-type", "d"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if result.stdout:
                base_path = result.stdout.split('\n')[0].strip()
                if base_path:
                    target_path = os.path.join(base_path, file)
                    return target_path

            print(f"Failed to find the path for cgroup_id: {cgroup_id}")
            return None

        except subprocess.CalledProcessError as e:
            print(f"{e.stderr.strip()}")
            return None

    def set_disk_io_throttle(self, cgroup_id: str, limits: Dict[str, Dict[str, int]],
                             disk_filter: Optional[str] = None, is_restore: bool = False) -> bool:
        """
        综合IO限制设置方法
        :param: cgroup_id: cgroup id
        :param: limits:
                {
                    "default": {"rbps": 1000000, "wbps": 500000},  # 默认值（可选）
                    "8:0": {"wbps": 2000000},  # 特定磁盘覆盖设置
                    "259:0": {"rbps": 3000000}
                }
        :param: disk_filter: 可选磁盘名称过滤 (如 "nvme")
        :return: success/not
        """
        success = True

        disk_ids = self.get_disk_id(disk_filter)
        if not disk_ids:
            return False

        io_max_path = self._get_full_cgroup_path(cgroup_id, "io.max")
        if not io_max_path:
            return False

        if not self._ensure_io_enabled(io_max_path):
            return False

        for disk_id in disk_ids:
            disk_limits = limits.get(disk_id, limits.get("default", {}))
            if not disk_limits:
                continue

            # 为当前设备构建限制命令
            limit_parts = []
            if "wbps" in disk_limits:
                limit_parts.append(f"wbps={disk_limits['wbps'] if not is_restore else 'max'}")
            if "rbps" in disk_limits:
                limit_parts.append(f"rbps={disk_limits['rbps'] if not is_restore else 'max'}")

            if limit_parts:
                limit_str = " ".join(limit_parts)
                cmd = f"sudo sh -c 'echo \"{disk_id} {limit_str}\" > {io_max_path}'"
                print(f"Setting IO limits for cgroup {cgroup_id}:\n: {disk_id} {limit_str} to {io_max_path}")
                if not self._run_cmd(cmd):
                    success = False

        return success

    def set_write_io_throttle_app(self, cgroup_id: str, wbps: int,
                                  disk_filter: Optional[str] = None) -> bool:
        """
        设置写入IO限制 (单位: bytes/s)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wbps": wbps}}, disk_filter)

    def set_read_io_throttle_app(self, cgroup_id: str, rbps: int,
                                 disk_filter: Optional[str] = None) -> bool:
        """
        设置读取IO限制 (单位: B/s)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"rbps": rbps}}, disk_filter)

    def restore_write_io_throttle_app(self, cgroup_id: str,
                                  disk_filter: Optional[str] = None) -> bool:
        """
        恢复写入IO限制
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wbps": 0}}, disk_filter, is_restore=True)

    def restore_read_io_throttle_app(self, cgroup_id: str,
                                 disk_filter: Optional[str] = None) -> bool:
        """
        恢复读取IO限制
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"rbps": 0}}, disk_filter, is_restore=True)

    def set_weight(self, cgroup_id: str, weight: int) -> bool:
        """
        设置IO权重 (1-10000)
        """
        if weight < 1 or weight > 10000:
            print("Weight must be between 1 and 10000")
            return False

        io_weight_path = self._get_full_cgroup_path(cgroup_id, "io.weight")

        # 确保IO控制器已启用
        if not self._ensure_io_enabled(io_weight_path):
            return False

        print(f"Setting IO weight to {weight} for cgroup {cgroup_id}")
        cmd = f"sudo sh -c 'echo \"{weight}\" > {io_weight_path}'"
        return self._run_cmd(cmd)

    def get_current_io_limits(self, cgroup_id: str) -> Optional[tuple[int, int]]:
        """
        获取当前的IO限制 (rbps, wbps)
        返回 (read_limit, write_limit) 或 None
        """
        io_max_path = self._get_full_cgroup_path(cgroup_id, "io.max")
        if not os.path.exists(io_max_path):
            return None

        try:
            with open(io_max_path, 'r') as f:
                content = f.read().strip()
                if not content:
                    return (0, 0)

                # 解析格式如 "259:0 rbps=20971520 wbps=10485760"
                parts = content.split()
                rbps = 0
                wbps = 0
                for part in parts[1:]:  # 跳过磁盘ID
                    if part.startswith('rbps='):
                        rbps = int(part.split('=')[1])
                    elif part.startswith('wbps='):
                        wbps = int(part.split('=')[1])
                return (rbps, wbps)
        except Exception as e:
            print(f"Failed to read io.max: {str(e)}")
            return None

if __name__ == "__main__":
    # 示例用法
    io_ctl = IOController()
    # 测试设置
    cgroup_id = "vte-spawn-5fedc730-30f8-4ee1-a973-174e151ea8dd.scope"

    # 测试用例1：（所有磁盘相同限制）
    # io_ctl.set_write_io_throttle_app(cgroup_id, 10 * 1024 * 1024)  # 所有磁盘写限制10MB/s
    # io_ctl.set_read_io_throttle_app(cgroup_id, 20 * 1024 * 1024)  # 所有磁盘读限制20MB/s

    # 测试用例2：新版高级用法
    # limits = {
    #     "default": {"wbps": 15 * 1024 * 1024},  # 默认写限制15MB/s
    #     "8:0": {"wbps": 10 * 1024 * 1024},  # sda单独设置写10MB/s
    #     "259:0": {"rbps": 30 * 1024 * 1024, "wbps": 5 * 1024 * 1024}  # nvme单独设置读30MB/s, 写5MB/s
    # }
    # io_ctl.set_disk_io_throttle(cgroup_id, limits)
    #
    # # 测试用例3：只对NVMe磁盘设置限制
    io_ctl.set_write_io_throttle_app(cgroup_id, 25 * 1024 * 1024, disk_filter="nvme")
    io_ctl.set_read_io_throttle_app(cgroup_id, 50 * 1024 * 1024, disk_filter="nvme")

    # 设置权重
    io_ctl.set_weight(cgroup_id, 300)
    # 检查设置
    limits = io_ctl.get_current_io_limits(cgroup_id)
    if limits:
        print(
            f"Current IO limits - Read: {limits[0] / 1024 / 1024:.1f}MB/s, Write: {limits[1] / 1024 / 1024:.1f}MB/s")
    else:
        print("Failed to get current limits")
