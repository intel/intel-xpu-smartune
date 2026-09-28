# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import pwd
import re
import subprocess # nosec
import time
from subprocess import check_output # nosec
from typing import Optional

from utils.logger import get_logger
logger = get_logger(__name__)
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
        except Exception as exc:
            logger.warning("Failed to resolve user slice UID: %s", exc)
        return ""

    def find_cgroup_dir(self, cgroup_id: str) -> Optional[str]:
        """Absolute cgroup v2 directory for *cgroup_id*, or None.

        The unit name alone does not determine the path -- a scope can sit under
        system.slice, user.slice/user-N.slice/user@N.service/app.slice, or a
        vendor slice -- so the tree is searched rather than guessed.
        """
        if not cgroup_id or not re.fullmatch(r"[A-Za-z0-9._@:-]+", cgroup_id):
            logger.warning(f"Rejecting invalid cgroup id for lookup: {cgroup_id!r}")
            return None
        for root, directories, _ in os.walk(self.cgroup_mount):
            if cgroup_id in directories:
                return os.path.join(root, cgroup_id)
        return None

    def read_applied_limits(self, cgroup_id: str) -> Optional[dict]:
        """The CPU and memory caps actually in force, read from cgroupfs.

        This is the only honest source.  ``systemctl show`` reports systemd's
        idea of the unit's properties, which stays stale when anything writes
        the cgroup files directly -- the drift is then invisible precisely
        because the property still reads back the value systemd last set.
        """
        path = self.find_cgroup_dir(cgroup_id)
        if not path:
            return None
        applied = {"path": path}
        try:
            with open(os.path.join(path, "cpu.max"), encoding="utf-8") as handle:
                parts = handle.read().split()
            applied["cpu_quota_usec"] = None if parts[0] == "max" else int(parts[0])
            applied["cpu_period_usec"] = int(parts[1]) if len(parts) > 1 else 100000
            with open(os.path.join(path, "memory.high"), encoding="utf-8") as handle:
                raw = handle.read().strip()
            applied["memory_high_bytes"] = None if raw == "max" else int(raw)
        except (OSError, IndexError, ValueError) as exc:
            logger.debug(f"Cannot read applied limits for {cgroup_id}: {exc}")
            return None
        return applied

    def verify_applied_limits(self, cgroup_id: str, cpu_quota: Optional[int],
                              mem_high: Optional[int], is_restore: bool,
                              attempts: int = 5, delay: float = 0.1) -> Optional[dict]:
        """Confirm in cgroupfs that the caps we just asked for are really there.

        ``systemctl set-property`` returns once systemd has accepted the
        property, not once the kernel file has been written, so the read is
        retried for a short window before any drift is believed.

        Returns ``{"ok": bool, ...}``, or None when the cgroup could not be
        found or read -- which is not a mismatch, only an unknown.
        """
        # systemd expresses CPUQuota as a percentage of one CPU's period; the
        # caller's cpu_quota is a share of the whole machine, which is how it
        # was scaled by self.cpus on the way in.
        expected_cpu = None
        if not is_restore and cpu_quota is not None:
            expected_cpu = cpu_quota * self.cpus
        expected_mem = None
        if not is_restore and mem_high is not None:
            expected_mem = int(mem_high) * 1024 * 1024

        applied = None
        for attempt in range(attempts):
            applied = self.read_applied_limits(cgroup_id)
            if applied is None:
                return None
            period = applied.get("cpu_period_usec") or 100000
            cpu_ok = True
            if expected_cpu is None:
                cpu_ok = applied["cpu_quota_usec"] is None
            elif applied["cpu_quota_usec"] is not None:
                want = period * expected_cpu / 100.0
                # One percent of the period absorbs systemd's own rounding
                # without hiding a real "we asked for 40% and got 90%".
                cpu_ok = abs(applied["cpu_quota_usec"] - want) <= period * 0.01
            else:
                cpu_ok = False

            mem_ok = True
            if expected_mem is None:
                mem_ok = applied["memory_high_bytes"] is None
            elif applied["memory_high_bytes"] is not None:
                # The kernel rounds memory.high down to a page multiple.
                mem_ok = abs(applied["memory_high_bytes"] - expected_mem) <= 4096
            else:
                mem_ok = False

            if cpu_ok and mem_ok:
                return {"ok": True, **applied}
            if attempt < attempts - 1:
                time.sleep(delay)

        result = {"ok": False, "expected_cpu_pct": expected_cpu,
                  "expected_memory_high_bytes": expected_mem, **(applied or {})}
        logger.warning(
            "Limit verification failed for %s: expected cpu=%s%% mem=%s, "
            "cgroupfs reports cpu.max=%s memory.high=%s (%s)",
            cgroup_id,
            "cleared" if expected_cpu is None else expected_cpu,
            "cleared" if expected_mem is None else f"{expected_mem // (1024 * 1024)}M",
            applied.get("cpu_quota_usec") if applied else "?",
            applied.get("memory_high_bytes") if applied else "?",
            applied.get("path") if applied else "unknown path",
        )
        return result

    def inspect_cgroup_limits(self, cgroup_id: str) -> dict:
        """Read active v2 resource limits for one systemd cgroup."""
        if not cgroup_id or not re.fullmatch(r"[A-Za-z0-9._@:-]+", cgroup_id):
            return {"available": False, "resources": []}
        for root, directories, _ in os.walk(self.cgroup_mount):
            if cgroup_id not in directories:
                continue
            path = os.path.join(root, cgroup_id)
            try:
                with open(os.path.join(path, "cpu.max"), encoding="utf-8") as handle:
                    cpu_limited = handle.read().split()[0] != "max"
                with open(os.path.join(path, "memory.high"), encoding="utf-8") as handle:
                    memory_limited = handle.read().strip() != "max"
                with open(os.path.join(path, "io.max"), encoding="utf-8") as handle:
                    io_limited = any(
                        value != "max"
                        for line in handle
                        for field in line.split()[1:]
                        if field.split("=", 1)[0] in {"rbps", "wbps", "riops", "wiops"}
                        for value in [field.split("=", 1)[1]]
                    )
            except (OSError, IndexError):
                return {"available": False, "resources": []}
            resources = []
            if cpu_limited:
                resources.append("cpu")
            if memory_limited:
                resources.append("memory")
            if io_limited:
                resources.append("disk_io")
            return {"available": True, "resources": resources}
        return {"available": True, "resources": []}

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
            path = '/sys/fs/cgroup/user.slice/user-%s.slice/' % self.uid
            result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

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
            path = '/sys/fs/cgroup/user.slice/user-%s.slice/user@%s.service/app.slice/' % (self.uid, self.uid)
            result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

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
                    result = subprocess.run(
                        ['find', path, '-maxdepth', '1', '-type', 'd'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True
                    )

                    path_apps = [
                        line.replace(path, '')
                        for line in result.stdout.splitlines()
                        if line.strip()
                           and line.replace(path, '')
                           and not line.endswith('app.slice')
                    ]

                    apps.extend(path_apps)

                except subprocess.CalledProcessError:
                    continue
                except Exception as e:
                    logger.error(f"Unexpected error processing path {path}: {str(e)}")
                    continue

            return list(set(apps))

        except Exception as e:
            logger.error(f"An error occurred in get_app_services(): {str(e)}")
            return []

    def restore_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()

        logger.debug(f"restore_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            # smartune runs as root; system scopes need no sudo.
            cmd = ['systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=100%']
            result = subprocess.run(cmd,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=100%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


    def high_cpu_throttle(self):
        scopes = self.get_user_scopes()
        services = self.get_app_services()

        logger.debug(f"high_cpu_throttle scopes = {scopes}, services = {services}")
        for scope in scopes:
            # smartune runs as root; system scopes need no sudo.
            result = subprocess.run(['systemctl', 'set-property', '--runtime', '%s' % scope, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        for service in services:
            result = subprocess.run(['systemctl', '--user', 'set-property', '--runtime', '%s' % service, 'CPUQuota=60%'],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

    def _resolve_session_user(self) -> Optional[str]:
        """Return the desktop user whose ``systemd --user`` session we target.

        Under interactive ``sudo -E`` startup, ``SUDO_USER`` names the invoking
        user.  Under the systemd service (``User=root``, no controlling tty)
        ``SUDO_USER`` is unset and ``os.getlogin()`` raises ``OSError``, so fall
        back to the owner of an active ``/run/user/<uid>`` runtime directory,
        skipping root's own.  Returns ``None`` when no desktop session is found.
        """
        user = os.getenv('SUDO_USER')
        if user:
            return user
        try:
            return os.getlogin()
        except OSError:
            pass
        try:
            for entry in sorted(os.listdir('/run/user')):
                if entry.isdigit() and int(entry) != 0:
                    return pwd.getpwuid(int(entry)).pw_name
        except (OSError, KeyError):
            pass
        return None

    def _resolve_process_unit(self, app_id: str):
        """Resolve a bare process name to the systemd unit it actually runs in.

        A process started directly from a shell (e.g. ``optimum-cli``) is not a
        systemd unit of its own; it lives inside its login session's scope
        (``session-N.scope`` under user.slice) or a service under system.slice.
        ``systemctl set-property`` needs that real unit name -- passing the bare
        process name makes systemctl assume a ``<name>.service`` that does not
        exist ("Unit optimum-cli.service not found").

        Note: session scopes are shared by every process in that login session,
        so limiting one affects the whole session -- this is the accepted
        trade-off for controlling a plain, non-unit process.

        Returns ``(unit_name, unit_type)`` or ``(None, None)`` when no running
        process/controllable unit can be found.
        """
        pids = app_utils.get_app_processes(app_id)
        if not pids:
            logger.warning(f"No running process found for {app_id}; cannot resolve a systemd unit")
            return None, None

        cgroup_path, _ = app_utils.dominant_cgroup_by_pids(pids)
        if not cgroup_path:
            logger.warning(f"Cannot resolve cgroup for {app_id} (pids={pids})")
            return None, None

        unit_name = os.path.basename(cgroup_path.rstrip('/'))
        if not (unit_name.endswith('.scope') or unit_name.endswith('.service')):
            logger.warning(
                f"Process {app_id} lives in '{cgroup_path}', which is not a "
                f"controllable systemd unit (scope/service)"
            )
            return None, None

        if '/system.slice/' in cgroup_path:
            unit_type = 'system_service'
        elif unit_name.endswith('.scope'):
            unit_type = 'scope'
        else:
            unit_type = 'service'

        logger.debug(f"Resolved process {app_id} -> unit {unit_name} ({unit_type})")
        return unit_name, unit_type

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
        if cpu_quota is not None and not (1 <= cpu_quota <= 100):
            logger.warning(f"Invalid cpu_quota {cpu_quota}, must be 1-100. no limit for cpu.")
            cpu_quota = None

        if mem_high is not None and mem_high <= 0:
            logger.warning(f"Invalid mem_high {mem_high}, no limit for mem.")
            mem_high = None

        if io_weight is not None and not (1 <= io_weight <= 10000):
            logger.warning(f"Invalid io_weight {io_weight}, no limit for io.")
            io_weight = None

        if not is_restore and cpu_quota is None and mem_high is None and io_weight is None:
            return True

        scopes = self.get_user_scopes()
        services = self.get_app_services()

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
            # systemctl must target the process's containing scope or service.
            matching_app, resolved_type = self._resolve_process_unit(app_id)
            unit_type = resolved_type or 'scope'

        logger.debug(f"matching_app: {matching_app} for app_id: {app_id}")
        if not matching_app:
            logger.warning(f"No matching unit for {app_id}")
            return False

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
            # Do not modify IOWeight: it can detach the io controller and race
            # with IOController io.max writes, causing EACCES.
        else:
            # IOWeight remains unchanged; see the io.max constraint above.
            properties.extend([
                "CPUQuota=",
                "MemoryHigh=",
            ])

        _MAX_RETRIES = 3
        try:
            dbus_address = None
            if getattr(self.config, "vendor", "") == "generic" and unit_type not in ('scope', 'system_service'):
                # User-space .service unit: smartune is root, so drop to the
                # desktop user to reach their `systemd --user` session bus.
                session_user = self._resolve_session_user()
                if not session_user:
                    raise Exception("Cannot resolve desktop user for user-scope systemctl")
                # Derive the DBus address from the *resolved* user's uid, not the
                # current (root) uid, otherwise systemctl --user would target the
                # wrong session bus (/run/user/0/bus).
                try:
                    session_uid = pwd.getpwnam(session_user).pw_uid
                except KeyError:
                    raise Exception(f"Cannot resolve uid for desktop user {session_user}")
                dbus_address = app_utils.get_dbus_address(session_uid)
                if not dbus_address:
                    raise Exception("Failed to retrieve DBus session address")
                cmd_base = [
                    'sudo', '-u', session_user,
                    f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
                    'systemctl', '--user', 'set-property', '--runtime', matching_app
                ]
            else:
                # System scopes/services (and non-generic vendors): smartune
                # already runs as root, so no sudo prefix is needed.
                cmd_base = ['systemctl', 'set-property', '--runtime', matching_app]

            cmd = cmd_base + properties

            # Merge the DBus address into the existing environment rather than
            # replacing it, so PATH/locale (needed to resolve sudo/systemctl)
            # are preserved.
            run_env = os.environ.copy()
            if dbus_address:
                run_env["DBUS_SESSION_BUS_ADDRESS"] = dbus_address

            logger.debug(f"Executing command: {' '.join(cmd)}")
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=True,
                        env=run_env
                    )
                    logger.debug(f"Executed result: {result}")
                    # systemctl exiting 0 only means systemd accepted the
                    # property.  Confirm against cgroupfs, never against
                    # `systemctl show`: the property keeps reporting whatever
                    # systemd last set even after a direct write has replaced
                    # it in the kernel, so drift would be invisible exactly
                    # where it matters.
                    self.verify_applied_limits(
                        matching_app, cpu_quota, mem_high, is_restore)
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
