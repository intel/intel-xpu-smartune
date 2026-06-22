# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import time
from subprocess import check_output
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
            logger.error(f"Error running get_user_scopes(): {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"An unexpected error occurred: {str(e)}")
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
            logger.error(f"Error running get_app_services(): {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"An unexpected error occurred: {str(e)}")
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
                    logger.error(f"Unexpected error processing path {path}: {str(e)}")
                    continue

            return list(set(apps))  # Remove duplicates while preserving order

        except Exception as e:
            logger.error(f"An error occurred in get_app_services(): {str(e)}")
            return []

    def restore_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()
        cmd_prefix = ['sudo'] if getattr(self.config, "vendor", "") == "generic" else []

        logger.debug(f"restore_cpu_throttle scopes = {scopes}, services = {services}")
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

        logger.debug(f"high_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            result = subprocess.run([*cmd_prefix, 'systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

    def _is_system_service(self, unit_name: str) -> bool:
        """Return True if *unit_name* lives under /sys/fs/cgroup/system.slice.

        System services (e.g. ``hs_agent.service``) must be controlled via
        ``sudo systemctl set-property --runtime`` without ``--user``.  User
        services (under user.slice) use ``systemctl --user set-property``.
        """
        system_cg = os.path.join(self.cgroup_mount, 'system.slice', unit_name)
        return os.path.isdir(system_cg)

    def _set_resource_quota(
            self,
            app_id: str,
            cpu_quota: Optional[int] = None,
            mem_high: Optional[int] = None,
            io_weight: Optional[int] = None,
            is_restore: bool = False
    ) -> bool:
        """
        Set resource limits safely (CPU / memory / IO).
        :param cpu_quota: CPU percentage (None = no change, valid range 1-100)
        :param mem_high: Memory soft limit in MB (None = no change, must be > 0)
        :param io_weight: IO weight (None = no change, valid range 1-10000, default 100)
        :param is_restore: When True, clear all limits and restore defaults
        """
        unit_type = "scope"
        # Validate parameter ranges
        if cpu_quota is not None and not (1 <= cpu_quota <= 100):
            logger.warning(f"Invalid cpu_quota {cpu_quota}, must be 1-100. no limit for cpu.")
            cpu_quota = None

        if mem_high is not None and mem_high <= 0:
            logger.warning(f"Invalid mem_high {mem_high}, no limit for mem.")
            mem_high = None

        if io_weight is not None and not (1 <= io_weight <= 10000):
            logger.warning(f"Invalid io_weight {io_weight}, no limit for io.")
            io_weight = None

        # If there is nothing to apply (and this is not a restore), skip the systemctl call entirely.
        if not is_restore and cpu_quota is None and mem_high is None and io_weight is None:
            return True

        scopes = self.get_user_scopes()
        services = self.get_app_services()

        # On TOS systems, service units use the same command as scopes (no unit_type distinction needed)
        if app_id.endswith('.scope'):
            matching_app = app_id
            if matching_app in scopes:
                unit_type = 'scope'
            elif self._is_system_service(app_id):
                unit_type = 'system_service'
            else:
                unit_type = 'service'
        elif app_id.endswith('.service'):
            matching_app = app_id
            # System services (under /sys/fs/cgroup/system.slice/) must use
            # "sudo systemctl" without "--user"; user services use "--user".
            unit_type = 'system_service' if self._is_system_service(app_id) else 'service'
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

        # Build systemctl property arguments
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
            # IOWeight is intentionally NOT touched here. This project drives
            # all IO limits through io.max directly (see IOController), and
            # writing IOWeight (or clearing it) makes systemd asynchronously
            # detach `io` from the parent slice's cgroup.subtree_control,
            # which races with concurrent io.max writes and produces EACCES
            # (the multi-SSD restore failure we hit in 2026-06).
        else:
            # Restore: clear CPU/memory limits we may have set above. IOWeight
            # is omitted for the same reason as in the set branch.
            properties.extend([
                "CPUQuota=",
                "MemoryHigh=",
            ])

        # Execute command with up to _MAX_RETRIES retries to handle transient dbus timeout issues
        _MAX_RETRIES = 3
        try:
            dbus_address = app_utils.get_dbus_address()
            if not dbus_address:
                raise Exception("Failed to retrieve DBus session address")

            if getattr(self.config, "vendor", "") == "generic":
                # scope and system_service both use "sudo systemctl" (no --user);
                # user-space .service units go through the D-Bus session bus.
                cmd_base = (
                    ['sudo', 'systemctl', 'set-property', '--runtime', matching_app]
                    if unit_type in ('scope', 'system_service') else
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

            logger.debug(f"Executing command: {' '.join(cmd)}")
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True,
                        env={"DBUS_SESSION_BUS_ADDRESS": dbus_address} if dbus_address else None
                    )
                    logger.debug(f"Executed result: {result}")
                    return True
                except subprocess.CalledProcessError as e:
                    logger.warning(
                        f"Attempt {attempt}/{_MAX_RETRIES} failed for {matching_app}: "
                        f"returncode={e.returncode}, stderr={e.stderr.strip()}"
                    )
                    if attempt < _MAX_RETRIES:
                        time.sleep(0.5)

            logger.error(f"Failed to set resource for {matching_app} after {_MAX_RETRIES} attempts")
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
        """Set resource limits for an application (CPU, memory, and IO)."""
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
