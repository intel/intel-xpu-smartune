# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import re
import shlex
import signal
from multiprocessing import JoinableQueue
from threading import Event, Timer
from typing import Any, List, Set, Union

import psutil
from bcc import BPF
from controller.control_manager import ControlManager
from db.DatabaseModel import AIAppPriority
from utils import app_utils
from utils.logger import logger


# Constants matching those defined in the BPF C code
COMM_LEN = 32
PY_MAX_FILE_LEN = 64

# Interpreter/shell executables whose real program identity lives in the cmdline
# (e.g. "python train.py", "bash run.sh") rather than in comm/argv0.  When a
# launch event's comm/filename is one of these, the effective program name is
# resolved from the cmdline instead.
#
# Kept in sync with monitor.app_discovery so the wizard's bpf_name extraction and
# this runtime resolution mirror each other exactly.
INTERPRETERS = frozenset({
    "python", "python2", "python3", "node", "nodejs",
    "perl", "ruby", "java", "php", "lua", "rscript",
    "bash", "sh", "dash", "zsh", "fish", "tcsh",
})


def _basename_is_interpreter(name: str) -> bool:
    """True for python / python3.12 / node / ... — version suffixes tolerated."""
    n = (name or "").lower()
    if n in INTERPRETERS:
        return True
    # Strip a trailing version: "python3.12" -> "python", "ruby3.0" -> "ruby".
    base = re.split(r"\d", n, 1)[0]
    return bool(base) and base in INTERPRETERS


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AppIntercept(metaclass=SingletonMeta):
    def __init__(self, c_src_file: str = "bpf_event.c"):
        self.bpf = BPF(src_file=c_src_file, cflags=["-Wno-duplicate-decl-specifier"])
        self.control_manager = ControlManager()
        self.monitored_apps: Set[str] = set()
        self.handled_processes: Set[int] = set()  # set of already-handled process IDs
        self.controlled_app_map = []
        self._app_map_index = {}  # index of controlled_app_map for O(1) lookup
        self.relaunch_apps = {}
        self.app_pending_queue = JoinableQueue(1000000)
        self.monitored_app_launched = {}  # currently launched monitored apps
        self.pending_exit_events = {}  # pending exit events keyed by PID
        # Per-app live-PID set: app_name → set of PIDs currently believed to be
        # running.  An app emits "running" only on the first PID joining and
        # "stopped" only when the last PID leaves, so a multi-process launch
        # (e.g. a shell wrapper that exec-chains into a daemon) no longer
        # causes the UI to flicker through running/stopped/running as each
        # intermediate execve fires its own BPF event.
        self.app_live_pids: dict[str, Set[int]] = {}
        # Fast-lookup structures for get_main_process (rebuilt by _rebuild_match_cache).
        self._comm_to_app: dict = {}          # comm_lower -> app_name  (O(1) bpf_name exact match)
        self._filename_exe_to_app: dict = {}  # exe_lower  -> app_name  (filename path match)
        self._quick_filter: frozenset = frozenset()  # union of above for pre-filtering
        # Per-app match policy cache.
        # app_name_lower -> {"mode": "aggregate"|"instance", "match_cmdline": tuple[str, ...]}
        self._app_match_cfg: dict = {}

        # Event-driven critical-mode flag.  Set by _on_critical_state_changed()
        # when the system pressure monitor transitions into "critical" state;
        # cleared when it leaves.  Used in print_event to avoid issuing SIGSTOP
        # unless the system is actually under critical pressure.
        self._system_critical = Event()
        self.control_manager.register_critical_state_listener(self._on_critical_state_changed)
        # Seed the flag from the current (possibly already cached) pressure level
        # so that apps detected before the first monitor callback are handled
        # correctly at startup.  The tuple is (level, score, is_disk_io_stressed).
        initial_level, *_ = self.control_manager.get_current_pressure_level()
        if initial_level == "critical":
            self._system_critical.set()

    def rebuild_controlled_map(self):
        self.controlled_app_map = app_utils.get_controlled_apps()
        self._rebuild_index()
        self._rebuild_match_cache()

    def _on_critical_state_changed(self, is_critical: bool) -> None:
        """Callback invoked by SystemPressureMonitor when pressure enters or leaves critical.

        Sets or clears the _system_critical event so that print_event can decide
        whether to issue SIGSTOP without polling the pressure monitor on every
        BPF exec event.
        """
        if is_critical:
            self._system_critical.set()
            logger.info("System pressure entered critical – low-priority app launches will be intercepted")
        else:
            self._system_critical.clear()
            logger.info("System pressure left critical – low-priority app launch interception disabled")

    def _rebuild_index(self):
        self._app_map_index = {
            app["app_name"].lower(): app
            for app in (self.controlled_app_map or [])
            if app.get("app_name") and app["app_name"].strip()
        }

    @staticmethod
    def _prefer_persisted_limited_status(app_id: str, fallback: str = "running") -> str:
        """Keep limited/a_limited when startup scan finds a running app.

        A page refresh triggers startup scan again. Without this guard, apps
        already marked as limited can be overwritten to running.
        """
        try:
            rec = AIAppPriority.query().where(AIAppPriority.app_id == app_id).first()
            current = (getattr(rec, 'status', '') or '').lower()
            if current in {"limited", "a_limited"}:
                return current
        except Exception:
            pass
        return fallback

    def _rebuild_match_cache(self) -> None:
        """Pre-build fast-lookup structures used by get_main_process.

        Called whenever monitored_apps or the app config changes so that the
        hot-path (print_event → get_main_process) never rebuilds these dicts
        itself.  Matching is driven entirely by ``bpf_name`` entries; the
        ``name`` field is treated as a display label only.
        """
        cnf_apps = getattr(self.control_manager.config, 'controlled_apps', None) or []
        app_executables = {
            item['name']: item.get('bpf_name', []) for item in cnf_apps
        }

        comm_to_app: dict[str, str] = {}
        filename_exe_to_app: dict[str, str] = {}

        for app in self.monitored_apps:
            for exe in app_executables.get(app, []):
                exe_lower = exe.lower()
                comm_to_app[exe_lower] = app
                filename_exe_to_app[exe_lower] = app

        app_match_cfg = {}
        for item in cnf_apps:
            app_name = (item.get('name') or '').strip()
            if not app_name:
                continue

            mode = str(item.get('match_mode', 'aggregate')).strip().lower() or 'aggregate'
            if mode not in ('aggregate', 'instance'):
                mode = 'aggregate'

            raw_match_cmdline = item.get('match_cmdline', [])
            if isinstance(raw_match_cmdline, str):
                try:
                    raw_tokens = shlex.split(raw_match_cmdline)
                except ValueError:
                    raw_tokens = raw_match_cmdline.split()
            elif isinstance(raw_match_cmdline, list):
                raw_tokens = [str(x) for x in raw_match_cmdline if str(x).strip()]
            else:
                raw_tokens = []

            norm_tokens = self._normalize_cmdline(raw_tokens)
            if mode == 'instance' and not norm_tokens:
                logger.warning(
                    "App '%s' configured with match_mode=instance but match_cmdline is empty; "
                    "falling back to aggregate mode.",
                    app_name,
                )
                mode = 'aggregate'

            app_match_cfg[app_name.lower()] = {
                'mode': mode,
                'match_cmdline': norm_tokens,
            }

        self._comm_to_app = comm_to_app
        self._filename_exe_to_app = filename_exe_to_app
        self._app_match_cfg = app_match_cfg
        # Any string whose presence in comm or filename justifies a full match check
        self._quick_filter = frozenset(
            set(comm_to_app.keys()) | set(filename_exe_to_app.keys())
        )

    def _read_proc_cmdline(self, pid: int) -> list[str]:
        """Read full cmdline argv from /proc/<pid>/cmdline."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            if not raw:
                return []
            return [x.decode('utf-8', 'ignore') for x in raw.split(b'\x00') if x]
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            return []

    def _normalize_cmdline(self, tokens: list[str]) -> tuple[str, ...]:
        """Normalize argv tokens for stable instance matching."""
        if not tokens:
            return ()

        normalized = []
        for idx, token in enumerate(tokens):
            t = (token or '').strip()
            if not t:
                continue
            if idx == 0:
                t = os.path.basename(t)
            normalized.append(t.lower())
        return tuple(normalized)

    def _resolve_program_name(self, comm: str, filename: str, cmdline_tokens: list[str]) -> str:
        """Resolve the effective program name for a launch event.

        If comm/filename is a known interpreter (python, bash, ...), the real
        program identity is the first non-flag cmdline argument (the script or
        module).  Otherwise the executable basename itself is the program name.
        """
        base = os.path.basename((filename or comm or '').strip()).lower()
        if not _basename_is_interpreter(base):
            return base

        # Interpreter launch: dig the real script/program out of the cmdline,
        # skipping argv0 (the interpreter) and any leading flags (-u, -m, ...).
        for tok in (cmdline_tokens or [])[1:]:
            t = (tok or '').strip()
            if not t or t.startswith('-'):
                continue
            return os.path.basename(t).lower()
        return base

    def _is_event_instance_match(self, pid: int, app_name: str, filename: str) -> tuple[bool, str]:
        """Decide whether this launch event matches the app's configured match mode.

        aggregate: always true once coarse bpf_name matching is satisfied.
        instance: require full normalized /proc cmdline match; fallback to argv0-only
                  when /proc cmdline is unavailable.
        """
        # App identity is name-based; a launch event is never rejected on the
        # basis of which (ephemeral) cgroup the new process landed in.
        cfg = self._app_match_cfg.get(app_name.lower(), {})
        mode = cfg.get('mode', 'aggregate')
        expected = tuple(cfg.get('match_cmdline', ()))

        if mode != 'instance':
            return True, "aggregate_mode"

        observed_tokens = self._read_proc_cmdline(pid)
        observed = self._normalize_cmdline(observed_tokens)
        if observed:
            if observed == expected:
                return True, "instance_cmdline_exact"
            logger.info(
                "Instance mismatch for app '%s': expected=%s observed=%s pid=%s",
                app_name, list(expected), list(observed), pid,
            )
            return False, "instance_cmdline_mismatch"

        # Fallback strategy: when /proc cmdline is unavailable for short-lived
        # processes, allow only an argv0-level check so we do not fully fail open.
        fallback_argv0 = os.path.basename((filename or '').strip()).lower()
        if expected and fallback_argv0 and expected[0] == fallback_argv0:
            logger.warning(
                "Instance fallback for app '%s': /proc cmdline unavailable, "
                "using argv0-only check (expected argv0=%s, pid=%s)",
                app_name, expected[0], pid,
            )
            return True, "instance_fallback_argv0"

        logger.info(
            "Instance mismatch for app '%s': /proc cmdline unavailable and "
            "argv0 fallback failed (expected=%s, filename=%s, pid=%s)",
            app_name, list(expected), filename, pid,
        )
        return False, "instance_cmdline_unavailable"

    def trace_print(self) -> None:
        self.bpf.trace_print()

    def _resolve_main_process_for_exec_event(self, pid: int, comm: str, filename: str) -> tuple[bool, str]:
        """Resolve app mapping on exec-complete events.

        APP_EXEC events do not carry argv[0] in the BPF payload. Start with the
        regular comm/filename lookup, then fall back to /proc cmdline token
        basenames so interpreter-style launches (python foo.py, bash run.sh)
        can still map to a configured bpf_name.
        """
        is_main_process, app_name = self.get_main_process(comm, filename)
        if is_main_process:
            return True, app_name

        # Interpreter-style launches (python foo.py, bash run.sh) carry the
        # interpreter as comm/filename; resolve the real program name from the
        # cmdline and map that back to a configured bpf_name.
        # This path mirrors app_discovery.extract_fields' bpf_name rule exactly:
        # when comm is an interpreter, identity is the cmdline script basename
        # (stored full, never truncated); when comm is the program, comm-exact
        # above already handled it.  So no truncation here — the cmdline-derived
        # full name matches the full bpf_name that extract stored.
        tokens = self._read_proc_cmdline(pid)
        program = self._resolve_program_name(comm, filename, tokens)
        if program:
            app = self._filename_exe_to_app.get(program) or self._comm_to_app.get(program)
            if app:
                return True, app

        return False, ""

    def _handle_launch_event(self, pid: int, comm: str, filename: str, app_name: str) -> None:
        """Handle a launch after app mapping and instance checks succeed."""
        app_data = self._app_map_index.get(app_name.lower())
        if not app_data:
            logger.warning(
                "Matched app '%s' but no controlled-app DB entry found; skip pid=%s",
                app_name, pid,
            )
            return

        app_id = app_data.get('app_id', '')
        app_priority = app_data.get('priority', 'low')
        logger.debug(f"launch: app_id={app_id}, app_name={app_name}, comm={comm}, filename={filename}")
        self.monitored_app_launched[pid] = (app_id, app_name, comm, filename)

        # Track this PID under its app and decide whether we
        # should emit a "running" notification.  Only the first
        # live PID for an app emits one; subsequent execve
        # events from sibling/child processes (shell wrapper
        # -> daemon -> helper) just join the set silently.
        live_pids = self.app_live_pids.setdefault(app_name, set())
        is_first_live_pid = len(live_pids) == 0
        live_pids.add(pid)

        if app_priority.lower() == "critical":
            app_utils.adjust_oom_priority(app_id, app_name, app_priority, app_data['cmdline'])
            if is_first_live_pid:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, True)
        else:
            # Only intercept (SIGSTOP) when the system is already in
            # critical pressure state.  If the system is idle the app
            # is allowed to start freely, avoiding the collateral
            # "Stopped" visible to the user that the previous
            # always-SIGSTOP pattern caused.
            if self._system_critical.is_set():
                try:
                    os.kill(pid, signal.SIGSTOP)
                except OSError as e:
                    logger.debug(f"SIGSTOP failed for PID {pid}: {e}")
                self.handle_monitored_app(pid, comm, filename, app_name, app_id)
            else:
                # System is not under critical pressure: let the app run.
                logger.debug("System not critical, allowing '%s' (PID: %s) to run freely", app_name, pid)
                if is_first_live_pid:
                    app_utils.callback_manager.send_callback_notification({
                        'app_id': app_id,
                        'app_name': app_name,
                        'status': "running",
                        'purpose': "app"
                    }, True)
        self.mark_process_handled(pid)

    def get_main_process(self, comm: str, filename: str) -> tuple[bool, str]:
        """Check whether this execve event is the main process of a monitored app.

        Uses pre-built lookup tables (_comm_to_app, _filename_exe_to_app) so
        no config access or dict construction occurs on the hot path.
        """
        comm_lower = comm.lower()
        filename_lower = filename.lower()

        # O(1) comm exact match – covers process-title bpf_name entries
        # (e.g. comm="mybench" while filename="python")
        if comm_lower in self._comm_to_app:
            return True, self._comm_to_app[comm_lower]

        # All remaining checks require the executable to live under a known
        # /bin/ path or to be launched via bash, to avoid false positives on
        # interpreter argv[0].
        is_bin_path = any(x in filename_lower for x in ('/bin/', '/usr/bin/', '/snap/bin/'))
        is_bash_launch = (comm_lower == 'bash')
        if not is_bin_path and not is_bash_launch:
            return False, ""

        # Exact filename-path match against bpf_name entries
        # (e.g. /usr/bin/llama-server with bpf_name=["llama-server"])
        filename_base = os.path.basename(filename_lower)
        for exe, app in self._filename_exe_to_app.items():
            if (
                filename_lower == exe or
                filename_base == exe or
                f"/{exe}" in filename_lower or
                filename_lower.endswith(f"/{exe}")
            ):
                return True, app

        return False, ""

    def is_process_alive(self, pid):
        try:
            # Check whether /proc/[pid]/status exists
            with open(f"/proc/{pid}/status") as f:
                return True
        except FileNotFoundError:
            return False


    def handle_exit_event(self, pid, app_id, app_name, old_comm, old_filename):
        """Deferred check to confirm whether the process has truly exited."""
        if self.is_process_alive(pid):
            logger.debug(f"[Delay Check] PID={pid} still alive, not exiting normally.")
            return

        # PID has exited for real; drop handled marker so future PID reuse
        # does not get incorrectly filtered as "already handled".
        self.handled_processes.discard(pid)

        # Per-app PID accounting: only emit "stopped" once the app has no
        # live PIDs left.  Intermediate exec-chain PIDs (shell wrapper that
        # exec'd into a daemon) come through this path and would otherwise
        # flicker "stopped" while the real worker is still running.
        live_pids = self.app_live_pids.get(app_name)
        if live_pids is not None:
            live_pids.discard(pid)
            still_running = bool(live_pids)
            if not still_running:
                self.app_live_pids.pop(app_name, None)
        else:
            still_running = False

        # Reconcile against current process reality. In some launcher/scope
        # setups, the tracked PID set may keep helper PIDs that are no longer
        # part of the app's effective process set, leaving UI stuck at running.
        if still_running:
            try:
                app_data = self._app_map_index.get(app_name.lower()) or {}
                cmdline = app_data.get('cmdline', '')
                real_status = app_utils.check_app_running_status(app_id, app_name, cmdline)
                if real_status != "running":
                    logger.info(
                        "Exit reconcile: app '%s' pid=%s has residual tracked pids=%s but "
                        "check_app_running_status=%s; forcing stopped.",
                        app_name, pid, sorted(live_pids), real_status,
                    )
                    still_running = False
                    self.app_live_pids.pop(app_name, None)
            except Exception as e:
                logger.debug(f"Exit reconcile skipped for app '{app_name}': {e}")

        if still_running:
            logger.debug(
                f"Monitored process exited but app '{app_name}' still has "
                f"{len(live_pids)} live PID(s); skipping 'stopped' notification."
            )
        else:
            logger.debug(f"Monitored process terminated: PID={pid}, app={app_name}")
            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': "stopped",
                'purpose': "app"
            }, store=True)
        del self.monitored_app_launched[pid]

        # Clean up pending_exit_events
        if pid in self.pending_exit_events:
            del self.pending_exit_events[pid]


    def print_event(self, cpu: int, data: Any, size: int) -> None:
        event = self.bpf["events"].event(data)
        filename = event.filename.decode('utf-8', 'ignore')
        comm = event.comm.decode('utf-8', 'ignore')
        pid = event.pid
        type = event.type

        if type == 0:  # launch-enter event
            comm_lower = comm.lower()
            filename_lower = filename.lower()
            # Fast pre-filter: skip the vast majority of unrelated BPF exec events
            # without entering get_main_process at all.
            if not (comm_lower in self._comm_to_app or
                    any(c in filename_lower or c in comm_lower
                        for c in self._quick_filter)):
                return

            # logger.debug(
            #     "*** Event: PID=%s, type=%s COMM=%s, FILENAME=%s, phase=enter_exec ***",
            #     pid, type, comm, filename,
            # )

        elif type == 1:  # exec-complete event (launch decisions happen here)
            cmdline_tokens = self._read_proc_cmdline(pid)
            cmdline_pretty = " ".join(cmdline_tokens) if cmdline_tokens else "<unavailable>"
            #logger.debug(
            #    "*** Post-Exec Event: PID=%s, type=%s COMM=%s, FILENAME=%s, CMDLINE=%s ***",
            #    pid, type, comm, filename, cmdline_pretty,
            #)

            # Prevent processing the same process tree more than once
            if self.is_process_handled(pid):
                return

            is_main_process, app_name = self._resolve_main_process_for_exec_event(pid, comm, filename)
            if not is_main_process:
                return

            logger.debug(f"Is this filename main process? {is_main_process}, app_name={app_name}")

            matched, reason = self._is_event_instance_match(pid, app_name, filename)
            if not matched:
                logger.debug(
                    "Skip launch event: app='%s' pid=%s comm=%s filename=%s reason=%s",
                    app_name, pid, comm, filename, reason,
                )
                return

            self._handle_launch_event(pid, comm, filename, app_name)

        elif type == 2:  # exit event
            if pid not in self.monitored_app_launched:
                return

            # Cancel any existing pending exit timer for this PID
            if pid in self.pending_exit_events:
                self.pending_exit_events[pid].cancel()

            app_id, app_name, old_comm, old_filename = self.monitored_app_launched[pid]
            # logger.debug(f"Detected possible exit: PID={pid}, comm={comm}")

            # Schedule a deferred check 1.5 s later to confirm the process has exited
            timer = Timer(1.5, self.handle_exit_event, args=[pid, app_id, app_name, old_comm, old_filename])
            self.pending_exit_events[pid] = timer
            timer.start()


    def is_process_handled(self, pid: int) -> bool:
        """Return True if this process (or a parent) has already been handled."""
        # Check the process and its ancestors
        try:
            process = psutil.Process(pid)
            for p in [process] + process.parents():
                if p.pid in self.handled_processes:
                    return True
        except psutil.NoSuchProcess:
            pass
        return False

    def mark_process_handled(self, pid: int) -> None:
        """Mark a process as handled."""
        self.handled_processes.add(pid)

    def handle_monitored_app(self, pid: int, comm: str, filename: str, app_name: str, app_id: str) -> None:
        """Handle a low-priority app that was launched while the system is under critical pressure.

        This method is only called from print_event when _system_critical is set,
        meaning SIGSTOP has already been issued.  It re-checks the event in case
        the system recovered between the SIGSTOP and this point; if so, it issues
        SIGCONT and lets the app run.  Otherwise it queues the app for deferred
        resumption.
        """
        logger.debug(f"Detected monitored app '{app_name}' (PID: {pid}, COMM: {comm}, FILE: {filename}, app_id: {app_id})")

        try:
            # Re-check the critical flag: the monitor may have transitioned out
            # of critical between the SIGSTOP in print_event and here.
            if not self._system_critical.is_set():
                logger.debug(f"System recovered before handling {app_name} (PID: {pid}), resuming")
                os.kill(pid, signal.SIGCONT)
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, True)
            else:
                logger.info(f"System resources busy, skipping relaunch of {app_name}")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "pending",
                    'purpose': "app"
                }, True)
                app_utils.update_app_status(app_id, "pending")
                self.app_pending_queue.put(
                    {"pid": pid, "comm": comm, "filename": filename, "app_name": app_name, "app_id": app_id})

        except Exception as e:
            logger.debug(f"Error handling {app_name} (PID: {pid}): {str(e)}")

    def add_to_monitorlist(self, app_names: Union[str, List[str]]) -> None:
        """Add one or more applications to the monitor list (supports batch operations)."""
        if not app_names:
            return

        # Normalise to a list
        names = [app_names] if isinstance(app_names, str) else app_names

        # Lowercase for comparison
        existing_lower = {name.lower() for name in self.monitored_apps}

        added_count = 0
        for name in names:
            if not name or not name.strip():
                logger.debug(f"Skipping empty app name in monitor list")
                continue
            if name.lower() not in existing_lower:
                self.monitored_apps.add(name)
                existing_lower.add(name.lower())
                added_count += 1
                logger.debug(f"Added '{name}' to monitoring list")

        if added_count == 0 and names:
            app_str = ', '.join(f"'{name}'" for name in names)
            logger.debug(f"All {len(names)} app(s) [{app_str}] already in monitoring list")
        elif added_count > 0:
            logger.debug(f"Successfully added {added_count}/{len(names)} new app(s)")
            self._rebuild_match_cache()

    def remove_from_monitorlist(self, app_name: str) -> None:
        """Remove one or more applications from the monitor list."""
        if app_name in self.monitored_apps:
            self.monitored_apps.remove(app_name)
            logger.debug(f"Removed '{app_name}' from monitoring list")
            self._rebuild_match_cache()
        else:
            logger.debug(f"'{app_name}' not found in monitoring list")

    def clear_monitorlist(self) -> None:
        """Clear the entire monitor list."""
        self.monitored_apps.clear()
        logger.debug("Cleared monitoring list")
        self._rebuild_match_cache()

    def get_monitored_apps(self) -> List[str]:
        """Return the current list of monitored applications."""
        return list(self.monitored_apps)

    def register_running_pids(self, app_id: str, app_name: str,
                              cmdline: str = "") -> int:
        """Adopt currently-running PIDs of an app into the BPF tracking state.

        The BPF tracker emits "stopped" only after every PID it has seen
        launch has exited (see :py:meth:`handle_exit_event`).  When an app
        is already running at the moment the user adds it to control —
        either via the dropdown form or the wizard — BPF never observed
        the launch, so its exits would be ignored and the app's status in
        the UI would stay "running" forever.

        This method discovers the PIDs via ``process_names`` (preferred)
        or the commandline-derived executable name and seeds them into
        ``app_live_pids`` / ``monitored_app_launched`` so that subsequent
        BPF exit events fire normally.

        Returns the number of PIDs adopted (0 if none found).
        """
        # Prefer the configured process_names list so multi-process apps
        # (e.g. a wrapper script + its daemon) get every PID adopted.
        pids: set[int] = set()
        process_names = app_utils._get_app_process_names(app_id=app_id, app_name=app_name)
        if process_names:
            for pname in process_names:
                try:
                    pids.update(app_utils.get_app_processes_for_app(pname, app_id=app_id, app_name=app_name))
                except Exception:
                    continue
        elif cmdline:
            try:
                exe = app_utils._get_executable_name(app_name, cmdline)
                if exe:
                    pids.update(app_utils.get_app_processes_for_app(exe, app_id=app_id, app_name=app_name))
            except Exception:
                pass

        if not pids:
            return 0

        live_pids = self.app_live_pids.setdefault(app_name, set())
        for pid in pids:
            if pid in self.monitored_app_launched:
                continue
            self.monitored_app_launched[pid] = (app_id, app_name, "", "")
            self.mark_process_handled(pid)
            live_pids.add(pid)

        logger.info(
            f"register_running_pids: adopted {len(pids)} PID(s) for app "
            f"'{app_name}' (id={app_id}); live={sorted(live_pids)}"
        )
        return len(pids)

    def scan_already_running_apps(self) -> list:
        """Scan currently running processes for monitored apps that pre-date the balancer.

        Called once when the UI balancer tab is first opened to detect apps that
        started before the balancer service (and were therefore missed by BPF).
        Any matching process is registered in monitored_app_launched and a
        "running" callback is sent so the UI and database reflect the correct state.
        After this one-time scan, ongoing detection is left entirely to BPF.

        Two scanning strategies are used:

        1. **BPF comm/exe matching** (existing logic) – iterates live processes and
           checks whether comm or exe matches a known monitored app via
           :meth:`get_main_process`.  This covers normal single-process desktop apps.

        2. **Multi-process apps** – for apps whose ``controlled_apps`` config entry
           contains a non-empty ``process_names`` list, the BPF comm-matching path
           may miss them (they are recognised by process name, not by bpf_name).
           A second pass calls :func:`app_utils.check_app_running_status` for each
           such app that was NOT already detected in pass 1, and emits the
           appropriate "running" or "stopped" callback.

        :return: list of dicts with keys app_id, app_name, pid for each detected app.
        """
        detected = []
        detected_app_ids: set[str] = set()

        # --- Pass 1: BPF comm/exe matching (original logic) ---
        if self.monitored_apps:
            try:
                for proc in psutil.process_iter(['pid', 'name', 'exe']):
                    try:
                        pid = proc.info['pid']
                        # Fast-path: skip PIDs already tracked by BPF or a prior scan
                        if pid in self.monitored_app_launched or pid in self.handled_processes:
                            continue
                        # Full parent-chain check (handles child processes of known apps)
                        if self.is_process_handled(pid):
                            continue

                        comm = proc.info.get('name') or ''
                        exe = proc.info.get('exe') or ''

                        is_match, app_name = self.get_main_process(comm, exe)
                        if not is_match:
                            continue

                        registered_app = self._app_map_index.get(app_name.lower())
                        if not registered_app:
                            continue

                        app_id = registered_app['app_id']
                        logger.info(
                            f"[startup scan] Detected pre-existing process: PID={pid}, "
                            f"app={app_name}, comm={comm}, exe={exe}"
                        )
                        self.monitored_app_launched[pid] = (app_id, app_name, comm, exe)
                        self.mark_process_handled(pid)

                        # Seed app_live_pids so the eventual exit BPF event
                        # for this PID reaches handle_exit_event with a known
                        # state and emits "stopped" correctly.  Otherwise
                        # apps that were already running at balancer startup
                        # would never report stopped when terminated.
                        live_pids = self.app_live_pids.setdefault(app_name, set())
                        is_first_live_pid = len(live_pids) == 0
                        live_pids.add(pid)

                        if is_first_live_pid:
                            startup_status = self._prefer_persisted_limited_status(app_id, "running")
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_id,
                                'app_name': app_name,
                                'status': startup_status,
                                'purpose': "app"
                            }, True)
                        detected.append({"app_id": app_id, "app_name": app_name, "pid": pid})
                        detected_app_ids.add(app_id)

                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logger.error(f"scan_already_running_apps (pass 1) failed: {e}")

        # --- Pass 2: multi-process apps (process_names) ---
        # Check apps that have process_names configured and were not found in pass 1.
        try:
            all_controlled = app_utils.get_controlled_apps() or []
            for app in all_controlled:
                app_id = app.get('app_id', '')
                app_name = app.get('app_name', '')
                cmdline = app.get('cmdline', '')
                if not app_id or app_id in detected_app_ids:
                    continue
                process_names = app_utils._get_app_process_names(app_id=app_id, app_name=app_name)
                if not process_names:
                    continue  # handled by pass 1 (or not monitored at all)

                status = app_utils.check_app_running_status(app_id, app_name, cmdline)
                if status == "running":
                    status = self._prefer_persisted_limited_status(app_id, status)
                logger.info(
                    f"[startup scan] Multi-process app '{app_name}' "
                    f"(process_names={process_names}): status={status}"
                )
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': status,
                    'purpose': "app"
                }, True)
                if status in {"running", "limited", "a_limited"}:
                    detected.append({"app_id": app_id, "app_name": app_name, "pid": None})
        except Exception as e:
            logger.error(f"scan_already_running_apps (pass 2) failed: {e}")

        logger.info(f"[startup scan] Detected {len(detected)} pre-existing monitored app(s): "
                    f"{[d['app_name'] for d in detected]}")
        return detected

    def check_system_resources(self, cpu_threshold: int = 70, mem_threshold: int = 80) -> bool:
        """Check current system resource usage."""
        try:
            # Get CPU utilisation
            cpu_percent = psutil.cpu_percent(interval=1)

            # Get memory utilisation
            mem_percent = psutil.virtual_memory().percent

            logger.debug(f"System status - CPU: {cpu_percent}%, Memory: {mem_percent}%")

            # Check whether usage is below the threshold
            return cpu_percent < cpu_threshold and mem_percent < mem_threshold

        except Exception as e:
            logger.debug(f"Error checking system resources: {str(e)}")
            # Default to allowing startup on error
            return True


if __name__ == "__main__":
    # Initialise BPF
    bpf_monitor = AppIntercept()

    # Add applications to the monitor list
    bpf_monitor.add_to_monitorlist("firefox")
    bpf_monitor.add_to_monitorlist("Calculator")

    # Open the perf buffer
    bpf_monitor.bpf["events"].open_perf_buffer(bpf_monitor.print_event)
    logger.debug(f"Monitoring execve() for: {', '.join(bpf_monitor.get_monitored_apps())}")

    while True:
        try:
            # Handle both trace output and BPF events
            bpf_monitor.bpf.perf_buffer_poll(timeout=100)
        except KeyboardInterrupt:
            logger.debug("\nExiting...")
            break
        except Exception as e:
            logger.debug(f"Error: {e}")
            break
