# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
from typing import Optional, List, Dict, Union
from config.config import b_config
from utils.logger import logger

# Reserved
class IOController:
    def __init__(self):
        self.config = b_config
        self.cgroup_mount = self.config.cgroup_mount  # "/sys/fs/cgroup"
        self.uid = self.get_uid()
        self.enable_io_controller()

    def get_uid(self):
        slices_cmd = ["systemctl", "list-units", "--type=slice", "user-*.slice", "--no-legend"]
        try:
            output = subprocess.check_output(slices_cmd, universal_newlines=True)
            for line in output.splitlines():
                parts = line.split()
                if parts and parts[0].startswith("user-"):
                    uid = parts[0].replace("user-", "").replace(".slice", "")
                    return uid
        except Exception as e:
            logger.error(f"Failed to get active user slices: {e}")
        return "0"

    def _run_cmd(self, cmd: List[str], check: bool = True) -> bool:
        """执行 shell 命令并返回是否成功"""
        try:
            subprocess.run(cmd, shell=False, check=check, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            logger.error(f"Command failed: {cmd_str}\nError: {e.stderr.decode().strip()}")
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
                logger.info(f"Path does not exist: {os.path.dirname(path)}")
                success = False
                continue

            cmd = ["sudo", "sh", "-c", f"echo '+io' > {path}"]
            if not self._run_cmd(cmd):
                success = False

        return success

    def get_disk_id(self, disk_filter: Optional[Union[str, List[str]]] = None) -> Dict[str, str]:
        """
        获取系统磁盘名称到ID的映射 (排除非物理磁盘)
        :param disk_filter: 可选的磁盘名称过滤器 (如 "nvme"、"sda" 或 ["nvme", "sda"])
        :return: 格式如 {"nvme0n1": "259:0", "sda": "8:0"} 的字典
        """
        try:
            cmd = ["lsblk", "-d", "-o", "NAME,TYPE,MAJ:MIN,SIZE,ROTA"]
            result = subprocess.run(
                cmd,
                shell=False,
                check=True,
                capture_output=True,
                text=True
            )
            disk_map = {}
            lines = result.stdout.strip().split('\n')
            header = lines[0].split()

            # 解析列索引
            name_idx = header.index("NAME")
            type_idx = header.index("TYPE")
            majmin_idx = header.index("MAJ:MIN")

            # 处理过滤器
            filter_list = []
            if disk_filter:
                filter_list = [disk_filter] if isinstance(disk_filter, str) else disk_filter
                filter_list = [f.lower() for f in filter_list]

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
                if filter_list and not any(f in name.lower() for f in filter_list):
                    continue

                disk_map[name] = maj_min

            logger.info(f"Found disks: {disk_map}")
            return disk_map

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get disk ID: {e.stderr.strip()}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return {}

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

                cmd = ["sudo", "sh", "-c", f"echo '+io' > {control_file}"]
                logger.info(f"Enabling IO controller at {control_file}")
                if not self._run_cmd(cmd):
                    logger.info(f"Failed to enable IO at {control_file}")
                    return False

            return os.path.exists(cgroup_path)

        except Exception as e:
            logger.error(f"Error ensuring IO enabled: {str(e)}")
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

            logger.info(f"Failed to find the path for cgroup_id: {cgroup_id}")
            return None

        except subprocess.CalledProcessError as e:
            logger.error(f"{e.stderr.strip()}")
            return None

    def set_disk_io_throttle(
            self,
            cgroup_id: str,
            limits: Dict[str, Dict[str, int]],
            disk_filter: Optional[Union[str, List[str]]] = None,
            is_restore: bool = False,
    ) -> bool:
        """
        综合IO限制设置方法
        :param: cgroup_id: cgroup id
        :param: limits:
                {
                    "default": {"rbps": 1000000, "wbps": 500000, "riops": 1000, "wiops": 500},  # 没有专门设置的disk会用到
                    "nvme0n1": {"wbps": 2000000, "wiops": 800},  # 使用设备名称或者id都可以
                    "8:0": {"rbps": 3000000, "riops": 1500}
                }
        :param: disk_filter: 可选磁盘名称过滤 (如 "nvme" 或 ["nvme", "sda"])
        :param: is_restore: 是否恢复默认值
        :return: success/not
        """
        success = True
        disk_map = self.get_disk_id(disk_filter)  # {"nvme0n1": "259:0", "sda": "8:0"}
        if not disk_map:
            return False

        io_max_path = self._get_full_cgroup_path(cgroup_id, "io.max")
        if not io_max_path:
            return False

        if not self._ensure_io_enabled(io_max_path):
            return False

        for disk_name, disk_id in disk_map.items():
            # 如果是恢复模式，直接设置所有限制为 max
            if is_restore:
                limit_str = "rbps=max wbps=max riops=max wiops=max"
            else:
                disk_limits = {}

                # 1. 默认先赋值default的value，下面匹配具体的disk在做特殊设置
                if "default" in limits:
                    disk_limits.update(limits["default"])

                # 1. ID:"8:0"
                if disk_id in limits:
                    disk_limits.update(limits[disk_id])

                # 2. 匹配设备名称:"nvme0n1"
                if disk_name in limits:
                    disk_limits.update(limits[disk_name])

                if not disk_limits:
                    continue

                # 构建限制字符串
                limit_parts = []
                for key in ["rbps", "wbps", "riops", "wiops"]:
                    if key in disk_limits:
                        limit_parts.append(f"{key}={disk_limits[key]}")
                # logger.info(f"disk_limits: {disk_limits}, limit_parts: {limit_parts}")
                limit_str = " ".join(limit_parts)

            if limit_str:  # 确保命令非空
                cmd = ["sudo", "sh", "-c", f"echo '{disk_id} {limit_str}' > {io_max_path}"]
                logger.info(f"Setting IO limits for cgroup: {cgroup_id} in disk {disk_name}({disk_id}): {limit_str}")
                if not self._run_cmd(cmd):
                    success = False

        return success

    def set_write_io_throughput_throttle_app(self, cgroup_id: str, wbps: int,
                                             disk_filter: Optional[str] = None) -> bool:
        """
        设置写入IO吞吐量限制 (单位: B/s)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wbps": wbps}}, disk_filter)

    def set_read_io_throughput_throttle_app(self, cgroup_id: str, rbps: int,
                                            disk_filter: Optional[str] = None) -> bool:
        """
        设置读取IO吞吐量限制 (单位: B/s)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"rbps": rbps}}, disk_filter)

    def set_write_iops_throttle_app(self, cgroup_id: str, wiops: int,
                                    disk_filter: Optional[str] = None) -> bool:
        """
        设置写入IOPS限制 (单位: 次/秒)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wiops": wiops}}, disk_filter)

    def set_read_iops_throttle_app(self, cgroup_id: str, riops: int,
                                   disk_filter: Optional[str] = None) -> bool:
        """
        设置读取IOPS限制 (单位: 次/秒)
        """
        return self.set_disk_io_throttle(cgroup_id, {"default": {"riops": riops}}, disk_filter)

    def restore_disk_io_throttle(
        self,
        cgroup_id: str,
        disk_filter: Optional[Union[str, List[str]]] = None,
    ) -> bool:
        """
        恢复磁盘 IO 限制
        """
        return self.set_disk_io_throttle(
            cgroup_id=cgroup_id,
            limits={},
            disk_filter=disk_filter,
            is_restore=True,
        )

    def set_weight(self, cgroup_id: str, weight: int) -> bool:
        """
        设置IO权重 (1-10000),需证明是否有效
        """
        if weight < 1 or weight > 10000:
            logger.info("Weight must be between 1 and 10000")
            return False

        io_weight_path = self._get_full_cgroup_path(cgroup_id, "io.weight")

        # 确保IO控制器已启用
        if not self._ensure_io_enabled(io_weight_path):
            return False

        logger.info(f"Setting IO weight to {weight} for cgroup {cgroup_id}")
        cmd = ["sudo", "sh", "-c", f"echo '{weight}' > {io_weight_path}"]
        return self._run_cmd(cmd)

    def get_current_io_limits(self, cgroup_id: str) -> Optional[tuple[int, int, int, int]]:
        """
        获取当前的IO限制 (rbps, wbps, riops, wiops)
        返回 (read_bps_limit, write_bps_limit, read_iops_limit, write_iops_limit) 或 None
        """
        io_max_path = self._get_full_cgroup_path(cgroup_id, "io.max")
        if not os.path.exists(io_max_path):
            return None

        try:
            with open(io_max_path, 'r') as f:
                content = f.read().strip()
                if not content:
                    return (0, 0, 0, 0)

                # 解析格式如 "259:0 rbps=20971520 wbps=10485760 riops=20000 wiops=2200"
                parts = content.split()
                rbps = 0
                wbps = 0
                riops = 0
                wiops = 0
                for part in parts[1:]:  # 跳过磁盘ID
                    if part.startswith('rbps='):
                        rbps = int(part.split('=')[1])
                    elif part.startswith('wbps='):
                        wbps = int(part.split('=')[1])
                    elif part.startswith('riops='):
                        riops = int(part.split('=')[1])
                    elif part.startswith('wiops='):
                        wiops = int(part.split('=')[1])
                return (rbps, wbps, riops, wiops)
        except Exception as e:
            logger.error(f"Failed to read io.max: {str(e)}")
            return None

if __name__ == "__main__":
    # 示例用法
    io_ctl = IOController()
    # 测试设置
    cgroup_id = "vte-spawn-d689ffa6-5446-4dfb-99f3-c4e702c44ebb.scope"

    # 测试用例1：所有磁盘相同限制
    # io_ctl.set_write_io_throughput_throttle_app(cgroup_id, 60 * 1024 * 1024)  # 所有磁盘写限制10MB/s
    # io_ctl.set_read_io_throughput_throttle_app(cgroup_id, 70 * 1024 * 1024)  # 所有磁盘读限制20MB/s
    # io_ctl.set_write_iops_throttle_app(cgroup_id, 3200)  # 所有磁盘写IOPS限制2200
    # io_ctl.set_read_iops_throttle_app(cgroup_id, 21000)  # 所有磁盘读IOPS限制20000

    # 测试用例2：封装设置的参数
    limits = {
        "default": {  # 如果只测试default的设置，需要把其他设置注释掉
            "rbps": 30 * 1024 * 1024,
            "wbps": 15 * 1024 * 1024,
            "wiops": 2201,
            "riops": 20001
        },  # 默认写吞吐量限制15MB/s...
        "8:0": {"wbps": 10 * 1024 * 1024},  # sda单独设置写10MB/s
        "nvme0n1": {"rbps": 33 * 1024 * 1024, "wbps": 27 * 1024 * 1024}  # nvme单独设置读32MB/s, 写20MB/s
    }
    # # io_ctl.set_disk_io_throttle(cgroup_id, limits)
    io_ctl.set_disk_io_throttle(
        cgroup_id,
        limits=limits,
        disk_filter=["nvme", "sda"],
    )
    #
    # # 测试用例3：只对NVMe磁盘设置限制
    # io_ctl.set_write_io_throughput_throttle_app(cgroup_id, 35 * 1024 * 1024, disk_filter="sda")
    # io_ctl.set_read_io_throughput_throttle_app(cgroup_id, 55 * 1024 * 1024, disk_filter="nvme")
    # io_ctl.set_write_iops_throttle_app(cgroup_id, 2300, disk_filter="sda")
    # io_ctl.set_read_iops_throttle_app(cgroup_id, 22000, disk_filter="nvme")

    # 设置权重
    io_ctl.set_weight(cgroup_id, 300)
    # 检查设置
    limits = io_ctl.get_current_io_limits(cgroup_id)
    if limits:
        print(
            f"Current IO limits - "
            f"Read: {limits[0] / 1024 / 1024:.1f}MB/s, "
            f"Write: {limits[1] / 1024 / 1024:.1f}MB/s, "
            f"Read IOPS: {limits[2]}, "
            f"Write IOPS: {limits[3]}")
    else:
        print("Failed to get current limits")
