# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import queue as _queue
import re
import subprocess
import time
import psutil
import threading
from getpass import getuser
from pwd import getpwnam
from datetime import datetime

from utils.logger import logger
from db.DatabaseModel import AIAppPriority
from typing import List, Dict, Any
from config.config import b_config
from gi.repository import Gio

_original_oom_scores: dict[str, str] = {}


def build_sudo_cmd(base_cmd: List[str]) -> List[str]:
    """
    Build command with or without sudo based on vendor configuration.

    :param base_cmd: The base command as a list
    :return: Command list with sudo prepended if vendor is "generic", otherwise original command
    """
    if getattr(b_config, "vendor", "generic") == "generic":
        return ["sudo"] + base_cmd
    return base_cmd


def build_sudo_shell_redirect(content: str, target_file: str) -> List[str]:
    """
    Build a shell redirection command with or without sudo based on vendor configuration.

    :param content: The content to write (e.g., "+io", "100")
    :param target_file: The target file path
    :return: Command list for shell redirection
    """
    shell_cmd = f"echo '{content}' > {target_file}"
    if getattr(b_config, "vendor", "generic") == "generic":
        return ["sudo", "sh", "-c", shell_cmd]
    return ["sh", "-c", shell_cmd]

class ClientCallbackManager:
    """Manages global state and operations for client-side callbacks."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            # Initialize SSE state once inside __new__ to avoid races
            instance._sse_queues: List[_queue.Queue] = []
            instance._sse_lock = threading.Lock()
            cls._instance = instance
        return cls._instance

    def add_sse_client(self, q: _queue.Queue) -> None:
        """Register an SSE client queue."""
        with self._sse_lock:
            self._sse_queues.append(q)

    def remove_sse_client(self, q: _queue.Queue) -> None:
        """Unregister an SSE client queue."""
        with self._sse_lock:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

    def send_callback_notification(self, data: Dict[str, Any], store=False) -> bool:
        """Send callback notification (thread-safe)."""
        if store:
            try:
                result = AIAppPriority.update_record(
                    id=data['app_id'],
                    status=data['status'],
                    up_time=datetime.now()
                )
                if not result:
                    logger.warning(f"Failed to update database record for {data['app_id']}")
            except Exception as db_error:
                logger.error(f"Database update error: {db_error}")

        with self._sse_lock:
            for q in list(self._sse_queues):
                try:
                    q.put_nowait(data)
                except Exception:
                    pass

        return True


# Singleton instance
callback_manager = ClientCallbackManager()


def get_cgroup_path_by_pid(pid):
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 3:
                    # cgroup v2: 0::<path>
                    return parts[2]
    except Exception:
        pass
    return None


def get_controlled_apps_config(apps_dict=None):
    if apps_dict is None:
        apps_dict = {}
    # Config-file controlled_apps: supplement entries not present in the database
    if hasattr(b_config, 'testing_network_app') and b_config.testing_network_app:
        for app in b_config.testing_network_app:
            app_name = app.get("app_name")
            app_id = app.get("app_cgroup")
            priority = app.get("priority")
            try:
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    if app_name and app_name.lower() in proc.name().lower():
                        cg_path = get_cgroup_path_by_pid(proc.pid)
                        if cg_path and app_id in cg_path:
                            if app_id not in apps_dict:
                                apps_dict[app_id] = {
                                    "app_name": app_name,
                                    "app_id": app_id,
                                    "priority": priority,
                                    "pid": proc.pid,
                                    "cgroup_path": cg_path,
                                }
                            break
            except Exception as e:
                logger.error(f"Error processing app {app_name}: {str(e)}", exc_info=True)
                continue


def get_app_priority(app_id: str = "", app_name: str = "") -> str:
    """Get the priority of an application."""
    try:
        # Build query conditions
        query = AIAppPriority.query()
        conditions = []
        if app_id:
            conditions.append(AIAppPriority.app_id == app_id)
        if app_name:
            conditions.append(AIAppPriority.name == app_name)

        if not conditions:
            return "low"

        query = query.where(conditions[0])
        record = query.first()

        if record:
            return record.priority or "low"
        else:
            return "low"

    except Exception as e:
        logger.error("Failed to get app priority from db: %s", str(e))
        return "low"


def get_priority_value(priority_str: str = "") -> int:
    """
    :param priority_str: e.g. critical
    :return: 100
    """
    priority = priority_str.lower()
    logger.debug(f"Getting priority for: {priority}, is: {b_config.app_priority}")
    if priority not in b_config.app_priority:
        raise ValueError(f"Invalid priority: {priority_str}")
    return b_config.app_priority[priority]


def get_controlled_apps_net():
    """ Get the list of all controlled apps with their network-related info (cgroup path, pid, etc.) """
    apps_dict = {}
    # 1. Database takes priority; fetch controlled apps from DB first
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        for app in controlled_apps:
            app_name = getattr(app, "name", None)
            app_id = getattr(app, "app_id", None)
            priority = getattr(app, "priority", None)
            cmdline = getattr(app, "cmdline", None)
            pid = None
            cgroup_path = None
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                if app_name and app_name.lower() in proc.name().lower():
                    cg_path = get_cgroup_path_by_pid(proc.pid)
                    if cg_path and app_id in cg_path:
                        apps_dict[app_id] = {
                            "app_name": app_name,
                            "app_id": app_id,
                            "priority": priority,
                            "pid": proc.pid,
                            "cgroup_path": cg_path,
                        }
                        break
    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)

    get_controlled_apps_config(apps_dict)
    # 3. Return the merged list
    return list(apps_dict.values()) if apps_dict else None


def get_controlled_apps(priority: str = None):
    """ Get the list of all controlled apps with basic info (without dynamic data like pid/cgroup) """
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        if priority is not None:
            controlled_apps = controlled_apps.filter(AIAppPriority.priority == priority)
        return [{
            "app_name": app.name,
            "app_id": app.app_id,
            "controlled": app.controlled,
            "priority": app.priority,
            "cmdline": app.cmdline,
        } for app in controlled_apps] if controlled_apps else None

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)
        return None


def get_app_control_info(app_id: str = None, app_name: str = None):
    """Return the control status and metadata for an application."""
    controlled_apps = get_controlled_apps() or []
    controlled_map = {app['app_id']: app for app in controlled_apps if app.get('app_id')}
    name_map = {app['app_name'].lower(): app for app in controlled_apps if app.get('app_name')}

    is_controlled = app_id in controlled_map or app_name in name_map
    controlled_data = None
    if is_controlled:
        controlled_data = controlled_map.get(app_id) or name_map.get(app_name)

    return is_controlled, controlled_data


def get_app_processes(app_name):
    """Return all running PIDs for an application via pgrep.

    :return:
        list[int]: e.g. [1234, 5678]
    """
    try:
        result = subprocess.run(
            ['pgrep', '-fi', app_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            return [int(pid) for pid in result.stdout.splitlines() if pid.strip()]
    except Exception as e:
        logger.warning(f"pgrep failed for {app_name}: {str(e)}")
    return []


def check_pids_disk_io_usage(running_pids: List[int], threshold_mb: float = 100.0) -> tuple[bool, str]:
    """
    Check whether the aggregate disk IO of a set of PIDs exceeds a threshold.
    :param running_pids: PIDs belonging to a single app
    :param threshold_mb: disk IO threshold in MB/s
    :return:
        tuple(bool, str): (is_busy, error_message)
    """
    try:
        sample_times, sample_interval = 3, 0.2

        iotop_base = ["iotop", "-b", "-o", "-k", "-n", str(sample_times), "-d", str(sample_interval)]
        iotop_cmd = build_sudo_cmd(iotop_base)
        for pid in running_pids:
            iotop_cmd.extend(["-p", str(pid)])

        result = subprocess.run(
            iotop_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        # Handle command execution errors
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            if "no such file or directory" in error_msg.lower():
                raise Exception("iotop is not installed; please install it first")
            elif "permission denied" in error_msg.lower():
                raise Exception("Insufficient sudo permissions")
            else:
                raise Exception(f"iotop execution failed: {error_msg}")

        # Parse iotop output
        io_pattern = re.compile(r"(?P<pid>\d+)\s+.+?(?P<read_kb>\d+\.\d+)\s+K/s\s+(?P<write_kb>\d+\.\d+)\s+K/s")
        pid_io_data = {pid: {"read": [], "write": []} for pid in running_pids}

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or "PID" in line or "DISK READ" in line:
                continue
            match = io_pattern.search(line)
            if match:
                pid = int(match.group("pid"))
                if pid in pid_io_data:
                    pid_io_data[pid]["read"].append(float(match.group("read_kb")))
                    pid_io_data[pid]["write"].append(float(match.group("write_kb")))

        # Calculate total IO rate
        total_io_mb = 0.0
        for io_data in pid_io_data.values():
            avg_read = sum(io_data["read"]) / len(io_data["read"]) if io_data["read"] else 0.0
            avg_write = sum(io_data["write"]) / len(io_data["write"]) if io_data["write"] else 0.0
            total_io_mb += (avg_read + avg_write) / 1024.0

        logger.debug(f"Total Disk IO for PIDs {running_pids}: {total_io_mb:.2f} MB/s (Threshold: {threshold_mb} MB/s)")
        # Return result; error_msg is empty string when there are no errors
        return total_io_mb > threshold_mb, ""
    except Exception as e:
        logger.error(f"Disk IO check failed: {str(e)}", exc_info=True)
        return False, str(e)


def get_pids_in_cgroup(cgroup_path):
    """Return all process PIDs inside the specified cgroup."""
    try:
        result = subprocess.run(
            ["systemd-cgls", "--no-page", cgroup_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout

        if not output:
            logger.debug(f"No output from systemd-cgls for cgroup {cgroup_path}")
            return []

        pids = re.findall(r"[├└]─(\d+)\s+.+", output)
        filtered_pids = []
        for pid in map(int, pids):
            try:
                cmdline = psutil.Process(pid).cmdline()
                if cmdline and cmdline[0] == "bash":
                    continue  # Skip processes with cmdline "bash"
                filtered_pids.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return filtered_pids

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout while getting PIDs for cgroup {cgroup_path}")
        return []
    except Exception as e:
        logger.error(f"Error getting PIDs for cgroup {cgroup_path}: {str(e)}")
        return []


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
        match = re.search(r"--command=([^\s]+)", app_cmdline)
        if match:
            return match.group(1).lower()  # "missioncenter"
        last_part = app_cmdline.split()[-1]
        if "." in last_part:
            return last_part.split(".")[-1].lower()

    # 3. Generic cases (e.g., "/usr/bin/foo")
    for part in app_cmdline.split():
        # Skip flags, env vars, and placeholders
        if part.startswith(("-", "%", "env")):
            continue

        if "/" in part:
            return os.path.basename(part)
        # If no path (e.g., "firefox"), use as-is
        return part.lower()

    return app_name.lower()


def adjust_oom_priority(
    app_id: str,
    app_name: str,
    priority: str,
    app_cmdline: str,
    restore: bool = False,
) -> None:
    """
    Adjust or restore the OOM priority (oom_score_adj) for an application.
    Primary purpose: protect "critical" apps from being killed by the OOM killer.
    :param app_id:
    :param app_name:
    :param priority: only takes effect when the value is "critical"
    :param app_cmdline: command line string used for pgrep matching
    :param restore: when True, restore the original oom_score_adj; otherwise set based on priority
    :return:
    """
    if not restore and priority.lower() != "critical":
        return  # skip non-critical apps unless restore=True is requested

    target_value = 0
    try:
        # Prefer the first configured process_name (an exe basename pulled
        # straight from /proc/<pid>/exe, so always shell-safe) over
        # _get_executable_name() which derives a regex-y string from the
        # display name when no cmdline is set.  Falls back to the legacy
        # path for old configs where process_names is empty.
        configured_process_names = _get_app_process_names(app_id=app_id, app_name=app_name)
        if configured_process_names:
            exe_name = configured_process_names[0]
        else:
            exe_name = _get_executable_name(app_name, app_cmdline)
        logger.debug(f"Target executable: {exe_name}")

        pgrep_result = subprocess.run(
            ["pgrep", "-f", exe_name],
            capture_output=True,
            text=True,
        )
        if pgrep_result.returncode != 0:
            logger.debug(f"App {app_name} is not running and no OOM adjustment needed.")
            return

        pids = [pid for pid in pgrep_result.stdout.strip().split("\n") if pid]
        for pid in pids:
            oom_file = f"/proc/{pid}/oom_score_adj"

            if restore:
                if pid not in _original_oom_scores:
                    logger.warning(f"No original OOM score recorded for PID {pid}. Skipping.")
                    continue
                target_value = _original_oom_scores.pop(pid)
                action = "Restoring"
            else:
                # Record the original value for this app
                if pid not in _original_oom_scores:
                    with open(oom_file, "r") as f:
                        _original_oom_scores[pid] = f.read().strip()
                target_value = "-1000"
                action = "Setting"

            # Update oom_score_adj
            logger.debug(f"{action} OOM priority for PID {pid} to {target_value}")
            base_cmd = ["tee", oom_file]
            cmd = ["sudo", *base_cmd] if getattr(b_config, "vendor", "") == "generic" else base_cmd
            subprocess.run(
                cmd,
                input=target_value,
                text=True,
                check=True,
            )

        _update_app_oom_score_adj(app_id, int(target_value))
        logger.info(f"OOM priority updated for {app_name} (PID(s): {', '.join(pids)})")

    except Exception as e:
        logger.error(f"Failed to adjust OOM priority for {app_name}: {e}")


def _update_app_oom_score_adj(app_id: str, score: int) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id,
            oom_score=score
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"oom_score_adj updated - ID: {app_id}, New score: {score}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def update_app_status(app_id: str, status: str) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id,
            status=status
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"Status updated - ID: {app_id}, New status: {status}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def get_app_resource_usage(app_id: str, app_name: str) -> dict:
    """Query the actual CPU, memory, and IO usage of a specific application via cgroup.

    If the app has ``process_names`` configured in ``controlled_apps``, the
    usage is aggregated across all cgroups those processes reside in via
    :func:`_get_multi_process_app_resource_usage`.  Otherwise the standard
    single-cgroup path is used.
    """
    try:
        # For multi-process apps with explicit process_names, aggregate across cgroups.
        process_names = _get_app_process_names(app_id=app_id, app_name=app_name)
        if process_names:
            return _get_multi_process_app_resource_usage(app_id, app_name, process_names)

        base_cgroup = "/sys/fs/cgroup"
        if hasattr(b_config, 'cgroup_mount') and b_config.cgroup_mount:
            base_cgroup = b_config.cgroup_mount

        # Find a representative PID to locate the cgroup.
        # Try app_name first; if that yields nothing, fall back to app_id (e.g. "benchmark.py")
        # so that processes whose argv[0] was renamed (e.g. via perl $0=) are still found.
        pids = get_app_processes(app_name)
        logger.debug(f"[resource_usage] app_name='{app_name}' -> pids from pgrep: {pids}")
        if not pids and app_id:
            fallback_name = os.path.basename(app_id)
            pids = get_app_processes(fallback_name)
            logger.debug(f"[resource_usage] fallback app_id basename='{fallback_name}' -> pids: {pids}")
        if not pids:
            logger.warning(f"No processes found for app {app_name} (ID: {app_id})")
            return {}

        representative_pid = pids[0]
        # Locate the cgroup from the first PID
        cgroup_path = get_cgroup_path_by_pid(representative_pid)
        logger.debug(
            f"[resource_usage] representative_pid={representative_pid}, "
            f"cgroup_path='{cgroup_path}'"
        )
        if not cgroup_path:
            logger.warning(f"No cgroup found for PID {representative_pid} of app {app_name}")
            return {}

        # Log the process cmdline for the representative PID to confirm we found the right process
        try:
            proc_cmdline = psutil.Process(representative_pid).cmdline()
            logger.debug(f"[resource_usage] pid={representative_pid} cmdline={proc_cmdline}")
        except Exception:
            pass

        cgroup_dir = os.path.join(base_cgroup, cgroup_path.lstrip('/'))
        logger.debug(f"[resource_usage] cgroup_dir='{cgroup_dir}'")
        num_cpus = os.cpu_count() or 1

        # --- Instantaneous memory from cgroup memory.current ---
        cgroup_mem_bytes = 0
        mem_current_path = os.path.join(cgroup_dir, "memory.current")
        try:
            with open(mem_current_path, 'r') as f:
                raw = f.read().strip()
            cgroup_mem_bytes = int(raw)
            logger.debug(
                f"[resource_usage] memory.current raw='{raw}' "
                f"({cgroup_mem_bytes / (1024**2):.2f} MB) from '{mem_current_path}'"
            )
        except FileNotFoundError:
            logger.debug(f"[resource_usage] memory.current NOT FOUND at '{mem_current_path}'")
        except (IOError, ValueError) as e:
            logger.debug(f"[resource_usage] memory.current read error: {e}")

        # Also read memory.swap.current (cgroup v2) to see if memory was pushed to swap
        swap_bytes = 0
        swap_current_path = os.path.join(cgroup_dir, "memory.swap.current")
        try:
            with open(swap_current_path, 'r') as f:
                swap_raw = f.read().strip()
            swap_bytes = int(swap_raw)
            logger.debug(
                f"[resource_usage] memory.swap.current raw='{swap_raw}' "
                f"({swap_bytes / (1024**2):.2f} MB) — memory reclaimed to swap"
            )
        except FileNotFoundError:
            logger.debug(f"[resource_usage] memory.swap.current NOT FOUND at '{swap_current_path}'")
        except (IOError, ValueError) as e:
            logger.debug(f"[resource_usage] memory.swap.current read error: {e}")

        # Also read memory.high to confirm what limit is currently in effect
        mem_high_path = os.path.join(cgroup_dir, "memory.high")
        try:
            with open(mem_high_path, 'r') as f:
                mem_high_raw = f.read().strip()
            logger.debug(f"[resource_usage] memory.high='{mem_high_raw}' (current effective limit)")
        except Exception:
            pass

        # --- Helpers to sample cumulative cgroup counters ---
        def read_cpu_usage_usec():
            try:
                with open(os.path.join(cgroup_dir, "cpu.stat"), 'r') as f:
                    for line in f:
                        if line.startswith('usage_usec'):
                            return int(line.split()[1])
            except (FileNotFoundError, IOError, ValueError):
                pass
            return 0

        def read_io_stats(label=""):
            rbytes, wbytes, rios, wios = 0, 0, 0, 0
            io_stat_path = os.path.join(cgroup_dir, "io.stat")
            try:
                with open(io_stat_path, 'r') as f:
                    raw_lines = f.readlines()
                if label:
                    logger.debug(
                        f"[resource_usage] io.stat ({label}) raw content "
                        f"(path='{io_stat_path}'): {[l.rstrip() for l in raw_lines]}"
                    )
                for line in raw_lines:
                    parts = dict(p.split('=') for p in line.split() if '=' in p)
                    rbytes += int(parts.get('rbytes', 0))
                    wbytes += int(parts.get('wbytes', 0))
                    rios += int(parts.get('rios', 0))
                    wios += int(parts.get('wios', 0))
            except FileNotFoundError:
                if label:
                    logger.debug(f"[resource_usage] io.stat NOT FOUND at '{io_stat_path}'")
            except (IOError, ValueError) as e:
                if label:
                    logger.debug(f"[resource_usage] io.stat read error: {e}")
            return rbytes, wbytes, rios, wios

        # Sample CPU and IO over a short window so we get accurate rates
        t1 = time.monotonic()
        cpu_usec1 = read_cpu_usage_usec()
        io_rbytes1, io_wbytes1, io_rios1, io_wios1 = read_io_stats(label="sample1")
        time.sleep(0.5)
        t2 = time.monotonic()
        cpu_usec2 = read_cpu_usage_usec()
        io_rbytes2, io_wbytes2, io_rios2, io_wios2 = read_io_stats(label="sample2")

        elapsed = t2 - t1
        elapsed_usec = elapsed * 1_000_000

        logger.debug(
            f"[resource_usage] CPU sample: usec1={cpu_usec1}, usec2={cpu_usec2}, "
            f"delta={cpu_usec2 - cpu_usec1}, elapsed={elapsed:.3f}s, num_cpus={num_cpus}"
        )
        logger.debug(
            f"[resource_usage] IO sample: rbytes1={io_rbytes1}, rbytes2={io_rbytes2}, "
            f"wbytes1={io_wbytes1}, wbytes2={io_wbytes2}, "
            f"delta_r={io_rbytes2 - io_rbytes1}, delta_w={io_wbytes2 - io_wbytes1}, "
            f"delta_rios={io_rios2 - io_rios1}, delta_wios={io_wios2 - io_wios1}"
        )

        cpu_percent = (
            round(max(0.0, cpu_usec2 - cpu_usec1) / (elapsed_usec * num_cpus) * 100, 1)
            if elapsed_usec > 0 else 0.0
        )
        io_read_mb_s = round(max(0.0, (io_rbytes2 - io_rbytes1) / elapsed / (1024 ** 2)), 2) if elapsed > 0 else 0.0
        io_write_mb_s = round(max(0.0, (io_wbytes2 - io_wbytes1) / elapsed / (1024 ** 2)), 2) if elapsed > 0 else 0.0
        io_read_iops = round(max(0.0, (io_rios2 - io_rios1) / elapsed), 1) if elapsed > 0 else 0.0
        io_write_iops = round(max(0.0, (io_wios2 - io_wios1) / elapsed), 1) if elapsed > 0 else 0.0
        mem_current_mb = round(cgroup_mem_bytes / (1024 ** 2), 2)
        mem_swap_mb = round(swap_bytes / (1024 ** 2), 2)

        all_pids = get_pids_in_cgroup(cgroup_path)
        logger.debug(
            f"Resource usage for {app_name} (ID: {app_id}): CPU={cpu_percent:.1f}%, "
            f"Memory_current={mem_current_mb:.2f}MB (swap={mem_swap_mb:.2f}MB), "
            f"IO Read={io_read_mb_s:.2f}MB/s ({io_read_iops:.1f} IOPS), "
            f"IO Write={io_write_mb_s:.2f}MB/s ({io_write_iops:.1f} IOPS)"
        )
        return {
            'pids': list(all_pids),
            'name': app_name,
            'cgroup_path': cgroup_path,
            'cpu_percent': cpu_percent,
            'mem_current': mem_current_mb,
            'mem_swap_current': mem_swap_mb,
            'io_read_mb': io_read_mb_s,
            'io_write_mb': io_write_mb_s,
            'io_read_iops': io_read_iops,
            'io_write_iops': io_write_iops,
        }
    except Exception as e:
        logger.error(f"Error getting resource usage for {app_name} (ID: {app_id}): {e}")
        return {}


def safe_notify(title, message, icon="dialog-information"):
    try:
        # Method 1: try native notify-send first
        user = os.getenv("SUDO_USER") or getuser()

        user_uid = getpwnam(user).pw_uid

        # Build the correct DBus address
        dbus_address = f'unix:path=/run/user/{user_uid}/bus'

        # Execute as the target user via sudo -u
        subprocess.run([
            'sudo', '-u', user,
            f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
            'DISPLAY=:0',
            'notify-send',
            f'--icon={icon}',
            title,
            message
        ], check=True)

    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            # Method 2: fall back to zenity
            subprocess.run(
                ["zenity", "--info", "--text", f"{title}\n{message}", "--title", "System notification"],
                check=True
            )
        except:
            print(f"\a⚠️ {title}: {message}")


def get_dbus_address():
    """Dynamically retrieve the current user's DBus session bus address."""
    uid = os.getuid()

    # Method 1: check the standard socket path
    standard_path = f"/run/user/{uid}/bus"
    if os.path.exists(standard_path):
        return f"unix:path={standard_path}"

    # Method 2: retrieve from process environment
    try:
        import psutil
        for proc in psutil.process_iter(['environ']):
            try:
                env = proc.environ()
                if 'DBUS_SESSION_BUS_ADDRESS' in env:
                    return env['DBUS_SESSION_BUS_ADDRESS']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass

    # Method 3: retrieve via loginctl
    try:
        cmd = ["loginctl", "show-user", str(uid), "--property=Display"]
        display = subprocess.check_output(cmd).decode().strip()
        if display:
            return f"unix:path=/run/user/{uid}/bus"
    except:
        pass

    return None


def _get_app_process_names(app_id: str = None, app_name: str = None) -> list:
    """Return the configured ``process_names`` list for an app, or [] if not set.

    Looks up the app in ``controlled_apps`` by ``id`` (exact) or ``name``
    (case-insensitive) and returns the ``process_names`` field.
    Returns an empty list when no match is found or the config is absent.
    """
    apps = getattr(b_config, 'controlled_apps', None) or []
    app_name_lower = app_name.lower() if app_name else None
    for app in apps:
        if (app_id and app.get('id') == app_id) or \
                (app_name_lower and app.get('name', '').lower() == app_name_lower):
            return app.get('process_names', []) or []
    return []


def check_app_running_status(app_id: str, app_name: str, cmdline: str = "") -> str:
    """Determine whether an app is currently running.

    Two modes depending on configuration:

    **Multi-process mode** (``process_names`` is non-empty in ``controlled_apps``):
        ALL configured process names must be found among running processes.
        Returns ``"running"`` only when every name is matched; otherwise
        ``"stopped"``.

    **Standard mode** (``process_names`` is empty / not configured):
        Any single match is sufficient.  The function tries, in order:

        1. ``app_name``  – searched with ``pgrep -f``
        2. ``app_id`` basename (e.g. ``"benchmark.py"`` from ``"/path/to/benchmark.py"``)
        3. ``cmdline`` first token (the executable basename, e.g. ``"gnome-calculator"``)

        Returns ``"running"`` if any lookup finds at least one live PID;
        otherwise ``"stopped"``.

    :param app_id:   Unique app identifier (DB primary key).
    :param app_name: Human-readable display name.
    :param cmdline:  Command-line string from config / DB (optional).
    :return:         ``"running"`` or ``"stopped"``
    """
    # --- Multi-process mode ---
    process_names = _get_app_process_names(app_id=app_id, app_name=app_name)
    if process_names:
        # ALL named processes must be running
        for proc_name in process_names:
            if not get_app_processes(proc_name):
                logger.debug(
                    f"[running_status] '{app_name}': required process '{proc_name}' not found → stopped"
                )
                return "stopped"
        logger.debug(
            f"[running_status] '{app_name}': all process_names {process_names} found → running"
        )
        return "running"

    # --- Standard mode: any one match is enough ---
    # 1. Try app_name
    if app_name and get_app_processes(app_name):
        logger.debug(f"[running_status] '{app_name}' matched by app_name → running")
        return "running"

    # 2. Try app_id basename (e.g. "benchmark.py")
    if app_id:
        id_basename = os.path.basename(app_id)
        if id_basename and id_basename != app_name and get_app_processes(id_basename):
            logger.debug(f"[running_status] '{app_name}' matched by app_id basename '{id_basename}' → running")
            return "running"

    # 3. Try the executable from the configured commandline
    if cmdline:
        exe = _get_executable_name(app_name, cmdline)
        if exe and exe != app_name.lower() and get_app_processes(exe):
            logger.debug(f"[running_status] '{app_name}' matched by cmdline exe '{exe}' → running")
            return "running"

    logger.debug(f"[running_status] '{app_name}' (id='{app_id}'): no running process found → stopped")
    return "stopped"


def fetch_all_apps():
    app_list = []
    # Prefer the unified controlled_apps list; fall back to the legacy all_apps
    # key for backward compatibility; finally enumerate system desktop apps via Gio.
    apps_config = getattr(b_config, 'controlled_apps', None) or getattr(b_config, 'all_apps', None)
    if apps_config:
        for app in apps_config:
            name = app.get("name", "")
            app_id = app.get("id")
            if not app_id:
                logger.warning(
                    f"fetch_all_apps: controlled_apps entry '{name}' is missing 'id'; "
                    "skipping to avoid duplicate/ambiguous records."
                )
                continue
            app_data = {
                "name": name,              # legacy key used by other callers
                "app_name": name,          # normalized key expected by the React dashboard
                "app_id": app_id,
                "cmdline": app.get("commandline", ""),
                "process_names": app.get("process_names", []) or [],
                "display_name": name,
            }
            app_list.append(app_data)
    else:
        apps = Gio.AppInfo.get_all()
        for app in apps:
            app_data = {
                "name": app.get_name(),    # legacy key used by other callers
                "app_name": app.get_name(),  # normalized key expected by the React dashboard
                "app_id": app.get_id(),  # org.gnome.Calculator.desktop
                "cmdline": app.get_commandline() or "",  # gnome-calculator
                "process_names": [],
                "display_name": app.get_display_name()
            }
            app_list.append(app_data)
    return app_list


def _get_multi_process_app_resource_usage(app_id: str, app_name: str, process_names: list) -> dict:
    """Aggregate cgroup resource usage across all processes of a multi-process app.

    Unlike the single-cgroup :func:`get_app_resource_usage`, this function:

    1. Finds all running PIDs whose process name is in *process_names*.
    2. Collects the unique set of cgroups those PIDs live in.
    3. Samples CPU / IO stats from every cgroup simultaneously (single 0.5 s
       sleep), then sums the deltas for a combined usage figure.

    This handles apps that span multiple systemd units / cgroups (e.g. a
    service that spawns a helper worker in a different slice).

    :param app_id:        Unique app identifier (DB primary key).
    :param app_name:      Human-readable name, used only for log messages.
    :param process_names: List of process names to look for (from config).
    :return:              Usage dict (same schema as :func:`get_app_resource_usage`)
                          with an extra ``cgroup_paths`` key listing every cgroup
                          found, so callers can apply limits to all of them.
    """
    base_cgroup = "/sys/fs/cgroup"
    if hasattr(b_config, 'cgroup_mount') and b_config.cgroup_mount:
        base_cgroup = b_config.cgroup_mount
    num_cpus = os.cpu_count() or 1

    # --- Discover PIDs and cgroups ---
    all_pids: list[int] = []
    cgroup_paths: set[str] = set()
    for proc_name in process_names:
        pids = get_app_processes(proc_name)
        logger.debug(f"[multi_process_resource] app='{app_name}' proc_name='{proc_name}' -> pids: {pids}")
        all_pids.extend(pids)
        for pid in pids:
            cg = get_cgroup_path_by_pid(pid)
            if cg:
                cgroup_paths.add(cg)

    if not cgroup_paths:
        logger.debug(f"[multi_process_resource] No processes found for '{app_name}' (process_names={process_names})")
        return {}

    cgroup_dirs = {cg: os.path.join(base_cgroup, cg.lstrip('/')) for cg in cgroup_paths}

    # --- Per-cgroup reader helpers ---
    def _read_cpu_usec(cg_dir: str) -> int:
        try:
            with open(os.path.join(cg_dir, "cpu.stat"), 'r') as f:
                for line in f:
                    if line.startswith('usage_usec'):
                        return int(line.split()[1])
        except (FileNotFoundError, IOError, ValueError):
            pass
        return 0

    def _read_io(cg_dir: str) -> tuple:
        rbytes = wbytes = rios = wios = 0
        try:
            with open(os.path.join(cg_dir, "io.stat"), 'r') as f:
                for line in f:
                    parts = dict(p.split('=') for p in line.split() if '=' in p)
                    rbytes += int(parts.get('rbytes', 0))
                    wbytes += int(parts.get('wbytes', 0))
                    rios += int(parts.get('rios', 0))
                    wios += int(parts.get('wios', 0))
        except (FileNotFoundError, IOError, ValueError):
            pass
        return rbytes, wbytes, rios, wios

    def _read_mem(cg_dir: str) -> int:
        try:
            with open(os.path.join(cg_dir, "memory.current"), 'r') as f:
                return int(f.read().strip())
        except (FileNotFoundError, IOError, ValueError):
            pass
        return 0

    def _read_swap(cg_dir: str) -> int:
        try:
            with open(os.path.join(cg_dir, "memory.swap.current"), 'r') as f:
                return int(f.read().strip())
        except (FileNotFoundError, IOError, ValueError):
            pass
        return 0

    # --- First snapshot ---
    t1 = time.monotonic()
    cpu1 = {cg: _read_cpu_usec(d) for cg, d in cgroup_dirs.items()}
    io1 = {cg: _read_io(d) for cg, d in cgroup_dirs.items()}
    # Per-cgroup memory snapshot (bytes) – needed for proportional limit distribution
    mem_per_cgroup = {cg: _read_mem(d) for cg, d in cgroup_dirs.items()}
    swap_per_cgroup = {cg: _read_swap(d) for cg, d in cgroup_dirs.items()}
    mem_bytes = sum(mem_per_cgroup.values())
    swap_bytes = sum(swap_per_cgroup.values())

    time.sleep(0.5)

    # --- Second snapshot ---
    t2 = time.monotonic()
    cpu2 = {cg: _read_cpu_usec(d) for cg, d in cgroup_dirs.items()}
    io2 = {cg: _read_io(d) for cg, d in cgroup_dirs.items()}

    elapsed = t2 - t1
    elapsed_usec = elapsed * 1_000_000

    # --- Aggregate deltas ---
    cpu_delta_per_cgroup = {cg: max(0, cpu2[cg] - cpu1[cg]) for cg in cgroup_paths}
    total_cpu_delta = sum(cpu_delta_per_cgroup.values())
    total_r = sum(max(0, io2[cg][0] - io1[cg][0]) for cg in cgroup_paths)
    total_w = sum(max(0, io2[cg][1] - io1[cg][1]) for cg in cgroup_paths)
    total_rios = sum(max(0, io2[cg][2] - io1[cg][2]) for cg in cgroup_paths)
    total_wios = sum(max(0, io2[cg][3] - io1[cg][3]) for cg in cgroup_paths)

    cpu_percent = round(total_cpu_delta / (elapsed_usec * num_cpus) * 100, 1) if elapsed_usec > 0 else 0.0
    io_read_mb_s = round(total_r / elapsed / (1024 ** 2), 2) if elapsed > 0 else 0.0
    io_write_mb_s = round(total_w / elapsed / (1024 ** 2), 2) if elapsed > 0 else 0.0
    io_read_iops = round(total_rios / elapsed, 1) if elapsed > 0 else 0.0
    io_write_iops = round(total_wios / elapsed, 1) if elapsed > 0 else 0.0

    # Use the first cgroup as the representative path for backward-compatible
    # single-cgroup callers; provide the full list in cgroup_paths.
    primary_cgroup = min(cgroup_paths)  # deterministic ordering

    logger.debug(
        f"[multi_process_resource] '{app_name}': cgroups={list(cgroup_paths)} "
        f"cpu={cpu_percent:.1f}% mem={mem_bytes/(1024**2):.1f}MB "
        f"io_r={io_read_mb_s:.2f}MB/s io_w={io_write_mb_s:.2f}MB/s"
    )
    return {
        'pids': all_pids,
        'name': app_name,
        'cgroup_path': primary_cgroup,
        'cgroup_paths': sorted(cgroup_paths),  # all cgroups – used by balancer for multi-cgroup limiting
        'cpu_percent': cpu_percent,
        'mem_current': round(mem_bytes / (1024 ** 2), 2),
        'mem_swap_current': round(swap_bytes / (1024 ** 2), 2),
        'io_read_mb': io_read_mb_s,
        'io_write_mb': io_write_mb_s,
        'io_read_iops': io_read_iops,
        'io_write_iops': io_write_iops,
        # Per-cgroup breakdown keyed by cgroup basename, used by the balancer to
        # distribute limits proportionally across cgroups of a multi-process app.
        'per_cgroup_mem': {os.path.basename(cg): mem_per_cgroup[cg] for cg in cgroup_paths},
        'per_cgroup_cpu_delta': {os.path.basename(cg): cpu_delta_per_cgroup[cg] for cg in cgroup_paths},
    }
