import os
import subprocess
from subprocess import check_output, Popen, PIPE

from utils.logger import logger
from utils import app_utils

class Controller:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
        self.cpus = os.cpu_count()
        self.uid = self.get_uid()
        self.default_cpu_max = self.get_cpu_max()

    def get_uid(self):
        # command used to get active user slices
        slices_cmd = "systemctl list-units user-*.slice | grep -oE 'user-[^ ]*.slice' || [ $? = 1 ]"

        active_user = check_output(slices_cmd, shell=True, universal_newlines=True).splitlines()
        if active_user:
            uid = active_user[0].strip('user-').strip('.slice')

        return uid

    def get_cpu_max(self):
        cpu_max = None
        cmd = "cat /sys/fs/cgroup/user.slice/user-%s.slice/cpu.max" % self.uid

        result = check_output(cmd, shell=True, universal_newlines=True).splitlines()
        if result:
            cpu_max = result[0].split()[1]

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

    def get_app_services(self):
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

    def restore_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()

        print(f"restore_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            result = subprocess.run(['sudo', 'systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=100%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=100%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


    def high_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()

        print(f"high_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            result = subprocess.run(['sudo', 'systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


    def _set_cpu_quota(self, app_id: str, quota_percent: int, is_restore: bool = False):
        """Core method to set CPU quota (throttle or restore)."""
        scopes = self.get_user_scopes()
        services = self.get_app_services()

        # logger.debug(f"critical_cpu_throttle scopes = {scopes}, services = {services}")
        if app_id.endswith('.scope'):
            matching_app = app_id
            unit_type = 'scope'
        else:
            app_base_name = app_id.replace('.desktop', '').split('.')[-1].lower()

            matching_app = None
            unit_type = None

            # Check scopes first
            for scope in scopes:
                if app_base_name in scope.lower():
                    matching_app = scope
                    unit_type = 'scope'
                    break

            # If not found in scopes, check services
            if not matching_app:
                for service in services:
                    if app_base_name in service.lower():
                        matching_app = service
                        unit_type = 'service'
                        break

        if not matching_app:
            logger.warning(f"No matching scope or service found for app_id: {app_id}")
            return False

        action = "Restoring" if is_restore else "Throttling"
        logger.info(f"{action} CPU for {unit_type}: {matching_app}")

        try:
            dbus_address = app_utils.get_dbus_address()
            if not dbus_address:
                raise Exception("无法获取DBus会话地址")

            quota_value = f"CPUQuota={quota_percent}%"
            # Build and log the command
            if unit_type == 'scope':
                cmd = ['sudo', 'systemctl', 'set-property', '--runtime', matching_app, quota_value]
            else:
                # cmd = ['systemctl', '--user', 'set-property', '--runtime', matching_app, 'CPUQuota=30%']
                cmd = [
                    'sudo', '-u', os.getenv('SUDO_USER') or os.getlogin(),
                    f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
                    'systemctl', '--user', 'set-property',
                    '--runtime', matching_app, quota_value
                ]

            logger.debug(f"Executing command: {' '.join(cmd)}")

            env = os.environ.copy()
            env['DBUS_SESSION_BUS_ADDRESS'] = dbus_address

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            logger.debug(f"Command output: {result}")
            if result.returncode == 0:
                return True
            else:
                logger.error(f"Failed to set CPU quota for {matching_app}")
                return False

        except Exception as e:
            logger.error(f"Unexpected error setting CPU quota: {str(e)}")
            return False

    def critical_cpu_throttle(self, app_id: str):
        """Throttle CPU to 30%."""
        return self._set_cpu_quota(app_id, quota_percent=30, is_restore=False)

    def restore_cpu_quota(self, app_id: str):
        """Restore CPU to 100%."""
        return self._set_cpu_quota(app_id, quota_percent=100, is_restore=True)


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
