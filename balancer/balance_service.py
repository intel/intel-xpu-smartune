# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import queue as _queue
import signal
import ssl
import psutil
from datetime import datetime
from threading import Lock

from flask import Flask, request, Response, stream_with_context

from balancer.balancer import DynamicBalancer
from balancer.controller.process_control import kill_process, suspend_process
from db.DatabaseModel import AIAppPriority, DBStatus, init_database
from monitor.monitor_api import (
    monitor_bp,
    register_system_pressure_monitor,
    register_network_config_reload_notifier,
    _start_snapshot_cleanup_task,
    _start_dynamic_info_auto_refresh,
    stop_dynamic_info_collector,
)
from monitor.system_info import preload_static_info, shutdown_gpu_usage
from smartune_api import auth_bp, smartune_bp, set_balancer_available
from utils.app_utils import adjust_oom_priority, callback_manager, check_app_running_status, fetch_all_apps, fetch_unregistered_apps, get_priority_value, get_app_processes_for_app, get_cgroup_path_by_pid, reconcile_controlled_apps, restore_config_entry, serialize_config_meta
from utils.http_utils import RetCode, construct_response
from utils.logger import logger

app = Flask(__name__)
app.register_blueprint(monitor_bp)
app.register_blueprint(smartune_bp)
app.register_blueprint(auth_bp)
set_balancer_available(True)
_start_snapshot_cleanup_task()

_KEY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "key")
CERT_FILE = os.path.join(_KEY_DIR, 'b_server.crt')
KEY_FILE = os.path.join(_KEY_DIR, 'b_server.key')

_service_lock = Lock()
_service = None  # Singleton service instance
_shutdown_lock = Lock()
_shutdown_started = False


class DynamicService:
    """Encapsulates the core balancer logic as a managed service."""

    def __init__(self):
        self.balancer = DynamicBalancer()
        # Share the controller's SystemPressureMonitor with the monitor API so that
        # both use the same instance (including is_limited_app_dominant state).
        register_system_pressure_monitor(self.balancer.control_manager.system_pressure_monitor)
        register_network_config_reload_notifier(self.balancer.network_controller.request_reload)
        self.rebuild_controlled_map()

    def start(self):
        self.balancer.start()
        # Begin continuous background collection of the configured
        # monitored_sections (no-op if the operator set it to []).
        _start_dynamic_info_auto_refresh()


    def cancel_relaunch(self, app_id):
        return self.balancer.cancel_relaunch_by_app_id(app_id)

    def resource_limit(self, app_id, app_name, priority, limit_overrides=None, target_cgroups=None):
        return self.balancer.set_resource_limit(
            app_id, app_name, priority,
            limit_overrides=limit_overrides,
            target_cgroups=target_cgroups,
        )

    def resource_limit_profile(self, app_id, app_name, priority):
        return self.balancer.get_resource_limit_profile(app_id, app_name, priority)

    def restore_resource(self, app_id):
        return self.balancer.set_restore_resource(app_id)

    def get_limit_snapshot(self, app_id):
        return self.balancer.get_limit_snapshot(app_id)

    def get_auto_limited_apps(self):
        return self.balancer.get_auto_limited_apps()

    def restore_auto_limited(self, app_id):
        return self.balancer.restore_auto_limited(app_id)

    def lock_to_manual(self, app_id):
        return self.balancer.lock_to_manual(app_id)

    def adopt_auto_limit(self, effective_app_id, new_app_id, new_app_name="", priority=""):
        return self.balancer.adopt_auto_limit(
            effective_app_id, new_app_id, new_app_name=new_app_name, priority=priority)

    def get_auto_limit_exclusions(self):
        return self.balancer.get_auto_limit_exclusions()

    def remove_auto_limit_exclusion(self, ident):
        return self.balancer.remove_auto_limit_exclusion(ident)

    def add_control(self, app_name):
        self.balancer.bpf_monitor.add_to_monitorlist(app_name)

    def remove_control(self, app_name):
        self.balancer.bpf_monitor.remove_from_monitorlist(app_name)

    def get_controlled_list(self):
        return self.balancer.bpf_monitor.get_monitored_apps()

    def rebuild_controlled_map(self):
        self.balancer.bpf_monitor.rebuild_controlled_map()

    def register_running_pids(self, app_id, app_name, cmdline=""):
        return self.balancer.bpf_monitor.register_running_pids(app_id, app_name, cmdline)

    def check_running_apps(self):
        return self.balancer.bpf_monitor.scan_already_running_apps()

    def shutdown(self):
        stop_dynamic_info_collector()
        self.balancer.shutdown()
        shutdown_gpu_usage()


def start_service():
    """Initialize the service and register OS signal handlers."""
    global _service
    with _service_lock:
        if _service is None:
            logger.info("Initializing DynamicService for the first time")
            _service = DynamicService()
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            _service.start()
        else:
            logger.debug("DynamicService already initialized, skipping")
    return _service


def _handle_signal(signum, frame):
    # Keep signal handler minimal and async-signal-safe: no logging/subprocess here.
    # Actual shutdown is handled in main() finally block.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def _shutdown_service_once():
    global _shutdown_started
    with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True

    try:
        if _service:
            _service.shutdown()
    except Exception as exc:
        logger.error(f"Service shutdown failed: {exc}")

    try:
        reset_app_status()
    except Exception as exc:
        logger.error(f"Reset app status failed during shutdown: {exc}")


def reset_app_status():
    """Reset all application statuses to 'NA'."""
    try:
        updated_count = AIAppPriority.update_all_records(
            status="NA",
            up_time=datetime.now()
        )
        if updated_count == 0:
            logger.warning("No records were updated currently.")
        else:
            logger.info(f"Reset {updated_count} app statuses to 'NA'")
    except Exception as e:
        logger.error(f"Failed to reset app statuses: {str(e)}")


def normalize_priority(raw, default="medium"):
    """Return a canonical application priority label."""
    labels = {"low", "medium", "high", "critical"}
    if isinstance(raw, str):
        value = raw.strip().lower()
        return value if value in labels else default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        if raw >= 90:
            return "critical"
        if raw >= 65:
            return "high"
        if raw >= 35:
            return "medium"
        return "low"
    return default


def normalize_network_priority(raw):
    """Return a canonical network priority label for class-based QoS."""
    if isinstance(raw, str) and raw.strip().lower() == "system":
        return "system"
    value = normalize_priority(raw, default="low")
    return value if value != "medium" else "low"


def _runtime_status_for_pid(pid: int) -> str:
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return "Stopped"
        return "Running"
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return "Stopped"


def _process_name_for_pid(pid: int, fallback: str) -> str:
    try:
        return psutil.Process(int(pid)).name() or fallback
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return fallback


def _cmdline_for_pid(pid: int, fallback: str = "") -> str:
    try:
        parts = psutil.Process(int(pid)).cmdline()
        return " ".join(parts).strip() or fallback
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return fallback


def _cgroup_for_pid(pid: int, fallback: str = "") -> str:
    try:
        path = get_cgroup_path_by_pid(int(pid))
    except Exception:
        path = None
    if not path:
        return fallback
    # Show the leaf unit/scope (e.g. "app-...-.scope") which is what users recognise.
    return os.path.basename(path.rstrip("/")) or path


def _exact_cgroup_member_pids(seed_pid: int, cgroup_id: str) -> list[int]:
    """Read every direct member of *seed_pid*'s cgroup when it is *cgroup_id*."""
    try:
        cgroup_path = get_cgroup_path_by_pid(int(seed_pid))
        if not cgroup_path or os.path.basename(cgroup_path.rstrip("/")) != cgroup_id:
            return []
        procs_file = os.path.join("/sys/fs/cgroup", cgroup_path.lstrip("/"), "cgroup.procs")
        with open(procs_file, "r") as handle:
            return [int(pid.strip()) for pid in handle if pid.strip().isdigit()]
    except (OSError, ValueError):
        return []


def _build_process_scope_snapshot(
        app_id: str,
        app_name: str,
        process_names: list,
        app_status: str,
        limited_pid_snapshot: list,
    limited_cgroup_snapshot: list,
        app_cmdline: str = "",
        limited_at: float = None,
):
    """Build per-instance status rows for a controlled app.

    Identity is name-based: for every configured process name we look up the
    live PIDs and group them by the cgroup they *currently* live in (discovered
    at read time, never from stale config).  Each distinct cgroup becomes one
    "instance" row, so two concurrent runs of the same program — even in fresh
    ``tmux-spawn-*.scope`` cgroups after a restart — show up as two rows and
    each carries its own limit status. Every configured name without a running
    instance keeps its own Stopped/Pending placeholder row so it remains
    visible and editable after the process exits.
    """
    configured_names = [str(x).strip() for x in (process_names or []) if str(x).strip()]
    limited_pids = {int(pid) for pid in (limited_pid_snapshot or []) if str(pid).isdigit()}
    limited_cgroups = {str(cg).strip() for cg in (limited_cgroup_snapshot or []) if str(cg).strip()}
    app_status_l = (app_status or "").strip().lower()
    app_cmdline = (app_cmdline or "").strip()
    app_scope = (app_id or "").strip()

    rows = []

    def _append_placeholder(display_name: str):
        runtime_status = "Pending" if app_status_l == "pending" else "Stopped"
        rows.append({
            "key": f"{app_id}:na:{display_name}",
            "pid": None,
            "process_name": display_name,
            "cmdline": app_cmdline,
            "cgroup": app_scope,
            "runtime_status": runtime_status,
            "limit_status": "N/A",
            "applied_at": None,
            "note": "Awaiting relaunch" if runtime_status == "Pending" else "-",
        })

    query_names = configured_names or [app_name, os.path.basename(app_id)]
    by_cgroup: dict[str, list[int]] = {}
    display_name_by_cgroup: dict[str, str] = {}
    matched_names: set[str] = set()
    for query_name in dict.fromkeys(name for name in query_names if name):
        pids = get_app_processes_for_app(query_name, app_id=app_id, app_name=app_name)
        for pid in sorted(set(pids)):
            if _runtime_status_for_pid(pid) != "Running":
                continue
            matched_names.add(query_name.lower())
            cgroup_id = _cgroup_for_pid(pid, app_scope)
            by_cgroup.setdefault(cgroup_id, []).append(pid)
            display_name_by_cgroup.setdefault(cgroup_id, query_name)

    for cgroup_id, cg_pids in sorted(by_cgroup.items()):
        # Preserve an identity-matched PID as the row representative. A session scope
        # can also contain sshd, a shell and its launcher; sorting all scope members and
        # picking the smallest PID would display sshd as the controlled application.
        representative_pid = min(cg_pids)
        scope_pids = sorted(set(cg_pids))
        # Name matching identifies the app, but a scope can also contain its workers
        # and child processes. Include every still-live process from this exact scope.
        scope_member_pids = [
            pid for pid in _exact_cgroup_member_pids(scope_pids[0], cgroup_id)
            if _runtime_status_for_pid(pid) == "Running"
            and _cgroup_for_pid(pid, app_scope) == cgroup_id
        ]
        scope_pids = sorted(set(cg_pids) | set(scope_member_pids))
        if limited_pids:
            is_limited = any(pid in limited_pids for pid in scope_pids)
            note = "Applied" if is_limited else "Started after last limit"
        elif limited_cgroups:
            is_limited = cgroup_id in limited_cgroups
            note = "Applied" if is_limited else "Started after last limit"
        else:
            is_limited = app_status_l in {"limited", "a_limited"}
            note = "Applied" if is_limited else "-"
        rows.append({
            "key": f"{app_id}:{cgroup_id}",
            "pid": representative_pid,
            "process_name": display_name_by_cgroup[cgroup_id],
            "cmdline": _cmdline_for_pid(representative_pid, app_cmdline),
            "scope_processes": [{
                "pid": pid,
                "process_name": _process_name_for_pid(pid, display_name_by_cgroup[cgroup_id]),
                "cmdline": _cmdline_for_pid(pid, app_cmdline),
            } for pid in scope_pids],
            "cgroup": cgroup_id,
            "runtime_status": "Running",
            "limit_status": "Limited" if is_limited else "Not Limited",
            "applied_at": limited_at if is_limited else None,
            "note": note,
        })

    for name in configured_names:
        if name.lower() not in matched_names:
            _append_placeholder(name)
    if not rows:
        _append_placeholder(app_name or app_id)

    running_rows = [r for r in rows if r.get("runtime_status") == "Running"]
    limited_running = [r for r in running_rows if r.get("limit_status") == "Limited"]

    if not running_rows:
        app_summary_status = "No Running Process"
        runtime_hint = "Pending" if app_status_l == "pending" else "Stopped"
    else:
        runtime_hint = "Running"
        if len(limited_running) == len(running_rows):
            app_summary_status = "Limited"
        elif limited_running:
            app_summary_status = "Partial Limited"
        else:
            app_summary_status = "Not Limited"

    return rows, app_summary_status, runtime_hint



@app.route('/app/get_apps', methods=['GET', 'POST'])
def get_apps():
    """Retrieve all system application entries and optionally sync them to the database."""
    try:
        data = request.get_json()
        store = data.get('store', False)
        app_list = fetch_all_apps()
        # Apps whose config.yaml entry was deleted by hand keep their row (the
        # startup reconciliation only un-controls them), so offer them for
        # re-enabling too -- otherwise the only way back is the wizard, which
        # would lose the priority and limit overrides the row still holds.
        # Appended after the loop below on purpose: they already have a row, and
        # they carry no config entry to sync one from.
        unregistered = fetch_unregistered_apps()
        for app in app_list:
            if store:
                app_id = app["app_id"]
                existing_app = None

                try:
                    existing_app = AIAppPriority.query().where(AIAppPriority.app_id == app_id).get()
                except Exception:
                    pass

                if not existing_app:
                    AIAppPriority.insert_record(
                        id=app_id,
                        app_id=app_id,
                        name=app["name"],
                        priority=0,
                        controlled=False,
                        remark="",
                        cmdline=app["commandline"],
                        status="NA",
                        last_update_time=datetime.now()
                    )

        return construct_response(
            data=app_list + unregistered,
            retmsg="Successfully retrieved app list"
        )
    except Exception as e:
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e),
            data={}
        )


@app.route('/app/set_priority', methods=['POST'])
def set_priority():
    """Set the priority of an application and update the database."""
    try:
        data = request.get_json()
        app_id = data.get('app_id')
        raw_priority = data.get('priority')

        if not app_id or raw_priority is None or raw_priority == "":
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Missing required parameters"
            )

        priority = normalize_priority(raw_priority)

        result = AIAppPriority.update_record(
            id=app_id,
            priority=priority,
            up_time=datetime.now()
        )

        logger.info(f"Set priority result for app_id={app_id}: {result}")

        if result == DBStatus.NOT_FOUND:
            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg="Application record not found in database"
            )

        _service.rebuild_controlled_map()
        app_record = AIAppPriority.query().where(AIAppPriority.app_id == app_id).get()
        if app_record:
            logger.debug(f"Updating OOM priority for app_id={app_id}, name={app_record.name}, priority={priority}, "
                         f"cmdline={app_record.cmdline}")
            adjust_oom_priority(app_id, app_record.name, priority, app_record.cmdline)

        return construct_response(
            data={},
            retmsg="Priority updated successfully"
        )
    except Exception as e:
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/set_network_priority', methods=['POST'])
def set_network_priority():
    """Set the network priority used for class-based network QoS."""
    try:
        data = request.get_json()
        app_id = data.get('app_id')
        raw_network_priority = data.get('network_priority')

        if not app_id or raw_network_priority is None or raw_network_priority == "":
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Missing required parameters"
            )

        network_priority = normalize_network_priority(raw_network_priority)

        result = AIAppPriority.update_record(
            id=app_id,
            network_priority=network_priority,
            up_time=datetime.now()
        )

        logger.info(f"Set network priority result for app_id={app_id}: {result}")

        if result == DBStatus.NOT_FOUND:
            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg="Application record not found in database"
            )

        return construct_response(
            data={},
            retmsg="Network priority updated successfully"
        )
    except Exception as e:
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/get_priority_data', methods=['POST'])
def get_priority_data():
    """Retrieve the priority settings for an app by app_id or name."""

    try:
        data = request.get_json()
        app_id = data.get('app_id', "")
        name = data.get('app_name', "")


        query = AIAppPriority.query()
        conditions = []
        if app_id:
            conditions.append(AIAppPriority.app_id == app_id)
        if name:
            conditions.append(AIAppPriority.name == name)

        if not conditions:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Either app_id or app_name is required"
            )

        query = query.where(conditions[0])
        record = query.first()

        if not record:
            not_found_msg = "No matching application found"
            if app_id and name:
                not_found_msg = f"No application found with app_id={app_id} or name={name}"
            elif app_id:
                not_found_msg = f"No application found with app_id={app_id}"
            elif name:
                not_found_msg = f"No application found with name={name}"

            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg=not_found_msg
            )


        priority_data = {
            "id": record.id,
            "app_id": record.app_id,
            "name": record.name,
            "priority": record.priority,
            "network_priority": getattr(record, "network_priority", None) or record.priority,
            "cgroup": record.cgroup,
            "remark": record.remark,
            "cmdline": record.cmdline,
            "up_time": record.up_time.isoformat() if record.up_time else None,
            "status": record.status
        }

        return construct_response(
            data=priority_data,
            retmsg="Successfully retrieved priority data"
        )
    except Exception as e:
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/set_to_control', methods=['POST'])
def set_to_control():
    """Enable or disable control for an application and register it with the monitor."""
    try:
        data = request.get_json()
        app_name = data.get('app_name', "")
        app_id = data.get('app_id', "")
        controlled = data.get('controlled', True)
        cgroup = data.get('cgroup', '')
        priority = data.get('priority', 0)
        priority = normalize_priority(priority)
        network_priority = normalize_network_priority(data.get('network_priority') or priority)
        remark = data.get('remark', '')
        cmdline = data.get('cmdline', '')

        _service.add_control(app_name)

        # Re-enabling an app whose controlled_apps entry was deleted by hand has
        # to put that entry back first: everything config-driven (BPF match
        # cache, process_names, multi-process maps) keys off it, and the next
        # startup reconciliation would otherwise just un-control the app again.
        if controlled:
            restore_config_entry(app_id, app_name=app_name, cmdline=cmdline)
            # A prior manual restore deliberately excludes the app from automatic
            # limiting. Explicitly taking it under control supersedes that choice,
            # so it must not remain in both Manual Control and Excluded.
            _service.remove_auto_limit_exclusion(app_id)

        update_fields = dict(
            controlled=controlled,
            priority=priority,
            network_priority=network_priority,
            cgroup=cgroup,
            remark=remark,
        )
        # Only persist name when a valid value was provided; never overwrite with an empty string
        if app_name and app_name.strip():
            update_fields["name"] = app_name
        result = AIAppPriority.update_record(id=app_id, **update_fields)

        if result == DBStatus.NOT_FOUND:
            AIAppPriority.insert_record(
                id=app_id,
                app_id=app_id,
                name=app_name,
                priority=priority,
                network_priority=network_priority,
                controlled=controlled,
                cgroup=cgroup,
                remark=remark,
                cmdline=cmdline,
                status="NA",
                last_update_time=datetime.now()
            )

        _service.rebuild_controlled_map()
        adjust_oom_priority(app_id, app_name, priority, cmdline)

        # After registering the app, probe whether it is already running so the
        # UI reflects the correct status immediately (without waiting for the next
        # BPF exec event).
        if controlled and app_id:
            status = check_app_running_status(app_id, app_name, cmdline)
            logger.info(f"set_to_control: initial status check for '{app_name}' → {status}")
            # If the app is already running, adopt its PIDs into the BPF
            # tracker.  Without this the eventual exit BPF events would be
            # ignored (no entry in monitored_app_launched) and the UI would
            # remain stuck on "running" after the user closes the app.
            if status == "running":
                _service.register_running_pids(app_id, app_name, cmdline)
            callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': status,
                'purpose': "app"
            }, store=True)

        return construct_response(
            data={
                "app_name": app_name,
                "controlled": controlled,
            },
            retmsg=f"App control {'enabled' if controlled else 'disabled'} and added to monitor"
        )
    except Exception as e:
        logger.error(f"Control set failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/discover_search', methods=['POST'])
def discover_search():
    """Wizard: scan /proc for processes matching user-provided keywords."""
    try:
        from monitor import app_discovery

        data = request.get_json(silent=True) or {}
        keywords = data.get('keywords') or []
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords = [k for k in keywords if isinstance(k, str)]
        if not keywords:
            return construct_response(
                data={"candidates": []},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="At least one keyword is required"
            )

        candidates = app_discovery.search_processes(keywords)
        return construct_response(
            data={
                "count": len(candidates),
                "candidates": [app_discovery.candidate_to_dict(c) for c in candidates],
            },
            retmsg=f"Found {len(candidates)} candidate(s) for keywords {keywords}"
        )
    except Exception as e:
        logger.error(f"discover_search failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/discover_extract', methods=['POST'])
def discover_extract():
    """Wizard: extract app fields from user-selected PIDs."""
    try:
        from monitor import app_discovery

        data = request.get_json(silent=True) or {}
        raw_pids = data.get('pids') or []
        pids = []
        for p in raw_pids:
            try:
                pids.append(int(p))
            except (TypeError, ValueError):
                continue
        if not pids:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="At least one pid is required"
            )

        name = (data.get('name') or '').strip()
        result = app_discovery.extract_fields(pids, name=name)
        return construct_response(
            data=app_discovery.extract_to_dict(result),
            retmsg=f"Extracted fields from {len(pids)} pid(s)"
        )
    except Exception as e:
        logger.error(f"discover_extract failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/new_controlled_app', methods=['POST'])
def new_controlled_app():
    """Wizard final step: register a new managed application."""
    try:
        from config.config import b_config

        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        app_id = (data.get('id') or '').strip()
        if not name or not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Both 'name' and 'id' are required"
            )

        priority = data.get('priority') or 'low'
        commandline = data.get('commandline') or ''
        remark = (data.get('remark') or '').strip()
        bpf_name = list(data.get('bpf_name') or [])
        process_names = list(data.get('process_names') or [])

        # Reject duplicates so the wizard never silently shadows an
        # already-controlled app.  Three flavours of conflict, each with a
        # specific message so the user can fix it without guessing:
        #   1. same id          — DB primary-key collision.
        #   2. same name        — would render as two indistinguishable rows.
        #   3. overlapping       — different name+id but the BPF / pgrep
        #      bpf_name or         match cache would route the same comm or
        #      process_names       exe to whichever entry rebuilds last,
        #                          so the second entry is effectively dead.
        existing = [item for item in (getattr(b_config, "controlled_apps", None) or [])
                    if isinstance(item, dict)]
        name_lower = name.lower()
        new_bpf = {b.lower() for b in bpf_name if b}
        new_procs = {p.lower() for p in process_names if p}

        for item in existing:
            existing_id = item.get("id", "")
            existing_name = item.get("name", "")
            if existing_id == app_id:
                return construct_response(
                    data={"conflict": "id", "with": existing_name, "with_id": existing_id},
                    retcode=RetCode.CONFLICT,
                    retmsg=(
                        f"An app with id '{app_id}' already exists. If it is the "
                        f"app you want to control, just enable it from the "
                        f"Application dropdown — no need to use the wizard."
                    ),
                )
            if (existing_name or "").lower() == name_lower:
                return construct_response(
                    data={"conflict": "name", "with": existing_name, "with_id": existing_id},
                    retcode=RetCode.CONFLICT,
                    retmsg=(
                        f"An app named '{name}' already exists. If it is the same "
                        f"app, enable it from the Application dropdown instead. "
                        f"To re-add it from scratch, purge the existing entry first."
                    ),
                )
            existing_bpf = {b.lower() for b in (item.get("bpf_name") or []) if b}
            existing_procs = {p.lower() for p in (item.get("process_names") or []) if p}
            bpf_overlap = new_bpf & existing_bpf
            proc_overlap = new_procs & existing_procs
            if bpf_overlap or proc_overlap:
                shared = sorted(bpf_overlap | proc_overlap)
                return construct_response(
                    data={
                        "conflict": "processes",
                        "with": existing_name,
                        "with_id": existing_id,
                        "shared": shared,
                    },
                    retcode=RetCode.CONFLICT,
                    retmsg=(
                        f"App '{existing_name}' is already monitoring "
                        f"{', '.join(shared)}. If that is the same application, "
                        f"enable it from the Application dropdown above. To "
                        f"re-add it from scratch, purge the existing entry first."
                    ),
                )

        # 1. Persist to config.yaml.
        config_entry = {
            'name': name,
            'id': app_id,
            'commandline': commandline,
            'bpf_name': bpf_name,
            'process_names': process_names,
        }
        ok = b_config.append_to_list_section('controlled_apps', config_entry)
        if not ok:
            return construct_response(
                data={},
                retcode=RetCode.EXCEPTION_ERROR,
                retmsg="Failed to write config.yaml"
            )

        # 2. Persist to DB.  The DB ``priority`` column is a string label
        #    ("low" / "medium" / "high" / "critical") — same shape that
        #    /app/set_to_control writes — because the dashboard front-end
        #    calls ``.toLowerCase()`` on it during render.  Passing an int
        #    crashes Balance.tsx and blanks the tab.
        priority_label = normalize_priority(priority, default="low")
        network_priority_label = normalize_network_priority(priority_label)
        # Mirror the config-only fields into the row as well, so the entry stays
        # recoverable if config.yaml is later edited by hand (see
        # utils.app_utils.reconcile_controlled_apps).
        config_meta = serialize_config_meta(config_entry)
        try:
            db_result = AIAppPriority.insert_record(
                id=app_id,
                app_id=app_id,
                name=name,
                priority=priority_label,
                network_priority=network_priority_label,
                controlled=True,
                cgroup='',
                remark=remark,
                cmdline=commandline,
                status="NA",
                config_meta_json=config_meta,
                last_update_time=datetime.now(),
            )

            # insert_record() returns ALREADY_EXISTING when the id already
            # exists (e.g. app previously added then uncontrolled). In that
            # case we must re-enable control on the existing row so
            # /app/get_controlled_app can see it immediately after wizard
            # finish.
            if db_result != DBStatus.SUCCESS:
                update_result = AIAppPriority.update_record(
                    id=app_id,
                    app_id=app_id,
                    name=name,
                    priority=priority_label,
                    network_priority=network_priority_label,
                    controlled=True,
                    cgroup='',
                    remark=remark,
                    cmdline=commandline,
                    status="NA",
                    config_meta_json=config_meta,
                )
                if update_result != DBStatus.SUCCESS:
                    logger.warning(
                        "new_controlled_app: DB upsert did not confirm success "
                        f"(insert={db_result}, update={update_result}) for app_id={app_id}"
                    )
        except Exception as db_exc:
            logger.warning(f"new_controlled_app: DB upsert failed (continuing): {db_exc}")

        # 3. Refresh the BPF match cache so this app is watched immediately.
        _service.add_control(name)
        _service.rebuild_controlled_map()

        # 4. Probe the running state immediately so the UI reflects "running"
        #    without waiting for the next BPF exec event.  Mirrors the
        #    /app/set_to_control behavior so apps added via the wizard get
        #    the same initial-status update as ones added manually.
        try:
            status = check_app_running_status(app_id, name, commandline)
            logger.info(f"new_controlled_app: initial status check for '{name}' → {status}")
            # Same as /app/set_to_control: if already running, adopt the PIDs
            # so the BPF tracker can later report "stopped" when the app ends.
            if status == "running":
                _service.register_running_pids(app_id, name, commandline)
            callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': name,
                'status': status,
                'purpose': "app",
            }, store=True)
        except Exception as status_exc:
            logger.warning(f"new_controlled_app: initial status check failed: {status_exc}")

        return construct_response(
            data={"name": name, "id": app_id},
            retmsg=f"Application '{name}' added"
        )
    except Exception as e:
        logger.error(f"new_controlled_app failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/merge_controlled_app_processes', methods=['POST'])
def merge_controlled_app_processes():
    """Add discovered process identities to an existing controlled app."""
    try:
        from config.config import b_config

        data = request.get_json(silent=True) or {}
        app_id = (data.get('id') or '').strip()
        if not app_id:
            return construct_response(
                data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg="'id' is required"
            )

        existing = [item for item in (getattr(b_config, "controlled_apps", None) or [])
                    if isinstance(item, dict)]
        target_index = next((index for index, item in enumerate(existing)
                             if item.get("id") == app_id), None)
        if target_index is None:
            return construct_response(
                data={}, retcode=RetCode.NOT_EXISTING,
                retmsg=f"No controlled_apps entry with id '{app_id}'"
            )

        target = dict(existing[target_index])

        def merge_names(current, incoming):
            merged = []
            seen = set()
            for value in list(current or []) + list(incoming or []):
                value = str(value).strip()
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    merged.append(value)
            return merged

        target['process_names'] = merge_names(target.get('process_names'), data.get('process_names'))
        target['bpf_name'] = merge_names(target.get('bpf_name'), data.get('bpf_name'))
        existing[target_index] = target
        if not b_config.set_list_section('controlled_apps', existing):
            return construct_response(
                data={}, retcode=RetCode.EXCEPTION_ERROR,
                retmsg="Failed to update config.yaml"
            )

        meta = serialize_config_meta(target)
        try:
            AIAppPriority.update_record(id=app_id, config_meta_json=meta)
        except Exception as db_exc:
            logger.warning("merge_controlled_app_processes: DB metadata update failed: %s", db_exc)

        _service.rebuild_controlled_map()
        return construct_response(
            data={"id": app_id, "name": target.get('name', ''),
                  "process_names": target['process_names'], "bpf_name": target['bpf_name']},
            retmsg=f"Process identities merged into '{target.get('name') or app_id}'"
        )
    except Exception as e:
        logger.error(f"merge_controlled_app_processes failed: {str(e)}")
        return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))


def _limited_process_names_for_app(app_id: str, cfg_entry: dict) -> set:
    """Program names that currently have at least one instance under a live limit.

    Used to guard identity edits: dropping such a name would strand the running
    throttle behind an entry that no longer references it.  Reuses the same
    per-instance snapshot the controlled-apps table is built from, so the
    "which name is limited" answer matches exactly what the UI shows.
    """
    try:
        snap = _service.get_limit_snapshot(app_id) or {}
    except Exception:
        return set()
    if not snap.get('limited'):
        return set()

    try:
        record = AIAppPriority.query().where(AIAppPriority.app_id == app_id).get()
    except Exception:
        record = None

    app_name = (cfg_entry.get('name') or (record.name if record else '') or '')
    cmdline = (record.cmdline if record else '') or cfg_entry.get('commandline', '') or ''
    rows, _, _ = _build_process_scope_snapshot(
        app_id=app_id,
        app_name=app_name,
        process_names=cfg_entry.get('process_names', []) or [],
        app_status=(record.status if record else ''),
        limited_pid_snapshot=snap.get('pids', []),
        limited_cgroup_snapshot=snap.get('cgroups', []),
        app_cmdline=cmdline,
        limited_at=snap.get('limited_at'),
    )
    return {
        (row.get('process_name') or '').strip()
        for row in rows
        if row.get('limit_status') == 'Limited' and (row.get('process_name') or '').strip()
    }


@app.route('/app/set_controlled_app_processes', methods=['POST'])
def set_controlled_app_processes():
    """Replace an existing app's process/BPF identities (add *and* remove).

    Unlike /app/merge_controlled_app_processes (add-only), this overwrites the
    lists with exactly what the caller sends, so the Edit dialog can prune stale
    names.  Guard: a program name whose instance is currently under an active
    manual limit may NOT be dropped -- doing so would leave the live cgroup
    throttle referenced by nothing, so the UI could never restore it.  The
    dashboard already locks those names in the Edit dialog; this is the
    server-side safety net for direct callers and races.
    """
    try:
        from config.config import b_config

        data = request.get_json(silent=True) or {}
        app_id = (data.get('id') or '').strip()
        if not app_id:
            return construct_response(
                data={}, retcode=RetCode.ARGUMENT_ERROR, retmsg="'id' is required"
            )

        existing = [item for item in (getattr(b_config, "controlled_apps", None) or [])
                    if isinstance(item, dict)]
        target_index = next((index for index, item in enumerate(existing)
                             if item.get("id") == app_id), None)
        if target_index is None:
            return construct_response(
                data={}, retcode=RetCode.NOT_EXISTING,
                retmsg=f"No controlled_apps entry with id '{app_id}'"
            )

        def clean_names(values):
            cleaned = []
            seen = set()
            for value in (values or []):
                value = str(value).strip()
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    cleaned.append(value)
            return cleaned

        new_process_names = clean_names(data.get('process_names'))
        new_bpf_name = clean_names(data.get('bpf_name'))

        if not new_process_names:
            return construct_response(
                data={}, retcode=RetCode.ARGUMENT_ERROR,
                retmsg="At least one program name is required"
            )

        target = dict(existing[target_index])

        # Safety net: refuse to drop a program name that currently owns a live limit.
        limited_names = _limited_process_names_for_app(app_id, target)
        keep = {name.lower() for name in new_process_names}
        dropped_limited = sorted(
            name for name in limited_names if name.lower() not in keep
        )
        if dropped_limited:
            return construct_response(
                data={"limited": dropped_limited},
                retcode=RetCode.OPERATING_ERROR,
                retmsg=(
                    "Cannot remove process name(s) that are currently limited: "
                    f"{', '.join(dropped_limited)}. Restore the limit first, then edit."
                ),
            )

        target['process_names'] = new_process_names
        target['bpf_name'] = new_bpf_name
        existing[target_index] = target
        if not b_config.set_list_section('controlled_apps', existing):
            return construct_response(
                data={}, retcode=RetCode.EXCEPTION_ERROR,
                retmsg="Failed to update config.yaml"
            )

        meta = serialize_config_meta(target)
        try:
            AIAppPriority.update_record(id=app_id, config_meta_json=meta)
        except Exception as db_exc:
            logger.warning("set_controlled_app_processes: DB metadata update failed: %s", db_exc)

        _service.rebuild_controlled_map()
        return construct_response(
            data={"id": app_id, "name": target.get('name', ''),
                  "process_names": new_process_names, "bpf_name": new_bpf_name},
            retmsg=f"Process identities updated for '{target.get('name') or app_id}'"
        )
    except Exception as e:
        logger.error(f"set_controlled_app_processes failed: {str(e)}")
        return construct_response(data={}, retcode=RetCode.EXCEPTION_ERROR, retmsg=str(e))


@app.route('/app/purge_controlled_app', methods=['POST'])
def purge_controlled_app():
    """Hard-delete an app from both config.yaml and the DB."""
    try:
        from config.config import b_config

        data = request.get_json(silent=True) or {}
        app_id = (data.get('id') or '').strip()
        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="'id' is required"
            )

        existing = [item for item in (getattr(b_config, "controlled_apps", None) or [])
                    if isinstance(item, dict)]
        target = next((item for item in existing if item.get("id") == app_id), None)
        if target is None:
            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg=f"No controlled_apps entry with id '{app_id}'"
            )

        target_name = target.get("name") or ""

        # Never remove the only UI/config reference to an active cgroup limit.
        # Auto limits follow their own restore protocol and are not deletable from
        # the UI; manual limits must be fully lifted before the app is purged.
        limit_snapshot = _service.get_limit_snapshot(app_id)
        if limit_snapshot.get('limited'):
            if limit_snapshot.get('source') == 'auto':
                return construct_response(
                    data={}, retcode=RetCode.OPERATING_ERROR,
                    retmsg="Restore the auto limit or take control before deleting this app"
                )
            if not _service.restore_resource(app_id):
                return construct_response(
                    data={}, retcode=RetCode.OPERATING_ERROR,
                    retmsg="Could not restore active resource limits; the app was not deleted"
                )

        # 1. Remove from config.yaml (preserves comments via the generic helper).
        removed_count = b_config.remove_from_list_section(
            'controlled_apps', {'id': app_id}
        )
        if removed_count == 0:
            return construct_response(
                data={},
                retcode=RetCode.EXCEPTION_ERROR,
                retmsg="Failed to remove entry from config.yaml"
            )

        # 2. Restore OOM score (if any) before deleting the DB row, so the
        #    bookkeeping in adjust_oom_priority sees the priority/cmdline.
        try:
            db_app = AIAppPriority.query().filter(AIAppPriority.id == app_id).first()
            if db_app is not None:
                adjust_oom_priority(
                    app_id, target_name, db_app.priority, db_app.cmdline or "",
                    restore=True,
                )
        except Exception as oom_exc:
            logger.warning(f"purge_controlled_app: OOM restore failed (continuing): {oom_exc}")

        # 3. Hard-delete the DB row.
        try:
            AIAppPriority.delete_record(id=app_id)
        except Exception as db_exc:
            logger.warning(f"purge_controlled_app: DB delete failed (continuing): {db_exc}")

        # 4. Drop it from the BPF monitor and rebuild its cache so the app
        #    is no longer watched.
        if target_name:
            _service.remove_control(target_name)
        _service.rebuild_controlled_map()

        return construct_response(
            data={"id": app_id, "name": target_name},
            retmsg=f"Application '{target_name or app_id}' purged; you can now re-add it"
        )
    except Exception as e:
        logger.error(f"purge_controlled_app failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/get_controlled_app', methods=['POST'])
def get_controlled_app():
    """Return all controlled applications along with their current metadata."""
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)

        if not controlled_apps:
            return construct_response(
                retcode=RetCode.NOT_EXISTING,
                retmsg="No controlled apps found",
                data=[]
            )

        # Build a lookup map from config/system apps so we can fill in metadata
        config_app_map = {a["app_id"]: a for a in fetch_all_apps()}

        result_data = []
        for app in controlled_apps:
            # Prefer the DB name, or fall back to the config-derived human-readable name
            cfg_app = config_app_map.get(app.app_id, {})
            app_name = app.name if app.name and app.name.strip() else (cfg_app.get("app_name") or cfg_app.get("name") or "")
            limit_snapshot = {}
            get_snapshot = getattr(_service, "get_limit_snapshot", None)
            if callable(get_snapshot):
                try:
                    snap = get_snapshot(app.app_id)
                    if isinstance(snap, dict):
                        limit_snapshot = snap
                except Exception as e:
                    logger.debug(f"get_limit_snapshot failed for {app.app_id}: {e}")

            process_rows, app_summary_status, runtime_hint = _build_process_scope_snapshot(
                app_id=app.app_id,
                app_name=app_name,
                process_names=cfg_app.get("process_names", []) or [],
                app_status=app.status,
                limited_pid_snapshot=limit_snapshot.get("pids", []),
                limited_cgroup_snapshot=limit_snapshot.get("cgroups", []),
                app_cmdline=app.cmdline or "",
                limited_at=limit_snapshot.get("limited_at"),
            )

            result_data.append({
                "app_id": app.app_id,
                "app_name": app_name,
                "controlled": app.controlled,
                "priority": app.priority,
                "network_priority": getattr(app, "network_priority", None) or app.priority,
                "oom_score": app.oom_score,
                "cmdline": app.cmdline,
                "cgroup": app.cgroup,
                "process_names": cfg_app.get("process_names", []) or [],
                "bpf_name": cfg_app.get("bpf_name", []) or [],
                "remark": app.remark,
                "status": app.status,
                "app_summary_status": app_summary_status,
                "runtime_hint": runtime_hint,
                "process_status_rows": process_rows,
                # Unified control contract for the merged management table:
                # NORMAL when the app holds no runtime limit, else the snapshot's
                # MANUAL_LIMITED / AUTO_LIMITED plus its multi-dimensional effective
                # view and (auto-only) pressure detail.
                "control_status": limit_snapshot.get("control_status", "NORMAL"),
                "effective": limit_snapshot.get("effective"),
                "auto_detail": limit_snapshot.get("auto_detail"),
                # The limit registry is authoritative while process discovery is live
                # data and may briefly find no matching process after a relaunch.
                "limited_scopes": limit_snapshot.get("cgroups", []),
            })

        return construct_response(
            data=result_data,
            retmsg=f"Found {len(result_data)} controlled apps"
        )
    except Exception as e:
        logger.error(f"Get controlled apps failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/check_running_apps', methods=['POST'])
def check_running_apps():
    """Scan currently running processes to find managed apps that started before the balancer.

    This endpoint is called once when the UI balancer tab is first opened.  It
    uses psutil to inspect live processes and registers any monitored app that is
    already running so its status is reflected correctly in the UI.  Ongoing
    detection after this initial scan is handled by BPF as usual.
    """
    try:
        detected = _service.check_running_apps()
        return construct_response(
            data=detected,
            retmsg=f"Startup scan complete, detected {len(detected)} pre-existing monitored app(s)"
        )
    except Exception as e:
        logger.error(f"check_running_apps failed: {str(e)}")
        return construct_response(
            data=[],
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/get_pending_app', methods=['POST'])
def get_pending_app():
    """Return all applications currently in pending state, ordered by priority."""
    try:
        pending_apps = AIAppPriority.query().filter(AIAppPriority.status == "pending")

        if not pending_apps:
            return construct_response(
                retcode=RetCode.NOT_EXISTING,
                retmsg="No pending apps found",
                data=[]
            )

        logger.debug(f"Found {len(pending_apps)} pending apps in database, pending_apps: {pending_apps}")

        result_data = []
        for app in pending_apps:
            result_data.append({
                "app_id": app.app_id,
                "app_name": app.name,
                "controlled": app.controlled,
                "priority": app.priority,
                "oom_score": app.oom_score,
                "priority_value": get_priority_value(app.priority),
                "cgroup": app.cgroup,
                "remark": app.remark,
                "status": app.status
            })


        sorted_data = sorted(result_data, key=lambda x: -x["priority_value"])
        logger.debug(f"Sorted pending apps: {sorted_data}")

        return construct_response(
            data=sorted_data,
            retmsg=f"Found {len(sorted_data)} pending apps (sorted by priority DESC)"
        )
    except Exception as e:
        logger.error(f"Get pending apps failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/set_oom_score', methods=['POST'])
def set_oom_score():
    """Set the OOM score for an application to protect it from the OOM killer."""
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")

        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id must be provided"
            )

        app_info = AIAppPriority.query().filter(AIAppPriority.app_id == app_id).first()

        logger.debug(f"set_oom_score: app_info: {app_info}")
        adjust_oom_priority(app_id, app_info.name, app_info.priority, app_info.cmdline)

        return construct_response(
            data={},
            retmsg="App OOM score set successfully"
        )
    except Exception as e:
        logger.error(f"Set OOM score failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/kill_process', methods=['POST'])
def kill_process_route():
    """Terminate (SIGTERM) or force-kill (SIGKILL) an arbitrary process by PID."""
    try:
        data = request.get_json() or {}
        pid = data.get('pid')
        force = bool(data.get('force'))

        if not isinstance(pid, int):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="pid must be an integer"
            )

        ok, msg = kill_process(pid, force)
        return construct_response(
            data={},
            retcode=RetCode.SUCCESS if ok else RetCode.OPERATING_ERROR,
            retmsg=msg
        )
    except Exception as e:
        logger.error(f"Kill process failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/suspend_process', methods=['POST'])
def suspend_process_route():
    """Freeze (SIGSTOP) or resume (SIGCONT) an arbitrary process by PID."""
    try:
        data = request.get_json() or {}
        pid = data.get('pid')
        resume = bool(data.get('resume'))

        if not isinstance(pid, int):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="pid must be an integer"
            )

        ok, msg = suspend_process(pid, resume)
        return construct_response(
            data={},
            retcode=RetCode.SUCCESS if ok else RetCode.OPERATING_ERROR,
            retmsg=msg
        )
    except Exception as e:
        logger.error(f"Suspend process failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/cancel_relaunch', methods=['POST'])
def cancel_relaunch_app():
    """ Cancel relaunch for a specific app by app_id. """
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")

        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Either app_id must be provided"
            )

        result = _service.cancel_relaunch(app_id)

        try:
            update_db_result = AIAppPriority.update_record(
                id=app_id,
                status="stopped",
                up_time=datetime.now()
            )
        except Exception as db_error:
            logger.error(f"Update database failed for {app_id}: {str(db_error)}")
            update_db_result = False

        if result and update_db_result:
            return construct_response(
                data={"app_id": app_id},
                retmsg="Successfully found and canceled relaunch"
            )
        else:
            return construct_response(
                data={"app_id": app_id},
                retcode=RetCode.OPERATING_ERROR,
                retmsg="No matching app found or failed to cancel relaunch it"
            )
    except Exception as e:
        logger.error(f"Cancel relaunch failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/resource_limit', methods=['POST'])
def app_resource_limit():
    """ Set resource limit for a specific app by app_id. """
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")
        app_name = data.get('app_name', "")
        priority = data.get('priority', "")
        limit_overrides = data.get('limit_overrides')
        target_cgroups = data.get('target_cgroups') or None

        if not app_id and not app_name and not priority:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id, app_name and priority must be provided"
            )

        result = _service.resource_limit(
            app_id, app_name, priority,
            limit_overrides=limit_overrides,
            target_cgroups=target_cgroups,
        )

        # set_resource_limit signals "intentionally skipped" with {"skipped": reason}.
        # That's a successful evaluation, not a failure — return 200 + the reason as
        # retmsg so the UI can show one notification and close the limit dialog,
        # instead of falling through to the OPERATING_ERROR branch (which would also
        # trigger a duplicate SSE 'limit_skipped' toast).
        if isinstance(result, dict) and "skipped" in result:
            return construct_response(
                data={"skipped": True},
                retmsg=result["skipped"],
            )
        if result:
            return construct_response(
                data={},
                retmsg="Successfully found and set resource limit"
            )
        return construct_response(
            data={},
            retcode=RetCode.OPERATING_ERROR,
            retmsg="No matching app found or failed to set resource limit"
        )
    except Exception as e:
        logger.error(f"Set resource limit failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/resource_limit_profile', methods=['POST'])
def app_resource_limit_profile():
    """Get editable resource-limit profile (defaults + bounds) for UI."""
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")
        app_name = data.get('app_name', "")
        priority = data.get('priority', "")

        if not app_id and not app_name:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id or app_name must be provided"
            )

        profile = _service.resource_limit_profile(app_id, app_name, priority or "undefined")
        return construct_response(
            data=profile,
            retmsg="Successfully fetched resource limit profile"
        )
    except Exception as e:
        logger.error(f"Get resource limit profile failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/resource_restore', methods=['POST'])
def app_resource_restore():
    """ Restore resource for a specific app by app_id. """
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")

        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id and app_name must be provided"
            )

        result = _service.restore_resource(app_id)

        if result:
            return construct_response(
                data={},
                retmsg="Successfully found and restored resource"
            )
        else:
            return construct_response(
                data={},
                retcode=RetCode.OPERATING_ERROR,
                retmsg="No matching app found or failed to restore resource"
            )
    except Exception as e:
        logger.error(f"Restore resource failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/auto_limited_apps', methods=['POST'])
def auto_limited_apps():
    """List the apps the balancer is currently auto-limiting (pressure-driven only)."""
    try:
        payload = _service.get_auto_limited_apps()
        return construct_response(
            data=payload,
            retmsg=f"Found {len(payload.get('apps', []))} auto-limited apps"
        )
    except Exception as e:
        logger.error(f"Get auto-limited apps failed: {str(e)}")
        return construct_response(
            data={"apps": [], "sys_pressure_level": "", "disk_pressure_level": ""},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/auto_limit_restore', methods=['POST'])
def auto_limit_restore():
    """Restore an auto-limited app on user request and exclude it from future auto-limits.

    Separate from /app/resource_restore, which only lifts manual limits: this one also
    takes the app out of the auto-limit candidate pool, or the next critical tick would
    simply limit it again.
    """
    try:
        data = request.get_json() or {}
        app_id = data.get('app_id', "")

        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id must be provided"
            )

        ok, msg = _service.restore_auto_limited(app_id)
        if ok:
            return construct_response(data={}, retmsg=msg)
        return construct_response(
            data={},
            retcode=RetCode.OPERATING_ERROR,
            retmsg=msg
        )
    except Exception as e:
        logger.error(f"Restore auto-limited app failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/lock_to_manual', methods=['POST'])
def lock_to_manual():
    """Take an auto-limited app over as a manual limit without releasing its cgroup caps.

    The "safe handoff": ownership flips auto->manual with the kernel throttle left
    exactly in place, so no crash window opens under sustained pressure. After this
    the app is MANUAL_LIMITED and the manual Limit/Restore/Edit buttons unlock.
    """
    try:
        data = request.get_json() or {}
        app_id = data.get('app_id', "")

        if not app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="app_id must be provided"
            )

        ok, msg = _service.lock_to_manual(app_id)
        if ok:
            return construct_response(data={}, retmsg=msg)
        return construct_response(
            data={},
            retcode=RetCode.OPERATING_ERROR,
            retmsg=msg
        )
    except Exception as e:
        logger.error(f"Lock auto-limited app to manual failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/adopt_auto_limit', methods=['POST'])
def adopt_auto_limit():
    """Adopt a running auto-limit into a newly-controlled app identity ("Take Control").

    Re-tags the live entry in place (controlled=True, public id -> new app_id) so the
    limit follows the app into management with no cgroup release and no duplicate row.
    """
    try:
        data = request.get_json() or {}
        effective_app_id = data.get('effective_app_id', "")
        new_app_id = data.get('app_id', "")

        if not effective_app_id or not new_app_id:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="effective_app_id and app_id must be provided"
            )

        ok, msg = _service.adopt_auto_limit(
            effective_app_id,
            new_app_id,
            new_app_name=data.get('app_name', "") or "",
            priority=data.get('priority', "") or "",
        )
        if ok:
            return construct_response(data={}, retmsg=msg)
        return construct_response(
            data={},
            retcode=RetCode.OPERATING_ERROR,
            retmsg=msg
        )
    except Exception as e:
        logger.error(f"Adopt auto-limit failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/auto_limit_exclusions', methods=['POST'])
def auto_limit_exclusions():
    """List the apps excluded from auto-limiting (runtime-only; cleared on restart)."""
    try:
        rows = _service.get_auto_limit_exclusions()
        return construct_response(
            data=rows,
            retmsg=f"Found {len(rows)} auto-limit exclusions"
        )
    except Exception as e:
        logger.error(f"Get auto-limit exclusions failed: {str(e)}")
        return construct_response(
            data=[],
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/auto_limit_exclusion_remove', methods=['POST'])
def auto_limit_exclusion_remove():
    """Put an excluded app back into the auto-limit candidate pool."""
    try:
        data = request.get_json() or {}
        ident = data.get('key') or data.get('app_id') or ""

        if not ident:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="key or app_id must be provided"
            )

        if _service.remove_auto_limit_exclusion(ident):
            return construct_response(data={}, retmsg="Auto-limit exclusion removed")
        return construct_response(
            data={},
            retcode=RetCode.OPERATING_ERROR,
            retmsg="No matching auto-limit exclusion found"
        )
    except Exception as e:
        logger.error(f"Remove auto-limit exclusion failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


_SSE_HEARTBEAT_TIMEOUT = 30  # seconds between keep-alive comments when no events arrive


@app.route('/app/events', methods=['GET'])
def app_events():
    """Server-Sent Events stream for app status changes."""
    q = _queue.Queue()
    callback_manager.add_sse_client(q)

    def generate():
        try:
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    data = q.get(timeout=_SSE_HEARTBEAT_TIMEOUT)
                    yield f"data: {json.dumps(data)}\n\n"
                except _queue.Empty:
                    # Keep-alive comment
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            callback_manager.remove_sse_client(q)

    response = Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


class _ClientDisconnectFilter(logging.Filter):
    """Silence werkzeug's noisy traceback when a client tears down a streaming
    response (typically /app/events SSE) mid-flight.

    Werkzeug logs BrokenPipeError / ssl.SSLError(UNEXPECTED_EOF_WHILE_READING)
    at ERROR level with full traceback, but these are benign – the peer
    closed the connection before we finished writing the next chunk.

    Note: werkzeug formats the traceback into the *message string*
    (server.log("error", "Error on request:\\n%s", traceback_str)) rather
    than via record.exc_info, so we have to grep the rendered message.
    """

    _BENIGN = (
        "BrokenPipeError",
        "UNEXPECTED_EOF_WHILE_READING",
        "ConnectionResetError",
        "ConnectionAbortedError",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            text = str(record.msg)
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        return not any(marker in text for marker in self._BENIGN)


def main():
    # Apply the disconnect filter both to werkzeug's logger AND to its handlers
    # (and the root logger's handlers). werkzeug's traceback travels through
    # multiple paths — sometimes via the named "werkzeug" logger, sometimes via
    # the root logger's StreamHandler — and a logger-level filter only catches
    # the former. Filtering at the handler level catches the rest.
    f = _ClientDisconnectFilter()
    for name in ("werkzeug", ""):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
    logger.info("Starting Balance Service...")
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        logger.error(f"Certificate files not found: {CERT_FILE}, {KEY_FILE}, "
                     f"please run 'start_smartune.sh' to generate them.")
        return

    init_database()
    # config.yaml decides *which* apps are managed, and it is read once at import
    # (no hot reload), so a hand-edited entry only ever takes effect here.  Must
    # precede start_service(): the BPF monitor list and the OOM adjustment are
    # seeded from the database's controlled rows once the balancer comes up.
    reconcile_controlled_apps()
    try:
        preload_static_info()
    except Exception as exc:
        logger.warning(f"Preload static info failed, will retry on first static request: {exc}")

    if not hasattr(app, "_service_initialized"):  # Make sure the service is only initialized once
        start_service()
        app._service_initialized = True

    # Create secure SSL context with recommended settings
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

    # Use environment variable for host binding, default to localhost for security
    # Set BALANCER_HOST=0.0.0.0 to bind to all interfaces if needed
    host = os.environ.get("BALANCER_HOST", "127.0.0.1")
    port = int(os.environ.get("BALANCER_PORT", "9001"))

    # werkzeug installs its own request-log handler; stop it from also
    # propagating to the root logger, which otherwise prints every access line
    # twice (once as "127.0.0.1 - -" and once as "INFO:werkzeug:...").
    logging.getLogger("werkzeug").propagate = False

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, ssl_context=ssl_context)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_service_once()


if __name__ == "__main__":
    main()
