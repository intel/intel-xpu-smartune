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
from utils.app_utils import build_sudo_shell_redirect

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

    def _run_cmd(self, cmd, check: bool = True, log_on_fail: bool = True) -> bool:
        """Execute a shell command and return True on success."""
        try:
            subprocess.run(cmd, shell=False, check=check, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            if log_on_fail:
                cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
                logger.error(f"Command failed: {cmd_str}\nError: {e.stderr.decode().strip()}")
            return False

    def _check_file_exists(self, path: str) -> bool:
        """Return True if the given path exists."""
        return os.path.exists(path)

    def enable_io_controller(self) -> bool:
        """
        Enable the IO controller by setting cgroup.subtree_control at each cgroup level.
        Returns True only if all levels were configured successfully.
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

            cmd = build_sudo_shell_redirect("+io", path)
            if not self._run_cmd(cmd):
                success = False

        return success

    def get_disk_id(self, disk_filter: Optional[Union[str, List[str]]] = None) -> Dict[str, str]:
        """
        Return a mapping of disk name to maj:min device ID (physical disks only).
        :param disk_filter: Optional name filter, e.g. "nvme" or ["nvme", "sda"]
        :return: dict like {"nvme0n1": "259:0", "sda": "8:0"}
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

            # Parse column indices from header
            name_idx = header.index("NAME")
            type_idx = header.index("TYPE")
            majmin_idx = header.index("MAJ:MIN")

            # Build the filter list
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

                # Only include physical disks
                if disk_type != "disk":
                    continue

                # Optional name filter
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
        Ensure the IO controller is enabled for the cgroup at the given path.

        :param cgroup_path: e.g. /sys/fs/cgroup/.../vte-spawn-xxx.scope/io.max
        :return: True if cgroup_path is usable after enabling

        Always walks every ancestor cgroup.subtree_control even if io.max
        already appears to exist: systemd's `set-property ... IOWeight=` (clear)
        can detach io from a parent's subtree_control while the child's io.max
        is still transiently visible — writing it then returns EACCES. Verifying
        subtree_control unconditionally re-adds +io in that window.
        """
        try:
            # Leaf node does not need a subtree_control entry
            target_dir = os.path.dirname(os.path.dirname(cgroup_path))

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

                cmd = build_sudo_shell_redirect("+io", control_file)
                logger.info(f"Enabling IO controller at {control_file}")
                if not self._run_cmd(cmd):
                    logger.info(f"Failed to enable IO at {control_file}")
                    return False

            return os.path.exists(cgroup_path)

        except Exception as e:
            logger.error(f"Error ensuring IO enabled: {str(e)}")
            return False

    def _get_full_cgroup_path(self, cgroup_id: str, file: str) -> Optional[str]:
        """Locate the full cgroup directory path for cgroup_id and return the path to file."""
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
        General-purpose disk IO throttle setter.
        :param cgroup_id: cgroup identifier
        :param limits: per-disk limit map, e.g.:
                {
                    "default": {"rbps": 1000000, "wbps": 500000, "riops": 1000, "wiops": 500},
                    "nvme0n1": {"wbps": 2000000, "wiops": 800},  # device name or maj:min both work
                    "8:0": {"rbps": 3000000, "riops": 1500}
                }
        :param disk_filter: optional disk name filter (e.g. "nvme" or ["nvme", "sda"])
        :param is_restore: when True, reset all limits to max
        :return: True if all operations succeeded
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
            # In restore mode, reset all limits to max
            if is_restore:
                limit_str = "rbps=max wbps=max riops=max wiops=max"
            else:
                disk_limits = {}

                # 1. Start with the "default" limits, then apply per-disk overrides
                if "default" in limits:
                    disk_limits.update(limits["default"])

                # Match by maj:min device ID
                if disk_id in limits:
                    disk_limits.update(limits[disk_id])

                # Match by device name
                if disk_name in limits:
                    disk_limits.update(limits[disk_name])

                if not disk_limits:
                    continue

                # Build the limit string
                limit_parts = []
                for key in ["rbps", "wbps", "riops", "wiops"]:
                    if key in disk_limits:
                        limit_parts.append(f"{key}={disk_limits[key]}")
                # logger.info(f"disk_limits: {disk_limits}, limit_parts: {limit_parts}")
                limit_str = " ".join(limit_parts)

            if limit_str:  # skip if nothing to apply
                if not os.path.exists(io_max_path):
                    if is_restore:
                        # Controller already detached — limits are gone.
                        continue
                    logger.error(
                        f"io.max not found for cgroup {cgroup_id} (disk {disk_name}), cannot apply limits"
                    )
                    success = False
                    continue

                cmd = build_sudo_shell_redirect(f"{disk_id} {limit_str}", io_max_path)
                logger.info(f"Setting IO limits for cgroup: {cgroup_id} in disk {disk_name}({disk_id}): {limit_str}")

                # Any `systemctl set-property` elsewhere (e.g. CPU/mem restore) can
                # asynchronously detach `io` from a parent's cgroup.subtree_control —
                # systemd doesn't know we set io.max directly, so it thinks the unit
                # has no IO constraints once CPUQuota=/MemoryHigh= are cleared. The
                # write then returns EACCES even though our pre-flight check passed.
                # Re-run _ensure_io_enabled (re-adds +io) and retry once.
                if not self._run_cmd(cmd, log_on_fail=False):
                    if self._ensure_io_enabled(io_max_path) and self._run_cmd(cmd):
                        continue
                    logger.error(
                        f"Failed to write io.max for cgroup {cgroup_id} (disk {disk_name}) "
                        f"after retry"
                    )
                    success = False

        return success

    def set_write_io_throughput_throttle_app(self, cgroup_id: str, wbps: int,
                                             disk_filter: Optional[str] = None) -> bool:
        """Set write IO throughput limit in bytes per second."""
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wbps": wbps}}, disk_filter)

    def set_read_io_throughput_throttle_app(self, cgroup_id: str, rbps: int,
                                            disk_filter: Optional[str] = None) -> bool:
        """Set read IO throughput limit in bytes per second."""
        return self.set_disk_io_throttle(cgroup_id, {"default": {"rbps": rbps}}, disk_filter)

    def set_write_iops_throttle_app(self, cgroup_id: str, wiops: int,
                                    disk_filter: Optional[str] = None) -> bool:
        """Set write IOPS limit (operations per second)."""
        return self.set_disk_io_throttle(cgroup_id, {"default": {"wiops": wiops}}, disk_filter)

    def set_read_iops_throttle_app(self, cgroup_id: str, riops: int,
                                   disk_filter: Optional[str] = None) -> bool:
        """Set read IOPS limit (operations per second)."""
        return self.set_disk_io_throttle(cgroup_id, {"default": {"riops": riops}}, disk_filter)

    def restore_disk_io_throttle(
        self,
        cgroup_id: str,
        disk_filter: Optional[Union[str, List[str]]] = None,
    ) -> bool:
        """Restore disk IO limits to unrestricted (max) values."""
        return self.set_disk_io_throttle(
            cgroup_id=cgroup_id,
            limits={},
            disk_filter=disk_filter,
            is_restore=True,
        )

    def set_weight(self, cgroup_id: str, weight: int) -> bool:
        """Set IO weight (1–10000) for the given cgroup."""
        if weight < 1 or weight > 10000:
            logger.warning("IO weight must be between 1 and 10000")
            return False

        io_weight_path = self._get_full_cgroup_path(cgroup_id, "io.weight")

        # Ensure the IO controller is enabled
        if not self._ensure_io_enabled(io_weight_path):
            return False

        logger.info(f"Setting IO weight to {weight} for cgroup {cgroup_id}")
        cmd = build_sudo_shell_redirect(str(weight), io_weight_path)
        return self._run_cmd(cmd)

    def get_current_io_limits(self, cgroup_id: str) -> Optional[tuple[int, int, int, int]]:
        """
        Return the current IO limits as (rbps, wbps, riops, wiops), or None on error.
        """
        io_max_path = self._get_full_cgroup_path(cgroup_id, "io.max")
        if not os.path.exists(io_max_path):
            return None

        try:
            with open(io_max_path, 'r') as f:
                content = f.read().strip()
                if not content:
                    return (0, 0, 0, 0)

                # Parse format: "259:0 rbps=20971520 wbps=10485760 riops=20000 wiops=2200"
                parts = content.split()
                rbps = 0
                wbps = 0
                riops = 0
                wiops = 0
                for part in parts[1:]:  # skip device ID field
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
    # Example usage
    io_ctl = IOController()
    # Test setup
    cgroup_id = "vte-spawn-d689ffa6-5446-4dfb-99f3-c4e702c44ebb.scope"

    # Test case 1: same limit for all disks
    # io_ctl.set_write_io_throughput_throttle_app(cgroup_id, 60 * 1024 * 1024)  # write limit 60MB/s
    # io_ctl.set_read_io_throughput_throttle_app(cgroup_id, 70 * 1024 * 1024)  # read limit 70MB/s
    # io_ctl.set_write_iops_throttle_app(cgroup_id, 3200)  # write IOPS limit 3200
    # io_ctl.set_read_iops_throttle_app(cgroup_id, 21000)  # read IOPS limit 21000

    # Test case 2: compound limit config
    limits = {
        "default": {
            "rbps": 30 * 1024 * 1024,
            "wbps": 15 * 1024 * 1024,
            "wiops": 2201,
            "riops": 20001
        },
        "8:0": {"wbps": 10 * 1024 * 1024},  # sda write limit 10MB/s
        "nvme0n1": {"rbps": 33 * 1024 * 1024, "wbps": 27 * 1024 * 1024}  # nvme read 33MB/s, write 27MB/s
    }
    # # io_ctl.set_disk_io_throttle(cgroup_id, limits)
    io_ctl.set_disk_io_throttle(
        cgroup_id,
        limits=limits,
        disk_filter=["nvme", "sda"],
    )
    #
    # # Test case 3: NVMe-only limits
    # io_ctl.set_write_io_throughput_throttle_app(cgroup_id, 35 * 1024 * 1024, disk_filter="sda")
    # io_ctl.set_read_io_throughput_throttle_app(cgroup_id, 55 * 1024 * 1024, disk_filter="nvme")
    # io_ctl.set_write_iops_throttle_app(cgroup_id, 2300, disk_filter="sda")
    # io_ctl.set_read_iops_throttle_app(cgroup_id, 22000, disk_filter="nvme")

    # Set weight
    io_ctl.set_weight(cgroup_id, 300)
    # Check current settings
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
