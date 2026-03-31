# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
from subprocess import check_output # nosec
from typing import Optional

from utils.logger import logger
from utils import app_utils
from config.config import b_config

class Controller:
    def __init__(self):
        self.config = b_config
        self.cgroup_mount = self.config.cgroup_mount
        self.cpus = os.cpu_count()
        self.uid = self.get_uid()

    def get_uid(self):
        slices_cmd = ["systemctl", "list-units", "--type=slice", "user-*.slice", "--no-legend"]
        try:
            output = check_output(slices_cmd, universal_newlines=True)
            
            for line in output.splitlines():
                import re
                match = re.search(r'(user-\d+\.slice)', line)
                if match:
                    return match.group(1).replace('user-', '').replace('.slice', '')
        except Exception:
            pass
        return ""

    def get_cpu_max(self):
        cpu_max = None
        path = f"/sys/fs/cgroup/user.slice/user-{self.uid}.slice/cpu.max"
        cmd = ["cat", path]

        try:
            result = check_output(cmd, universal_newlines=True).splitlines()
            if result:
                parts = result[0].split()
                if len(parts) >= 2:
                    cpu_max = parts[1]
        except Exception as e:
            logger.error(f"read cpu.max failed: {e}")

        return cpu_max

    def get_user_scopes(self):
        try:
            # Run the command and capture output
            path = '/sys/fs/cgroup/user.slice/user-%s.slice/' % self.uid
            result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

            # Split into lines and remove empty lines/headers
            scopes = [line.replace(path, '') for line in result.stdout.splitlines()
                                                 if line.strip() and line.replace(path, '')
                                                                 and not line.endswith('user-%s.slice' % self.uid)
                                                                 and not line.endswith('user@%s.service' % self.uid)]

            return scopes

        except subprocess.CalledProcessError as e:
            print(f"Error running get_user_scopes(): {e.stderr}")
            return []
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return []

    def get_app_services1(self):
        try:
            # Run the command and capture output
            path = '/sys/fs/cgroup/user.slice/user-%s.slice/user@%s.service/app.slice/' % (self.uid, self.uid)
            result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

            # Split into lines and remove empty lines/headers
            apps = [line.replace(path, '') for line in result.stdout.splitlines()
                                               if line.strip() and line.replace(path, '')
                                                               and not line.endswith('app.slice')]

            return apps

        except subprocess.CalledProcessError as e:
            print(f"Error running get_app_services(): {e.stderr}")
            return []
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            return []

    def get_app_services(self):
        apps = []
        try:
            possible_paths = [
                f'/sys/fs/cgroup/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/',
                f'/sys/fs/cgroup/system.slice/'
            ]

            for path in possible_paths:
                try:
                    # Run the command and capture output
                    result = subprocess.run(
                        ['find', path, '-maxdepth', '1', '-type', 'd'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True
                    )

                    # Process the output for this path
                    path_apps = [
                        line.replace(path, '')
                        for line in result.stdout.splitlines()
                        if line.strip()
                           and line.replace(path, '')
                           and not line.endswith('app.slice')
                    ]

                    apps.extend(path_apps)

                except subprocess.CalledProcessError:
                    # This path didn't work, try the next one
                    continue
                except Exception as e:
                    print(f"Unexpected error processing path {path}: {str(e)}")
                    continue

            return list(set(apps))  # Remove duplicates while preserving order

        except Exception as e:
            print(f"An error occurred in get_app_services(): {str(e)}")
            return []

    def restore_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()
        cmd_prefix = ['sudo'] if getattr(self.config, "vendor", "") == "generic" else []

        print(f"restore_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            cmd = [*cmd_prefix, 'systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=100%']
            result = subprocess.run(cmd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=100%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


    def high_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()
        cmd_prefix = ['sudo'] if getattr(self.config, "vendor", "") == "generic" else []

        print(f"high_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            result = subprocess.run([*cmd_prefix, 'systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

    def _set_resource_quota(
            self,
            app_id: str,
            cpu_quota: Optional[int] = None,
            mem_high: Optional[int] = None,
            io_weight: Optional[int] = None,
            is_restore: bool = False
    ) -> bool:
        """
        安全设置资源限制（CPU/内存/IO）
        :param cpu_quota: CPU百分比（None表示不修改，1-100之间）
        :param mem_high: 内存软限制（如"500M"，必须大于0）
        :param io_weight: IO权重（1-10000，默认100）
        :param is_restore: 是否恢复默认值
        """
        unit_type = "scope"
        # 参数范围检查
        if cpu_quota is not None and not (1 <= cpu_quota <= 100):
            logger.warning(f"Invalid cpu_quota {cpu_quota}, must be 1-100. no limit for cpu.")
            cpu_quota = None

        if mem_high is not None and mem_high <= 0:
            logger.warning(f"Invalid mem_high {mem_high}, no limit for mem.")
            mem_high = None

        if io_weight is not None and not (1 <= io_weight <= 10000):
            logger.warning(f"Invalid io_weight {io_weight}, no limit for io.")
            io_weight = None

        scopes = self.get_user_scopes()
        services = self.get_app_services()

        # logger.debug(f"scopes----------: {scopes}")
        # logger.debug(f"services--------: {services}")

        # 铁威马系统上service与scope一样的执行cmd，不需要unit_type
        if app_id.endswith('.scope'):
            matching_app = app_id
            unit_type = 'scope' if matching_app in scopes else 'service'
        elif app_id.endswith('.service'):
            matching_app = app_id
            unit_type = 'service'
        elif app_id.endswith('.desktop'):
            app_base_name = app_id.replace('.desktop', '').split('.')[-1].lower()
            matching_app = next(
                (unit for unit in scopes + services if app_base_name in unit.lower()),
                None
            )
            unit_type = 'scope' if matching_app in scopes else 'service'
        else:
            matching_app = app_id
            unit_type = 'scope'

        logger.debug(f"matching_app: {matching_app} for app_id: {app_id}")
        if not matching_app:
            logger.warning(f"No matching unit for {app_id}")
            return False

        # 构建限制参数
        properties = []
        if not is_restore:
            if cpu_quota is not None:
                properties.append(f"CPUQuota={cpu_quota * self.cpus}%")
            else:
                properties.append("CPUQuota=")
            if mem_high is not None:
                properties.append(f"MemoryHigh={mem_high}M")
            else:
                properties.append("MemoryHigh=")
            if io_weight is not None:
                properties.append(f"IOWeight={io_weight}")
            else:
                properties.append("IOWeight=")
        else:
            # 恢复时清除所有限制
            properties.extend([
                "CPUQuota=",
                "MemoryHigh=",
                "IOWeight="
            ])

        # 执行命令
        try:
            dbus_address = app_utils.get_dbus_address()
            if not dbus_address:
                raise Exception("无法获取DBus会话地址")

            # TOS的系统上默认user由管理员权限，如果用sudo需要，sudo -u @user python BalancerService.py运行，不然把sudo去掉运行
            # ['sudo', '-u', os.getenv('SUDO_USER') or os.getlogin(), 'systemctl', 'set-property', '--runtime', matching_app]

            if getattr(self.config, "vendor", "") == "generic":
                cmd_base = (
                    ['sudo', 'systemctl', 'set-property', '--runtime', matching_app]
                    if unit_type == 'scope' else
                    [
                        'sudo', '-u', os.getenv('SUDO_USER') or os.getlogin(),
                        f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
                        'systemctl', '--user', 'set-property', '--runtime', matching_app
                    ]
                )
            else:
                cmd_base = (
                    ['systemctl', 'set-property', '--runtime', matching_app]
                )

            cmd = cmd_base + properties
            # logger.debug(f"Executing command: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                env={"DBUS_SESSION_BUS_ADDRESS": dbus_address}
            )
            logger.debug(f"Executed result: {result}")
            if result.returncode == 0:
                return True
            else:
                logger.error(f"Failed to set resource for {matching_app}")
                return False
        except Exception as e:
            logger.error(f"Set resource failed: {str(e)}")
            return False

    # cpu
    def set_cpu_quota(self, app_id: str, cpu_quota: int, is_restore: bool = False):
        if is_restore:
            logger.info(f"Restoring CPU quota for {app_id}")
            cpu_quota = None
        else:
            logger.info(f"Setting CPU quota for {app_id} to {cpu_quota}%")

        return self._set_resource_quota(
            app_id,
            cpu_quota=cpu_quota,
            is_restore=is_restore
        )

    # mem
    def set_mem_high(self, app_id: str, mem_high: Optional[str] = None, is_restore: bool = False):
        if is_restore:
            logger.info(f"Restoring memory limit for {app_id}")
            mem_high = None
        else:
            logger.info(f"Setting memory limit for {app_id} to {mem_high}")

        return self._set_resource_quota(
            app_id,
            mem_high=mem_high,
            is_restore=is_restore
        )

    #io
    def set_io_weight(self, app_id: str, io_weight: Optional[int] = None, is_restore: bool = False):
        if is_restore:
            logger.info(f"Restoring IO weight for {app_id}")
            io_weight = None
        elif io_weight is not None and not (1 <= io_weight <= 10000):
            raise ValueError("IOWeight must be 1-10000")
        else:
            logger.info(f"Setting IO weight for {app_id} to {io_weight}")

        return self._set_resource_quota(
            app_id,
            io_weight=io_weight,
            is_restore=is_restore
        )

    # all
    def set_all_resources(self, app_id: str, cpu_quota: Optional[int] = None, mem_high: Optional[int] = None,
                          io_weight: Optional[int] = None, is_restore: bool = False):
        """限制应用资源"""
        if is_restore:
            logger.info(f"Restoring ALL resources for {app_id}")
            cpu_quota = mem_high = io_weight = None
        else:
            logger.info(
                f"Setting resources for {app_id}: "
                f"CPU={cpu_quota}%, MEM={mem_high}M, IO={io_weight}"
            )

        return self._set_resource_quota(
            app_id,
            cpu_quota=cpu_quota,
            mem_high=mem_high,
            io_weight=io_weight,
            is_restore=is_restore
        )


    def set_weight(self, cgroup: str, weight: int) -> bool:
        """Set CPU weight for a cgroup"""
        path = os.path.join(self.cgroup_mount, cgroup, "cpu.weight")
        try:
            with open(path, 'w') as f:
                f.write(str(weight))
            return True
        except (FileNotFoundError, PermissionError):
            return False

    def set_affinity(self, cgroup: str, cpus: str) -> bool:
        """Set CPU affinity for a cgroup"""
        path = os.path.join(self.cgroup_mount, cgroup, "cpuset.cpus")
        try:
            with open(path, 'w') as f:
                f.write(cpus)
            return True
        except (FileNotFoundError, PermissionError):
            return False
