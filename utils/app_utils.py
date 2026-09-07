# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import os
import queue as _queue
import re
import shlex
import subprocess # nosec
import time
import psutil
import threading
from datetime import datetime

from utils.logger import logger
from db.DatabaseModel import AIAppPriority, DBStatus, get_write_epoch
from typing import List, Dict, Any, Optional
from config.config import b_config

_original_oom_scores: dict[str, dict[str, str]] = {}

# Snapshot of the controlled-apps table, kept in sync via the model's write
# epoch.  See get_controlled_apps().  The epoch starts below zero so the first
# read always populates the snapshot.
_controlled_apps_lock = threading.Lock()
_controlled_apps_snapshot: Optional[List[Dict[str, Any]]] = None
_controlled_apps_epoch: int = -1


def _default_cgroup_root() -> str:
    """The configured cgroup mount, defaulting to the standard v2 location."""
    return getattr(b_config, "cgroup_mount", None) or "/sys/fs/cgroup"


def write_cgroup_file(content: str, target_file: str, allowed_roots=None) -> None:
    """Write *content* to a cgroup control file without invoking a shell.

    The real path of *target_file* (after resolving symlinks and ``..``) must
    fall inside one of *allowed_roots* -- the configured cgroup mount by
    default -- otherwise a ``PermissionError`` is raised and nothing is written.

    This is defence-in-depth: today every caller passes an internally-built
    path, but because this process runs as root the guard makes sure a future
    caller that forwards a tainted path (``../../etc/...``) cannot clobber an
    arbitrary file. ``PermissionError`` subclasses ``OSError``, so callers that
    already treat a failed write as an ``OSError`` handle a refusal unchanged.
    """
    roots = tuple(allowed_roots) if allowed_roots is not None else (_default_cgroup_root(),)
    real_path = os.path.realpath(target_file)
    real_roots = [os.path.realpath(root) for root in roots]
    if not any(real_path == root or real_path.startswith(root + os.sep) for root in real_roots):
        raise PermissionError(f"Refusing to write outside {roots}: {target_file!r} -> {real_path}")
    with open(real_path, "w", encoding="utf-8") as target:
        target.write(f"{content}\n")

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
                next_status = str(data.get('status', '') or '').lower()
                if next_status == 'running':
                    # Refresh/startup scans may re-detect alive processes.
                    # Never downgrade a manually/auto limited app to running
                    # via this generic callback persistence path.
                    rec = AIAppPriority.query().where(AIAppPriority.app_id == data.get('app_id')).first()
                    current_status = str(getattr(rec, 'status', '') or '').lower()
                    if current_status in {'limited', 'a_limited'}:
                        logger.info(
                            "Skip running overwrite for app_id=%s (current=%s)",
                            data.get('app_id'), current_status,
                        )
                        store = False

                if store:
                    result = AIAppPriority.update_record(
                        id=data['app_id'],
                        status=data['status'],
                        up_time=datetime.now()
                    )
                    if result != DBStatus.SUCCESS:
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


def dominant_cgroup_by_pids(pids) -> tuple:
    """Pick the cgroup that holds most of *pids*, as ``(cgroup_path, representative_pid)``.

    The first matched PID is not a safe representative: an app started through a wrapper
    (``sudo systemd-run --scope ...``) keeps the wrapper in the *launcher's* cgroup -- the
    terminal's ``vte-spawn-*.scope`` -- while every worker it spawned lives in the scope
    the app actually owns.  ``pids[0]`` is usually that wrapper (lowest PID, spawned
    first), so the limit lands on the terminal and the workers run unthrottled.

    A majority vote over the matched PIDs cannot be fooled by a single stray process.
    Ties break on the lexicographically-smallest path, matching the "primary cgroup"
    convention used when an app legitimately spans several cgroups.

    Returns ``(None, None)`` when no PID has a readable cgroup.
    """
    counts: Dict[str, int] = {}
    first_pid: Dict[str, int] = {}
    for pid in pids:
        path = get_cgroup_path_by_pid(pid)
        if not path:
            continue
        counts[path] = counts.get(path, 0) + 1
        first_pid.setdefault(path, pid)
    if not counts:
        return None, None
    winner = min(counts, key=lambda p: (-counts[p], p))
    if len(counts) > 1:
        logger.debug(
            f"[cgroup-vote] {len(pids)} pid(s) span {len(counts)} cgroup(s) "
            f"{ {os.path.basename(p): c for p, c in counts.items()} }; picked {winner}"
        )
    return winner, first_pid[winner]


def get_controlled_apps_config(apps_dict=None):
    if apps_dict is None:
        apps_dict = {}
    # Config-file controlled_apps: supplement entries not present in the database
    if hasattr(b_config, 'testing_network_app') and b_config.testing_network_app:
        for app in b_config.testing_network_app:
            app_name = app.get("app_name")
            app_id = app.get("app_cgroup")
            priority = app.get("priority")
            network_priority = app.get("network_priority") or priority
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
                                    "network_priority": network_priority,
                                    "pid": proc.pid,
                                    "cgroup_path": cg_path,
                                    "cgroup_paths": [cg_path],
                                }
                            else:
                                existing = apps_dict[app_id]
                                paths = set(existing.get("cgroup_paths") or [])
                                paths.add(cg_path)
                                existing["cgroup_paths"] = sorted(paths)
                                # Keep a representative legacy field for callers
                                # that still read a single cgroup path.
                                existing["cgroup_path"] = existing["cgroup_paths"][0]
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
            network_priority = getattr(app, "network_priority", None) or priority
            # cmdline kept for possible future matching refinements.
            _cmdline = getattr(app, "cmdline", None)
            matched_pid = None
            matched_paths = set()
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                if app_name and app_name.lower() in proc.name().lower():
                    cg_path = get_cgroup_path_by_pid(proc.pid)
                    if cg_path and app_id in cg_path:
                        if matched_pid is None:
                            matched_pid = proc.pid
                        matched_paths.add(cg_path)

            if matched_paths:
                paths_sorted = sorted(matched_paths)
                apps_dict[app_id] = {
                    "app_name": app_name,
                    "app_id": app_id,
                    "priority": priority,
                    "network_priority": network_priority,
                    "pid": matched_pid,
                    "cgroup_path": paths_sorted[0],
                    "cgroup_paths": paths_sorted,
                }
    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)

    get_controlled_apps_config(apps_dict)
    # 3. Return the merged list
    return list(apps_dict.values()) if apps_dict else None


def _fetch_controlled_apps() -> Optional[List[Dict[str, Any]]]:
    """Read every controlled app straight from the database.

    Returns the rows (possibly an empty list) on success, or ``None`` if the
    query failed, so the caller can tell an empty table from an unusable
    database and keep serving its last good snapshot.
    """
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        return [{
            "app_name": app.name,
            "app_id": app.app_id,
            "controlled": app.controlled,
            "priority": app.priority,
            "network_priority": getattr(app, "network_priority", None) or app.priority,
            "cmdline": app.cmdline,
        } for app in controlled_apps]

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)
        return None


def get_controlled_apps(priority: str = None):
    """ Get the list of all controlled apps with basic info (without dynamic data like pid/cgroup)

    Served from an in-memory snapshot that is refreshed only when the table has
    actually been written to (tracked by the model's write epoch).  This keeps
    the throttle decision path off the database: it calls
    :func:`get_app_control_info` once per candidate, every database access takes
    a process-wide lock shared with the monitor's snapshot writer, and under a
    saturated disk that lock has been observed held for minutes -- long enough
    for the balancer to stop applying the limits that would have relieved it.
    """
    global _controlled_apps_snapshot, _controlled_apps_epoch

    epoch = get_write_epoch(AIAppPriority)
    with _controlled_apps_lock:
        if _controlled_apps_epoch != epoch:
            fetched = _fetch_controlled_apps()
            # On a failed read keep the previous snapshot and leave the epoch
            # behind, so the next call retries instead of caching the failure.
            if fetched is not None:
                _controlled_apps_snapshot = fetched
                _controlled_apps_epoch = epoch
        apps = _controlled_apps_snapshot

    if not apps:
        return None
    if priority is not None:
        apps = [app for app in apps if app.get("priority") == priority]
    # Copy: callers treat the result as their own and some mutate the entries.
    return [dict(app) for app in apps] or None


def get_app_control_info(app_id: str = None, app_name: str = None):
    """Return the control status and metadata for an application."""
    controlled_apps = get_controlled_apps() or []
    controlled_map = {app['app_id']: app for app in controlled_apps if app.get('app_id')}
    name_map = {app['app_name'].lower(): app for app in controlled_apps if app.get('app_name')}

    # An app's identity is its process-name set, not just its display name: an app
    # shown as "optimum" may actually run as "optimum-cli" (its configured
    # process_names). The pressure loop samples the *process* name, so without also
    # matching process_names the running process is judged uncontrolled and lands as a
    # duplicate, undefined-priority auto-limited row instead of being attributed to the
    # managed app. setdefault keeps the display-name mapping authoritative on collision.
    for app in controlled_apps:
        for pname in _get_app_process_names(app_id=app.get('app_id'), app_name=app.get('app_name')):
            if pname:
                name_map.setdefault(pname.lower(), app)

    # Match BOTH identities the top-consumer sampler produces against process_names:
    #   * app_name — the kernel comm (e.g. "python" for a python-launched script), and
    #   * app_id   — the *derived* program identity ("optimum-cli", resolved from the
    #                cmdline for a process living in a shared session/scope).
    # For a script launched from an SSH shell the comm is "python" while the derived id
    # is "optimum-cli"; only the latter equals the configured process_name, so matching
    # app_name alone still misses it and the app splits into a duplicate uncontrolled row.
    lname = app_name.lower() if app_name else None
    aid = app_id.lower() if app_id else None
    is_controlled = (app_id in controlled_map
                     or bool(aid and aid in name_map)
                     or bool(lname and lname in name_map))
    controlled_data = None
    if is_controlled:
        controlled_data = (controlled_map.get(app_id)
                           or (name_map.get(aid) if aid else None)
                           or (name_map.get(lname) if lname else None))

    return is_controlled, controlled_data


def get_app_processes(app_name):
    """Return all running PIDs for an application.

    Matching rule:
    - If ``app_name`` includes command-line parameters (e.g. "bench -m aaa"),
      match by full process cmdline tokens.
    - Otherwise keep the existing fuzzy ``pgrep -fi`` behavior.

    :return:
        list[int]: e.g. [1234, 5678]
    """
    query = (app_name or '').strip()
    if not query:
        return []

    # If the configured process_name carries arguments, use cmdline-level
    # matching so similarly named processes with different args are separated.
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    if len(tokens) > 1:
        target_prog = os.path.basename(tokens[0]).lower()
        required_tokens = [t.lower() for t in tokens[1:] if t.strip()]
        matched: set[int] = set()
        try:
            for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline') or []
                    if not cmdline:
                        continue

                    argv0 = os.path.basename((cmdline[0] or '').strip()).lower()
                    pname = (proc.info.get('name') or '').strip().lower()
                    pexe = os.path.basename((proc.info.get('exe') or '').strip()).lower()

                    if target_prog not in {argv0, pname, pexe}:
                        continue

                    cmdline_lower = [str(x).strip().lower() for x in cmdline if str(x).strip()]
                    if all(tok in cmdline_lower for tok in required_tokens):
                        matched.add(int(proc.info['pid']))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            return sorted(matched)
        except Exception as e:
            logger.warning(f"cmdline match failed for {app_name}: {str(e)}")
            return []

    try:
        result = subprocess.run(
            ['pgrep', '-fi', query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            return [int(pid) for pid in result.stdout.splitlines() if pid.strip()]
    except Exception as e:
        logger.warning(f"pgrep failed for {app_name}: {str(e)}")
    return []


def _get_controlled_app_entry(app_id: str = None, app_name: str = None) -> Optional[dict]:
    """Return the matching controlled_apps entry, or None when not found."""
    apps = getattr(b_config, 'controlled_apps', None) or []
    app_name_lower = app_name.lower() if app_name else None
    for app in apps:
        if not isinstance(app, dict):
            continue
        if app_id and app.get('id') == app_id:
            return app
        if app_name_lower and app.get('name', '').lower() == app_name_lower:
            return app
    return None


def get_app_processes_for_app(process_query: str, app_id: str = None, app_name: str = None) -> List[int]:
    """Find controlled-app PIDs by their exact configured identity.

    ``process_names`` comes from the discovery identity rule, so a fuzzy
    command-line match can claim an SSH shell or launcher that merely mentions
    the name. The optional app metadata is retained for callers' compatibility;
    process identity itself is deliberately independent of cgroup placement.
    """
    return get_app_processes_by_exact_name(process_query)


_DERIVE_PROCESS_NAME = None


def derived_process_identity(info: dict) -> str:
    """Program identity of one process, using the wizard's own derivation rule.

    ``monitor.app_discovery._derive_process_name`` is what wrote ``process_names`` into
    config.yaml in the first place, so reusing it here makes matching symmetric with
    writing: an interpreter/shell launch resolves to the script on both sides, and
    ``python3 /opt/app/server.py`` -- configured as "server.py" -- stays findable even
    though its argv0, comm and exe all read "python3".

    Imported lazily on purpose: ``monitor/__init__`` pulls in ``res_monitor``, which
    imports this module, so a top-level import would re-enter app_utils while it is still
    half-initialised.
    """
    global _DERIVE_PROCESS_NAME
    if _DERIVE_PROCESS_NAME is None:
        from monitor.app_discovery import _derive_process_name
        _DERIVE_PROCESS_NAME = _derive_process_name

    cmdline = info.get('cmdline') or []
    return _DERIVE_PROCESS_NAME({
        'comm': (info.get('name') or '').strip(),
        'exe': (info.get('exe') or '').strip(),
        'cmdline_argv0': (cmdline[0] or '').strip() if cmdline else '',
        'cmdline_tokens': list(cmdline),
    })


def get_app_processes_by_exact_name(process_name: str) -> List[int]:
    """Find the PIDs of ``process_name`` without matching anything else.

    Deliberately NOT a drop-in replacement for :func:`get_app_processes_for_app`. That one
    is fuzzy (``pgrep -fi``, substring against the whole command line), which is the right
    trade for "is this app running" -- there a miss is worse than an over-match. This
    function is for the one place where the trade inverts: resolving which cgroups get a
    resource limit written to them, where an over-match writes io.max into somebody else's
    cgroup. Measured on the testing_io.sh workload, "fio_lo" under ``pgrep -fi`` reached 35
    PIDs across 5 cgroups -- the app itself, plus ``fio_lo2`` (the app the test requires to
    stay *unmanaged*), ``fio_lo3``, the terminal scope holding six ``sudo systemd-run``
    launchers, and the session scope of an unrelated shell that merely had the string
    "fio_lo" somewhere in its command line.

    Two independent exact tests, either of which is enough:

    * the configured name equals argv[0]'s basename, ``comm``, or the exe basename. All
      three are checked because none alone is reliable: ``comm`` is truncated to 15
      characters by the kernel, ``exe`` is unreadable for other users' processes, and
      argv[0] can be rewritten by the process itself.
    * the configured name equals the process's derived identity (see
      :func:`derived_process_identity`) -- this is what finds wrapped and interpreted
      apps, whose configured name is a script rather than an executable.

    Neither test looks at the command line as a substring, so the union of the two cannot
    over-match; it is only ever harder to miss than either test alone.

    When both come up empty it reports what the fuzzy lookup *would* have found and still
    returns nothing. Falling back to that result was the original plan, until the fallback
    was measured: it can only fire when the app has no matching process, i.e. when it is
    not running -- and then ``pgrep -fi`` returns whatever else happens to carry the name,
    which in the measured case was the diagnostic shell command that mentioned it. Writing
    a limit into that process's cgroup is worse than writing none, so the log line is the
    deliverable here: it names the app whose ``process_names`` needs fixing.
    """
    q = (process_name or '').strip().lower()
    if not q:
        return []

    matched: set[int] = set()
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        try:
            info = proc.info
            cmdline = info.get('cmdline') or []
            names = {
                os.path.basename((cmdline[0] or '').strip()).lower() if cmdline else '',
                (info.get('name') or '').strip().lower(),
                os.path.basename((info.get('exe') or '').strip()).lower(),
            }
            if q in names:
                matched.add(int(info['pid']))
                continue

            # A derived identity that differs from the three fields above is always the
            # basename of some argument -- the script the wrapper runs. Screening on that
            # first keeps the derivation itself off the hot path: it re-reads config on
            # every call, and this loop walks every process on the machine.
            if q not in {os.path.basename((t or '').strip()).lower() for t in cmdline[1:]}:
                continue
            if derived_process_identity(info).strip().lower() == q:
                matched.add(int(info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if matched:
        return sorted(matched)

    loose = sorted({int(pid) for pid in get_app_processes(process_name)})
    if loose:
        logger.warning(
            f"[app-identity] no process matches {process_name!r} exactly, though fuzzy "
            f"matching finds {len(loose)} pid(s) {loose[:8]} -- not limiting them. Either the "
            f"app is not running and these merely mention its name, or process_names is wrong.")
    return []


def check_pids_disk_io_usage(running_pids: List[int], threshold_mb: float = 100.0) -> tuple[bool, str]:
    """
    Check whether the aggregate disk IO of a set of PIDs exceeds a threshold.

    Measures actual block-layer bytes from each process's IO counters
    (``/proc/<pid>/io`` read_bytes/write_bytes) over a short interval. No external tool
    (previously ``iotop``) and no privileges beyond what smartune already runs with, so it
    also works on hosts where iotop / kernel taskstats are unavailable.
    :param running_pids: PIDs belonging to a single app
    :param threshold_mb: disk IO threshold in MB/s
    :return:
        tuple(bool, str): (is_busy, error_message)
    """
    try:
        interval = 0.5

        def _sample() -> Dict[int, int]:
            # {pid: read_bytes + write_bytes}; skip pids that vanished or are unreadable.
            totals: Dict[int, int] = {}
            for pid in running_pids:
                try:
                    io = psutil.Process(pid).io_counters()
                    totals[pid] = io.read_bytes + io.write_bytes
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            return totals

        t0 = _sample()
        time.sleep(interval)
        t1 = _sample()

        total_bytes = 0
        for pid, end in t1.items():
            start = t0.get(pid)
            if start is not None and end >= start:  # ignore pids that restarted mid-sample
                total_bytes += end - start
        total_io_mb = total_bytes / (1024.0 * 1024.0) / interval

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
        # Skip flags, env vars, placeholders, and KEY=VALUE assignments
        if part.startswith(("-", "%", "env")):
            continue
        if "=" in part and not part.startswith("/"):
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
    :param app_cmdline: command line string used for process matching
    :param restore: when True, restore the original oom_score_adj; otherwise set based on priority
    :return:
    """
    if not restore and priority.lower() != "critical":
        return  # skip non-critical apps unless restore=True is requested

    try:
        if restore:
            original_scores = _original_oom_scores.get(app_id)
            if not original_scores:
                logger.debug(f"No OOM priority snapshot to restore for {app_name}.")
                return

            restored_pids = []
            target_value = 0
            for pid, original_score in list(original_scores.items()):
                oom_file = f"/proc/{pid}/oom_score_adj"
                if not os.path.exists(oom_file):
                    logger.debug(f"PID {pid} exited before OOM priority could be restored.")
                    del original_scores[pid]
                    continue

                logger.debug(f"Restoring OOM priority for PID {pid} to {original_score}")
                write_cgroup_file(original_score, oom_file, allowed_roots=("/proc",))
                del original_scores[pid]
                restored_pids.append(pid)
                target_value = int(original_score)

            if not original_scores:
                _original_oom_scores.pop(app_id, None)
            if not restored_pids:
                logger.debug(f"No live PID required OOM priority restoration for {app_name}.")
                return

            _update_app_oom_score_adj(app_id, target_value)
            logger.info(
                f"OOM priority restored for {app_name} (PID(s): {', '.join(restored_pids)})"
            )
            return

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
        original_scores = _original_oom_scores.setdefault(app_id, {})
        for pid in pids:
            oom_file = f"/proc/{pid}/oom_score_adj"

            # Record the original value for this app before setting the
            # critical OOM score.  Restoration only uses this app's snapshot.
            if pid not in original_scores:
                with open(oom_file, "r") as f:
                    original_scores[pid] = f.read().strip()
            target_value = "-1000"
            action = "Setting"

            # Update oom_score_adj. smartune runs as root (see smartune.service),
            # so write /proc/<pid>/oom_score_adj directly instead of shelling out
            # to `sudo tee` -- same shell-free path as write_cgroup_file().
            logger.debug(f"{action} OOM priority for PID {pid} to {target_value}")
            # oom_file is /proc/<pid>/oom_score_adj (pid comes from pgrep, so
            # always numeric); allow the /proc tree for this one write.
            write_cgroup_file(str(target_value), oom_file, allowed_roots=("/proc",))

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
        if result != DBStatus.SUCCESS:
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
        if result != DBStatus.SUCCESS:
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
        pids = get_app_processes_for_app(app_name, app_id=app_id, app_name=app_name)
        logger.debug(f"[resource_usage] app_name='{app_name}' -> pids from pgrep: {pids}")
        if not pids and app_id:
            fallback_name = os.path.basename(app_id)
            pids = get_app_processes_for_app(fallback_name, app_id=app_id, app_name=app_name)
            logger.debug(f"[resource_usage] fallback app_id basename='{fallback_name}' -> pids: {pids}")
        if not pids:
            logger.warning(f"No processes found for app {app_name} (ID: {app_id})")
            return {}

        cgroup_path, representative_pid = dominant_cgroup_by_pids(pids)
        logger.debug(
            f"[resource_usage] representative_pid={representative_pid}, "
            f"cgroup_path='{cgroup_path}'"
        )
        if not cgroup_path:
            logger.warning(f"No cgroup found for any of {len(pids)} PID(s) of app {app_name}")
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

        # 1 s window: a compromise between sample stability and limit-path
        # latency; see the same sleep in _get_multi_process_app_resource_usage.
        t1 = time.monotonic()
        cpu_usec1 = read_cpu_usage_usec()
        io_rbytes1, io_wbytes1, io_rios1, io_wios1 = read_io_stats(label="sample1")
        time.sleep(1)
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


def get_dbus_address(uid=None):
    """Dynamically retrieve the DBus session bus address for ``uid``.

    When ``uid`` is None the current process uid is used. Callers that run as
    root but need to reach a desktop user's ``systemd --user`` session must pass
    that user's uid, otherwise the root session bus (``/run/user/0/bus``) is
    resolved and ``systemctl --user`` talks to the wrong session.
    """
    if uid is None:
        uid = os.getuid()

    # Method 1: check the standard socket path
    standard_path = f"/run/user/{uid}/bus"
    if os.path.exists(standard_path):
        return f"unix:path={standard_path}"

    # Method 2: retrieve from a process owned by the target uid
    try:
        for proc in psutil.process_iter(['uids', 'environ']):
            try:
                if proc.uids().real != uid:
                    continue
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
    except Exception:
        pass

    return None


def _get_app_process_names(app_id: str = None, app_name: str = None) -> list:
    """Return the configured ``process_names`` list for an app, or [] if not set.

    Looks up the app in ``controlled_apps`` by ``id`` (exact) or ``name``
    (case-insensitive) and returns the ``process_names`` field.
    Returns an empty list when no match is found or the config is absent.
    """
    app = _get_controlled_app_entry(app_id=app_id, app_name=app_name)
    if app:
        return app.get('process_names', []) or []
    return []


def check_app_running_status(app_id: str, app_name: str, cmdline: str = "") -> str:
    """Determine whether an app is currently running.

    Two modes depending on configuration:

    **Multi-process mode** (``process_names`` is non-empty in ``controlled_apps``):
        Any configured process name found among running processes is enough to
        treat the app as running. This avoids false "stopped" for apps where
        helper/child processes are optional or short-lived.

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
        # At least one named process running means app is running.
        found = []
        for proc_name in process_names:
            if get_app_processes_for_app(proc_name, app_id=app_id, app_name=app_name):
                found.append(proc_name)

        if found:
            logger.debug(
                f"[running_status] '{app_name}': matched process_names {found} (configured={process_names}) → running"
            )
            return "running"

        logger.debug(
            f"[running_status] '{app_name}': no configured process_names matched {process_names} → stopped"
        )
        return "stopped"

    # --- Standard mode: any one match is enough ---
    # 1. Try app_name
    if app_name and get_app_processes_for_app(app_name, app_id=app_id, app_name=app_name):
        logger.debug(f"[running_status] '{app_name}' matched by app_name → running")
        return "running"

    # 2. Try app_id basename (e.g. "benchmark.py")
    if app_id:
        id_basename = os.path.basename(app_id)
        if id_basename and id_basename != app_name and get_app_processes_for_app(id_basename, app_id=app_id, app_name=app_name):
            logger.debug(f"[running_status] '{app_name}' matched by app_id basename '{id_basename}' → running")
            return "running"

    # 3. Try the executable from the configured commandline
    if cmdline:
        exe = _get_executable_name(app_name, cmdline)
        if exe and exe != app_name.lower() and get_app_processes_for_app(exe, app_id=app_id, app_name=app_name):
            logger.debug(f"[running_status] '{app_name}' matched by cmdline exe '{exe}' → running")
            return "running"

    logger.debug(f"[running_status] '{app_name}' (id='{app_id}'): no running process found → stopped")
    return "stopped"


def fetch_all_apps():
    """Return the configured controllable apps from ``b_config.controlled_apps``.

    Returns an empty list when the config has no entries — the dashboard
    surfaces a hint telling the user to add apps via the wizard.
    """
    app_list = []
    apps_config = getattr(b_config, 'controlled_apps', None) or []
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
            "bpf_name": app.get("bpf_name", []) or [],
            "display_name": name,
        }
        app_list.append(app_data)
    return app_list


def build_config_meta(entry: dict) -> Dict[str, Any]:
    """Extract the config-only fields of a ``controlled_apps`` entry.

    These three live nowhere else: the database knows an app's name, id and
    cmdline, but not how BPF or the wizard identify its processes.  Mirroring
    them into ``config_meta_json`` is what makes a hand-deleted entry
    recoverable.
    """
    entry = entry if isinstance(entry, dict) else {}
    return {
        "bpf_name": [str(b) for b in (entry.get("bpf_name") or [])],
        "process_names": [str(p) for p in (entry.get("process_names") or [])],
        "commandline": str(entry.get("commandline") or ""),
    }


def serialize_config_meta(entry: dict) -> str:
    """Render :func:`build_config_meta` for storage in ``config_meta_json``."""
    return json.dumps(build_config_meta(entry), sort_keys=True)


def _parse_config_meta(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a stored ``config_meta_json``; None when absent or unreadable."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def render_config_entry_yaml(name: str, app_id: str, meta: Optional[dict]) -> str:
    """Render a ``controlled_apps`` entry as YAML the user can paste back.

    Uses the same scalar formatter the config writer itself uses
    (:meth:`config.config.Config._format_yaml_value`), so the block matches what
    ``append_to_list_section`` would have written.
    """
    meta = meta if isinstance(meta, dict) else {}
    fmt = b_config._format_yaml_value
    fields = [("name", name), ("id", app_id), ("commandline", meta.get("commandline") or "")]
    fields += [(key, meta.get(key) or []) for key in ("bpf_name", "process_names")]
    lines = [f"  - {fields[0][0]}: {fmt(fields[0][1])}"]
    lines += [f"    {key}: {fmt(value)}" for key, value in fields[1:]]
    return "\n".join(lines)


def config_entry_from_row(row, app_name: str = "", cmdline: str = "") -> Dict[str, Any]:
    """Rebuild a ``controlled_apps`` entry from a row's stored snapshot.

    Rows written before ``config_meta_json`` existed have no snapshot; they still
    yield a usable entry (name / id / cmdline), just without the BPF and
    process-name hints, which the user can fill in later.
    """
    meta = _parse_config_meta(getattr(row, "config_meta_json", None)) or {}
    name = str(getattr(row, "name", "") or "").strip() or app_name
    return {
        "name": name,
        "id": str(getattr(row, "app_id", "") or "").strip(),
        "commandline": meta.get("commandline") or (getattr(row, "cmdline", None) or cmdline or ""),
        "bpf_name": list(meta.get("bpf_name") or []),
        "process_names": list(meta.get("process_names") or []),
    }


def fetch_unregistered_apps() -> List[Dict[str, Any]]:
    """Apps the database still knows about but ``controlled_apps`` no longer lists.

    These are entries a user deleted from config.yaml by hand;
    :func:`reconcile_controlled_apps` un-controls them at startup.  Surfacing
    them alongside :func:`fetch_all_apps` is what lets the UI offer them for
    re-enabling instead of forcing another trip through the wizard -- their row
    still carries the priority, OOM score and saved limit overrides.

    Same shape as :func:`fetch_all_apps`, plus ``previously_managed``.
    """
    apps = []
    try:
        for row in AIAppPriority.query():
            app_id = str(getattr(row, "app_id", "") or "").strip()
            name = str(getattr(row, "name", "") or "").strip()
            if not app_id:
                continue
            if _get_controlled_app_entry(app_id=app_id, app_name=name) is not None:
                continue  # still registered in config.yaml -- fetch_all_apps() has it
            entry = config_entry_from_row(row)
            apps.append({
                "name": entry["name"],
                "app_name": entry["name"],
                "app_id": app_id,
                "priority": getattr(row, "priority", None),
                "network_priority": getattr(row, "network_priority", None),
                "remark": getattr(row, "remark", "") or "",
                "cgroup": getattr(row, "cgroup", "") or "",
                "cmdline": entry["commandline"],
                "process_names": entry["process_names"],
                "display_name": entry["name"],
                "previously_managed": True,
            })
    except Exception as e:
        logger.error(f"fetch_unregistered_apps failed: {e}", exc_info=True)
    return apps


def restore_config_entry(app_id: str, app_name: str = "", cmdline: str = "") -> bool:
    """Write an app's ``controlled_apps`` entry back into config.yaml.

    Called when re-enabling an app whose entry the user deleted by hand.  Without
    it the app would be controlled in the database yet invisible to every
    config-driven lookup (BPF match cache, process_names, multi-process maps) --
    the very split state :func:`reconcile_controlled_apps` exists to resolve, and
    the next restart would simply un-control it again.

    Returns True when an entry was written; False when one already exists, the
    app is unknown, or the write failed.
    """
    if not app_id:
        return False
    if _get_controlled_app_entry(app_id=app_id, app_name=app_name) is not None:
        return False

    try:
        row = AIAppPriority.query().where(AIAppPriority.app_id == app_id).first()
    except Exception as e:
        logger.error(f"restore_config_entry: database lookup failed for '{app_id}': {e}")
        return False
    if row is None:
        return False

    entry = config_entry_from_row(row, app_name=app_name, cmdline=cmdline)
    if not entry["name"] or not entry["id"]:
        logger.warning(f"restore_config_entry: row '{app_id}' has no usable name/id; skipping")
        return False

    if not b_config.append_to_list_section("controlled_apps", entry):
        logger.warning(f"restore_config_entry: failed to write config.yaml entry for '{app_id}'")
        return False

    logger.info(
        "[config-sync] restored controlled_apps entry for '%s' (id=%s) from the stored snapshot",
        entry["name"], app_id,
    )
    return True


def diff_controlled_apps(config_apps, db_rows) -> tuple:
    """Compare ``controlled_apps`` against the app table.

    Returns ``(orphans, stale_meta)``:

    * ``orphans``    – rows still marked ``controlled`` whose config entry is
      gone, i.e. apps the user un-registered by editing config.yaml.
    * ``stale_meta`` – rows whose stored ``config_meta_json`` no longer matches
      their config entry and should be refreshed.

    Rows that are already ``controlled=False`` and absent from config are left
    alone: that is just a registration the user never enabled, and nothing keys
    off it.

    Kept free of database and config imports so it can be exercised directly.
    """
    by_id: Dict[str, dict] = {}
    by_name: Dict[str, dict] = {}
    for entry in (config_apps or []):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "").strip()
        if entry_id:
            by_id[entry_id] = entry
            continue
        # Entries without an id are already skipped by fetch_all_apps(); match
        # them by name so a malformed-but-intentional entry is not mistaken for
        # a deletion and silently un-controlled.
        entry_name = str(entry.get("name") or "").strip().lower()
        if entry_name:
            by_name[entry_name] = entry

    orphans, stale_meta = [], []
    for row in (db_rows or []):
        app_id = str(getattr(row, "app_id", "") or "").strip()
        name = str(getattr(row, "name", "") or "").strip()
        info = {
            "row_id": getattr(row, "id", None) or app_id,
            "app_id": app_id,
            "name": name,
            "priority": getattr(row, "priority", None),
        }
        stored_meta = _parse_config_meta(getattr(row, "config_meta_json", None))

        entry = by_id.get(app_id) or by_name.get(name.lower())
        if entry is None:
            if getattr(row, "controlled", False):
                info["meta"] = stored_meta
                orphans.append(info)
            continue

        meta = build_config_meta(entry)
        if stored_meta != meta:
            info["meta"] = meta
            stale_meta.append(info)

    return orphans, stale_meta


def reconcile_controlled_apps() -> Dict[str, list]:
    """Reconcile the app table with config.yaml.  Call once at startup.

    Must run before the service starts: the BPF monitor list is seeded from the
    database (``controlled=True``) in DynamicBalancer._run_app_intercept_loop,
    which also re-applies the OOM adjustment, so an app dropped from config has
    to be un-controlled before any of that happens.

    Never raises -- a reconciliation problem must not keep the service down.
    """
    result: Dict[str, list] = {"uncontrolled": [], "meta_refreshed": []}
    try:
        rows = list(AIAppPriority.query())
        orphans, stale_meta = diff_controlled_apps(
            getattr(b_config, "controlled_apps", None) or [], rows
        )

        for info in orphans:
            logger.warning(
                "[config-sync] '%s' (id=%s, priority=%s) is no longer in config.yaml "
                "controlled_apps -- switching it to uncontrolled. To manage it again, "
                "put its entry back under controlled_apps and enable it from the "
                "Balancer page:\n%s",
                info["name"] or info["app_id"], info["app_id"], info["priority"],
                render_config_entry_yaml(info["name"], info["app_id"], info.get("meta")),
            )
            if AIAppPriority.update_record(id=info["row_id"], controlled=False) == DBStatus.SUCCESS:
                result["uncontrolled"].append(info["app_id"])
            else:
                logger.warning(f"[config-sync] failed to un-control '{info['app_id']}'")

        for info in stale_meta:
            updated = AIAppPriority.update_record(
                id=info["row_id"],
                config_meta_json=json.dumps(info["meta"], sort_keys=True),
            )
            if updated == DBStatus.SUCCESS:
                result["meta_refreshed"].append(info["app_id"])

        if result["uncontrolled"] or result["meta_refreshed"]:
            logger.info(
                "[config-sync] un-controlled %d app(s) %s; refreshed config snapshot for %d app(s)",
                len(result["uncontrolled"]), result["uncontrolled"], len(result["meta_refreshed"]),
            )
        else:
            logger.info("[config-sync] config.yaml and the app table are already in sync")
    except Exception as e:
        logger.error(f"[config-sync] reconciliation failed: {e}", exc_info=True)

    return result


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
    # Exact name matching, not the fuzzy pgrep: the cgroup set discovered here is what
    # gets a resource limit written to it, so one over-matched process silently drags a
    # whole unrelated cgroup (another app's scope, or the launching terminal's) into the
    # limit. See get_app_processes_by_exact_name.
    for proc_name in process_names:
        pids = get_app_processes_by_exact_name(proc_name)
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

    # 1 s window: a compromise between sample stability and limit-path
    # latency; the 0.5 s window made multi-process apps look idle when only
    # one helper was active at sample time.
    time.sleep(1)

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

    # Per-cgroup CPU debug: if total_cpu_delta is 0 we want to see whether
    # cpu.stat usage_usec moved at all, and on which cgroup the PIDs landed.
    logger.debug(
        f"[multi_process_resource] '{app_name}' cpu samples: "
        f"pids={all_pids} elapsed={elapsed:.3f}s "
        + ", ".join(f"{os.path.basename(cg)}: {cpu1[cg]}->{cpu2[cg]} (Δ={cpu_delta_per_cgroup[cg]})"
                    for cg in cgroup_paths)
    )

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
