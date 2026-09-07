# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import re
import shlex
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments
# with shell=False (default). No untrusted shell execution or string
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import time
from collections import defaultdict
from time import sleep

import psutil
from config.config import b_config
from utils.app_utils import (
    derived_process_identity,
    fetch_all_apps,
    get_cgroup_path_by_pid,
    get_pids_in_cgroup,
)
from utils.logger import logger

from monitor import PSIMonitor
from monitor.app_discovery import is_noise_process
from monitor.cgroup import IO_STAT_FIELDS, as_io_rates, io_stat_deltas, snapshot_cgroup_io
from monitor.disk_pressure import DiskIOMonitor, disk_name_for_devno
from utils.self_ident import is_own_process

_GPU_DRM_DRIVERS = frozenset({'i915', 'xe'})
_GPU_SAMPLE_INTERVAL = 0.3  # seconds between the two fdinfo snapshots for GPU utilisation


def _process_control_flags(pids, name, cmdline):
    """Per-app control hints for the dashboard "Operation" menu.

    An app can span several PIDs, so kill/suspend act on the whole set; the
    flags mirror those the /processes endpoint attaches to individual rows:
      - is_self: any PID belongs to SmartTune itself -> never signal it
      - balancer_candidate: worth offering "Add to balancer" (not a shell/self)
      - status: 'stopped' when any PID is SIGSTOP'd, else a representative state
    """
    pid_list = [p for p in pids if isinstance(p, int)]
    is_self = is_own_process(cmdline=cmdline) or any(is_own_process(p) for p in pid_list)
    balancer_candidate = not is_self and not is_noise_process(name)
    status = ''
    for p in pid_list:
        try:
            st = psutil.Process(p).status()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if st == psutil.STATUS_STOPPED:
            status = 'stopped'
            break
        if not status:
            status = st
    return pid_list, is_self, balancer_candidate, status


def _parse_fdinfo_mem_bytes(line, is_xe):
    """Parse a single drm-total-*/drm-memory-* line into bytes, or return 0.

    Handles unit conversion (KiB/MiB/GiB/B) and driver-specific GTT semantics:
    - xe: GTT regions represent real GPU memory, included.
    - i915: GTT regions are virtual address-space reservations, excluded.
    Cycle-counter lines (drm-total-cycles-*) are always excluded.
    """
    if not (line.startswith('drm-total-') or line.startswith('drm-memory-')):
        return 0
    if not is_xe and '-gtt' in line:
        return 0
    if '-cycles-' in line:
        return 0
    parts = line.split(':', 1)
    if len(parts) != 2:
        return 0
    val_parts = parts[1].strip().split()
    if len(val_parts) < 2:
        return 0
    try:
        value = int(val_parts[0])
        unit = val_parts[1].upper()
        if unit in ('KIB', 'KI', 'K', 'KB'):
            value *= 1024
        elif unit in ('MIB', 'MI', 'M', 'MB'):
            value *= 1024 * 1024
        elif unit in ('GIB', 'GI', 'G', 'GB'):
            value *= 1024 * 1024 * 1024
        elif unit != 'B':
            return 0
        return value
    except (ValueError, IndexError):
        return 0


def _parse_fdinfo_engines(content):
    """Parse engine utilization fields from fdinfo content.

    Returns a dict ``{engine_name: {"cycles": int, "total_cycles": int, "time_ns": int}}``.
    Uses the same data model as gpu_monitor.scan_drm_fdinfo_clients:
    - xe driver reports ``drm-cycles-*`` / ``drm-total-cycles-*`` (cycle counts).
    - i915 driver reports ``drm-engine-*`` (nanoseconds).
    Both are stored per engine so callers can compute utilization uniformly.
    """
    engines = {}

    def _ensure(name):
        if name not in engines:
            engines[name] = {"cycles": 0, "total_cycles": 0, "time_ns": 0}

    for line in content.splitlines():
        parts = line.split(':', 1)
        if len(parts) < 2:
            continue
        key, val = parts[0].strip(), parts[1].strip()

        if key.startswith('drm-engine-'):
            eng = key[len('drm-engine-'):]
            _ensure(eng)
            try:
                engines[eng]["time_ns"] = int(val.split()[0])
            except (ValueError, IndexError):
                pass
        elif key.startswith('drm-total-cycles-'):
            eng = key[len('drm-total-cycles-'):]
            _ensure(eng)
            try:
                engines[eng]["total_cycles"] = int(val)
            except ValueError:
                pass
        elif key.startswith('drm-cycles-'):
            eng = key[len('drm-cycles-'):]
            _ensure(eng)
            try:
                engines[eng]["cycles"] = int(val)
            except ValueError:
                pass

    return engines


def _read_pid_fdinfo_gpu(pid, seen_client_ids=None):
    """Read Intel GPU DRM engine times and memory from /proc/<pid>/fdinfo.

    Returns ``{"engines", "mem_bytes", "per_pdev": {pdev: {engines, mem_bytes}}}``
    when the process holds at least one Intel GPU fd, or ``None`` otherwise.
    ``engines``/``mem_bytes`` are merged across devices; ``per_pdev`` keeps them
    split per GPU (PCI address, e.g. igpu vs dgpu).
    """
    fd_dir = f'/proc/{pid}/fd'
    fdinfo_dir = f'/proc/{pid}/fdinfo'

    if not os.path.isdir(fd_dir):
        return None

    try:
        fd_entries = os.listdir(fd_dir)
    except (OSError, PermissionError):
        return None

    all_engines = {}
    total_mem_bytes = 0
    per_pdev = {}
    found = False
    _seen = seen_client_ids if seen_client_ids is not None else set()

    for fd_name in fd_entries:
        try:
            link = os.readlink(os.path.join(fd_dir, fd_name))
            if '/dev/dri/' not in link:
                continue
        except (OSError, PermissionError):
            continue

        fdinfo_path = os.path.join(fdinfo_dir, fd_name)
        try:
            with open(fdinfo_path, 'r') as fh:
                fdinfo_content = fh.read()
        except (OSError, PermissionError):
            continue

        driver = None
        client_id = None
        pdev = None
        for line in fdinfo_content.splitlines():
            if line.startswith('drm-driver:'):
                driver = line.split(':', 1)[1].strip()
            elif line.startswith('drm-client-id:'):
                try:
                    client_id = int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith('drm-pdev:'):
                pdev = line.split(':', 1)[1].strip()

        if driver not in _GPU_DRM_DRIVERS:
            continue

        found = True

        if client_id is not None:
            if client_id in _seen:
                continue
            _seen.add(client_id)

        is_xe = (driver == 'xe')
        # Fall back to the DRM node name (renderD128, ...) when drm-pdev is absent.
        dev_key = pdev or os.path.basename(link)
        dev = per_pdev.setdefault(dev_key, {'engines': {}, 'mem_bytes': 0})

        fd_engines = _parse_fdinfo_engines(fdinfo_content)
        for eng, data in fd_engines.items():
            for target in (all_engines, dev['engines']):
                if eng not in target:
                    target[eng] = {"cycles": 0, "total_cycles": 0, "time_ns": 0}
                target[eng]["cycles"] += data["cycles"]
                target[eng]["total_cycles"] += data["total_cycles"]
                target[eng]["time_ns"] += data["time_ns"]

        for line in fdinfo_content.splitlines():
            mem = _parse_fdinfo_mem_bytes(line, is_xe)
            total_mem_bytes += mem
            dev['mem_bytes'] += mem

    if not found:
        return None

    return {'engines': all_engines, 'mem_bytes': total_mem_bytes, 'per_pdev': per_pdev}


def _accumulate_engine_delta(out, t0_engines, t1_engines):
    """Accumulate per-engine deltas between two snapshots into *out*.

    Each engine entry has {cycles, total_cycles, time_ns}.  Deltas are computed
    per field and added to the running totals in *out*.
    """
    for eng, d1 in t1_engines.items():
        d0 = t0_engines.get(eng, {"cycles": 0, "total_cycles": 0, "time_ns": 0})
        if eng not in out:
            out[eng] = {"cycles": 0, "total_cycles": 0, "time_ns": 0}
        out[eng]["cycles"] += max(0, d1["cycles"] - d0["cycles"])
        out[eng]["total_cycles"] += max(0, d1["total_cycles"] - d0["total_cycles"])
        out[eng]["time_ns"] += max(0, d1["time_ns"] - d0["time_ns"])


def _peak_engine_util(engine_delta, elapsed_ns):
    """Peak engine utilisation % from a delta dict (Xe cycles, i915 time)."""
    util = 0.0
    for d in engine_delta.values():
        if d["total_cycles"] > 0:
            u = (d["cycles"] / d["total_cycles"]) * 100
        elif elapsed_ns > 0 and d["time_ns"] > 0:
            u = (d["time_ns"] / elapsed_ns) * 100
        else:
            u = 0.0
        if u > util:
            util = u
    return round(min(util, 100.0), 1)



def _identity_keys(name: str) -> tuple:
    """The forms a configured name can legitimately appear in as a process name.

    Just the name itself, plus its 15-character prefix when it is longer than that:
    the kernel truncates ``comm`` to 15 characters, so a longer program is only ever
    *seen* in the truncated form.
    """
    n = (name or '').strip().lower()
    if not n:
        return ()
    return (n,) if len(n) <= 15 else (n, n[:15])


class ResourceMonitor:
    def __init__(self):
        """Initialise the resource monitor."""
        self.config = b_config
        self.cpu_cores = os.cpu_count() or 16
        self._disk_io = DiskIOMonitor(self.config)
        self._prev_pid_io = {}  # pid -> (read_bytes, write_bytes, ts) for per-pid IO rates
        # Desktop application metadata
        try:
            self.desktop_apps = {app["app_id"]: app for app in fetch_all_apps()}
            logger.info(f"Loaded {len(self.desktop_apps)} desktop applications")
        except Exception as e:
            logger.warning(f"Could not load desktop apps: {str(e)}")
            self.desktop_apps = {}

        # Multi-process app lookup structures, built from controlled_apps entries
        # that have a non-empty process_names list.
        # _proc_name_to_app  : process_name_lower -> app_id
        # _multiprocess_apps : app_id -> {'name': str, 'process_names_lower': list}
        self._proc_name_to_app: dict[str, str] = {}
        self._multiprocess_apps: dict[str, dict] = {}
        self._load_multiprocess_config()

        # Registered apps indexed by the exact identities a process can present.
        self._desktop_name_to_app: dict[str, str] = {}
        self._desktop_exe_to_app: dict[str, str] = {}
        self._build_desktop_identity_index()

        # The balancer must never rank itself as a limit candidate: it reads /proc for
        # every process on every tick, and psutil's read_count / write_count are syscall
        # counts, not block-device operations, so those reads look like a huge IOPS load
        # at almost no bandwidth. Excluding the whole cgroup rather than just our PID,
        # because a limit is a property of a cgroup: any helper we spawn is inside it and
        # every cap written there reaches us. The limit-path counterpart of
        # :func:`is_own_process`. Resolved once -- our own cgroup does not change.
        self._self_cgroup = ''
        try:
            own = get_cgroup_path_by_pid(os.getpid())
            self._self_cgroup = own if own and own != '/' else ''
            logger.info(f"Self cgroup (never limited): {self._self_cgroup or 'unknown'}")
        except Exception as e:
            logger.warning(f"Could not resolve own cgroup, self-exclusion is off: {e}")

    def _load_multiprocess_config(self) -> None:
        """Populate multi-process app lookup maps from controlled_apps config."""
        apps = getattr(self.config, 'controlled_apps', None) or []
        for app in apps:
            pnames = app.get('process_names') or []
            if not pnames:
                continue
            app_id = app.get('id', '')
            app_name = app.get('name', '')
            pnames_lower = [p.lower() for p in pnames]
            self._multiprocess_apps[app_id] = {
                'name': app_name,
                'process_names_lower': pnames_lower,
            }
            for p in pnames_lower:
                if p:
                    self._proc_name_to_app[p] = app_id

    def _build_desktop_identity_index(self) -> None:
        """Index every registered app by the exact names its processes can present.

        Replaces a substring test -- "does the app's name appear anywhere inside the
        process name" -- under which an app claimed every process whose name merely began
        with or contained it. Two apps whose names share a prefix are then indistinguishable,
        and the traffic of one is reported against the other, so the limit is written to the
        wrong app. (Seen with the disk-IO test workload, whose three binaries are named as
        prefixes of one another; the same holds for any `foo` / `foo-worker` pair.)

        The substring form was covering two differences that are real, and both are handled
        exactly here instead:

        * ``comm`` is truncated to 15 characters by the kernel, so a longer program name is
          only ever observed in that form -- see :func:`_identity_keys`;
        * an app is usually registered under a display name while its processes show the
          executable from ``commandline``. That executable is resolved with the wizard's own
          derivation rule, so an interpreter or shell launch indexes as the script rather
          than as the interpreter -- indexing it as the interpreter would hand the app every
          process on the machine that shares it, which is the same over-match in a new place.

        First writer wins: when two apps claim one identity the earlier registration keeps
        it, instead of the limit silently moving to whichever was configured last.
        """
        for app_id, app in self.desktop_apps.items():
            try:
                tokens = (app.get('cmdline') or '').split()
                exe_token = tokens[0] if tokens else ''
                derived = derived_process_identity(
                    {'name': '', 'exe': exe_token, 'cmdline': tokens}) if exe_token else ''
                for name in (app.get('name') or '', derived, *(app.get('process_names') or [])):
                    for key in _identity_keys(name):
                        self._desktop_name_to_app.setdefault(key, app_id)
                # Only when the executable IS the program: for a wrapper launch the
                # derivation resolved past it, and indexing the wrapper path would match
                # every process started through the same interpreter or shell.
                if exe_token and derived.lower() == os.path.basename(exe_token).lower():
                    self._desktop_exe_to_app.setdefault(exe_token.lower(), app_id)
            except Exception as e:
                logger.warning(f"Could not index registered app {app_id}: {e}")

    def _app_id_for_process(self, info: dict) -> str:
        """Which configured multi-process app a running process belongs to, or ''.

        Every test here is an exact comparison against a configured process name. Asking
        instead whether a configured name appears *anywhere* in the command line turns
        every launcher into a match for the app it launches: `sudo systemd-run --scope
        --unit=lo-io.scope /tmp/fio_lo` was reported as the app "fio_lo" while living in
        the terminal's own scope, so that scope was merged into the app and then received
        the app's io.max.

        argv[0], comm and exe are all consulted because none is reliable alone: comm is
        truncated to 15 characters by the kernel, exe is unreadable for other users'
        processes, and argv[0] can be rewritten by the process itself. The derived
        identity covers the apps the wizard recorded by script name rather than by
        executable name, whose three exec fields all read "python3" or "bash".
        """
        cmdline = info.get('cmdline') or []
        for name in (
            os.path.basename((cmdline[0] or '').strip()).lower() if cmdline else '',
            (info.get('name') or '').strip().lower(),
            os.path.basename((info.get('exe') or '').strip()).lower(),
        ):
            app_id = self._proc_name_to_app.get(name) if name else None
            if app_id:
                return app_id

        # A derived identity that differs from the three fields above is always the
        # basename of one of the arguments, so screening on those keeps the derivation --
        # which re-reads config on every call -- off a loop over every process on the box.
        args = {os.path.basename((t or '').strip()).lower() for t in cmdline[1:]}
        if not (args & self._proc_name_to_app.keys()):
            return ''
        return self._proc_name_to_app.get(
            derived_process_identity(info).strip().lower(), '')

    def _get_top_processes(self, n=1, samples=3, interval=1.0, mode='default'):
        """Return the top resource-consuming applications, aggregated per cgroup.
        :param n: number of top processes to return
        :param samples: number of sampling rounds for the top process
        :param interval: sampling interval in seconds
        :param mode: scoring mode — 'default' ranks by combined CPU+memory score; 'io' ranks by IO throughput

        In 'io' mode the reported I/O comes from cgroup v2 ``io.stat`` (bytes AND request
        counts), not from per-PID ``io_counters()``. That is not an optimisation: psutil's
        ``read_count``/``write_count`` are syscall counts, so the IOPS this method used to
        report was ~8x low on large buffered writes and worse on async O_DIRECT. See
        ``monitor/cgroup.py`` for the measurements. Default mode is unchanged.
        """
        # Step 1: Sample candidate processes (weighted, per-process-group)
        num_candidates = max(n * 3, 9)  # ensures enough candidates to cover at least n distinct cgroups
        if mode == 'io':
            # Rank candidates by actual disk IO, not CPU+memory: a low-CPU, high-IO process
            # (a large sequential writer, a database flushing) never enters a CPU-weighted
            # candidate set, which would make the whole disk-IO throttle path silently
            # no-op for exactly the apps it exists to control. The PSI-derived CPU/memory
            # weights below are irrelevant here, so they are not computed on this path.
            candidate_procs = self._get_disk_io_candidate_processes(num=num_candidates)
        else:
            psi_data = PSIMonitor().get_current_pressure()
            dynamic_weights = self._adjust_weights_by_pressure(psi_data)
            candidate_procs = self._get_candidate_processes(
                num=num_candidates,
                samples=samples,
                interval=interval,
                dynamic_weights=dynamic_weights
            )

        # logger.debug(f"Candidate processes for cgroup aggregation: {candidate_procs}")
        # Step 2: Collect unique cgroup paths
        # Exclude '/' (root cgroup): its pids span the entire system process tree, which
        # would create a spurious "super group" with an artificially high aggregate score.
        # Our own cgroup is dropped here rather than in each candidate collector: this is
        # the one place every mode passes through, and it is the set that decides which
        # cgroups can receive a limit (see _self_cgroup).
        cgroup_paths = set()
        for proc in candidate_procs:
            cgroup_path = get_cgroup_path_by_pid(proc['pid'])
            if cgroup_path and cgroup_path != '/' and cgroup_path != self._self_cgroup:
                cgroup_paths.add(cgroup_path)

        # Step 2b: For apps with explicit process_names, scan ALL their running
        # processes and add their cgroups so they are always included in the
        # aggregation pass (even when they are not in the top-N candidates).
        # Build a reverse map: cgroup_path -> app_id for later merging.
        multiapp_cgroup_to_app: dict[str, str] = {}
        if self._multiprocess_apps:
            try:
                for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
                    try:
                        app_id = self._app_id_for_process(proc.info)
                        if not app_id:
                            continue
                        cg = get_cgroup_path_by_pid(proc.info['pid'])
                        if cg and cg != '/' and cg != self._self_cgroup:
                            cgroup_paths.add(cg)
                            multiapp_cgroup_to_app[cg] = app_id
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logger.warning(f"Multi-process app scan failed: {e}")

        # Step 3: Aggregate processes per cgroup
        cgroup_data = defaultdict(lambda: {
            'cpu_total': 0,  # sum of CPU usage (%) for all processes
            'mem_percent_total': 0,  # sum of memory usage (%) for all processes
            'mem_rss_total': 0,  # sum of RSS memory (bytes) for all processes
            'io_read_total': 0,    # cumulative IO read bytes delta for all processes
            'io_write_total': 0,   # cumulative IO write bytes delta for all processes
            'io_read_count_total': 0,   # cumulative IO read count delta (for IOPS)
            'io_write_count_total': 0,  # cumulative IO write count delta (for IOPS)
            # Per-device I/O counts for this window, {"maj:min": {rbytes, wbytes, rios, wios}}.
            # Populated from cgroup io.stat in 'io' mode only; empty in default mode.
            'io_per_device': {},
            'count': 0,
            'pids': set(),
            'names': set(),
            'cmdlines': set(),
            # Dominant process: the single process contributing the most to the mode's metric.
            # In default mode this is the process with the highest CPU%; in io mode it is the
            # process with the highest IO delta.  We track this so the UI shows "stress" instead
            # of an unrelated process like "vte-2.91" that happens to share the same cgroup.
            'dominant_pid': None,
            'dominant_name': '',
            'dominant_cmdline': '',
            'dominant_metric': 0.0,  # highest individual contribution seen so far
        })

        # Cache the PID list and Process objects for each cgroup
        cgroup_pids = {}
        pid_process_map = {}
        # IO rate is computed as a delta between two snapshots taken io_sample_interval apart.
        # Using cumulative io_counters directly would give total-lifetime-bytes / elapsed which
        # produces huge, incorrect values (e.g. hundreds of MB/s for an idle Firefox).
        io_sample_interval = 0.5  # seconds between the two IO counter snapshots
        # Each entry: (read_bytes, write_bytes, read_count, write_count) at t0
        pid_io_start: dict[int, tuple[int, int, int, int]] = {}

        # First pass: initialise CPU timers and record initial IO counters (t0)
        for cgroup_path in cgroup_paths:
            pids_in_cgroup = get_pids_in_cgroup(cgroup_path)
            cgroup_pids[cgroup_path] = pids_in_cgroup
            for pid in pids_in_cgroup:
                try:
                    p = psutil.Process(pid)
                    if mode == 'default':
                        p.cpu_percent(interval=None)  # initialise CPU timer (only needed in default mode)
                    try:
                        io = p.io_counters()
                        pid_io_start[pid] = (io.read_bytes, io.write_bytes,
                                             io.read_count, io.write_count)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                        pid_io_start[pid] = (0, 0, 0, 0)
                    pid_process_map[pid] = p
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        # In 'io' mode the reported I/O comes from cgroup v2 io.stat, not from the
        # per-PID counters read below. The per-PID pass still runs -- it supplies the
        # names, memory and the dominant process -- but its read_count/write_count are
        # syscall counts (syscr/syscw), not device requests, and under-report real IOPS
        # by ~8x on large buffered writes and worse on async O_DIRECT. Bytes agree
        # between the two sources; only the operation counts diverge. See
        # monitor/cgroup.py and balancer/test/probe_cgroup_io.py.
        io_stat_t0 = snapshot_cgroup_io(cgroup_pids.keys()) if mode == 'io' else None

        # Sleep covers both CPU and IO measurement intervals
        t0 = time.time()
        time.sleep(io_sample_interval)
        elapsed = time.time() - t0

        # Close the io.stat window right after the sleep, before the (slower) per-PID
        # pass below. Reading it afterwards would stretch the window by however long
        # that pass takes while still dividing by `elapsed`, understating every rate.
        io_stat_elapsed, io_stat_counts = 0.0, {}
        if mode == 'io':
            io_stat_elapsed, io_stat_counts = io_stat_deltas(
                *io_stat_t0, *snapshot_cgroup_io(cgroup_pids.keys()))

        # Second pass: read final counters and compute deltas
        for cgroup_path, pids_in_cgroup in cgroup_pids.items():
            for pid in pids_in_cgroup:
                if pid not in pid_process_map:
                    continue
                p = pid_process_map[pid]
                try:
                    with p.oneshot():
                        cpu_percent = p.cpu_percent(interval=None) if mode == 'default' else 0
                        mem_percent = p.memory_percent() if mode == 'default' else 0
                        mem_info = p.memory_info()
                        try:
                            io_end = p.io_counters()
                            io_start = pid_io_start.get(pid, (0, 0, 0, 0))
                            io_read_delta = max(0, io_end.read_bytes - io_start[0])
                            io_write_delta = max(0, io_end.write_bytes - io_start[1])
                            io_read_count_delta = max(0, io_end.read_count - io_start[2])
                            io_write_count_delta = max(0, io_end.write_count - io_start[3])
                        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                            io_read_delta = io_write_delta = 0
                            io_read_count_delta = io_write_count_delta = 0
                        try:
                            proc_name = p.name()
                            proc_cmdline = ' '.join(p.cmdline()) or proc_name
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            proc_name = ''
                            proc_cmdline = ''

                    cgroup_data[cgroup_path]['cpu_total'] += cpu_percent
                    cgroup_data[cgroup_path]['mem_percent_total'] += mem_percent
                    cgroup_data[cgroup_path]['mem_rss_total'] += mem_info.rss
                    cgroup_data[cgroup_path]['io_read_total'] += io_read_delta
                    cgroup_data[cgroup_path]['io_write_total'] += io_write_delta
                    cgroup_data[cgroup_path]['io_read_count_total'] += io_read_count_delta
                    cgroup_data[cgroup_path]['io_write_count_total'] += io_write_count_delta
                    cgroup_data[cgroup_path]['count'] += 1
                    cgroup_data[cgroup_path]['pids'].add(pid)
                    if proc_name:
                        cgroup_data[cgroup_path]['names'].add(proc_name)
                        cgroup_data[cgroup_path]['cmdlines'].add(proc_cmdline)

                    # Track the single process contributing most to this cgroup's metric so
                    # the UI shows a meaningful name (e.g. "stress") rather than whichever
                    # process name Python's set iteration happens to return first (e.g. "vte").
                    metric = cpu_percent if mode == 'default' else (io_read_delta + io_write_delta)
                    if metric > cgroup_data[cgroup_path]['dominant_metric'] and proc_name:
                        cgroup_data[cgroup_path]['dominant_metric'] = metric
                        cgroup_data[cgroup_path]['dominant_pid'] = pid
                        cgroup_data[cgroup_path]['dominant_name'] = proc_name
                        cgroup_data[cgroup_path]['dominant_cmdline'] = proc_cmdline
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        # Step 3a: In 'io' mode, replace the per-PID I/O totals with the cgroup io.stat
        # counts for the same window. Done here -- after the per-PID pass, before the
        # multi-process merge -- so the merge and the scoring below need no special case:
        # they keep summing the same four fields, which now hold device-side numbers.
        # A cgroup absent from io.stat did no I/O this window and is zeroed rather than
        # left holding syscall-derived values.
        if mode == 'io':
            for cgroup_path, data in cgroup_data.items():
                counts = io_stat_counts.get(cgroup_path) or {}
                data['io_read_total'] = counts.get('rbytes', 0)
                data['io_write_total'] = counts.get('wbytes', 0)
                data['io_read_count_total'] = counts.get('rios', 0)
                data['io_write_count_total'] = counts.get('wios', 0)
                data['io_per_device'] = counts.get('per_device', {})

        # Step 3b: Merge cgroup_data entries that belong to the same multi-process app.
        # Entries whose cgroup_path appears in multiapp_cgroup_to_app are grouped by
        # app_id; all but the first (primary) cgroup are folded in and deleted.
        if multiapp_cgroup_to_app:
            # Group cgroups by app_id
            app_cgroup_groups: dict[str, list] = {}
            for cg, app_id in multiapp_cgroup_to_app.items():
                app_cgroup_groups.setdefault(app_id, []).append(cg)

            for app_id, cg_list in app_cgroup_groups.items():
                if len(cg_list) <= 1:
                    continue  # only one cgroup, nothing to merge
                # Use the lexicographically-first cgroup as the stable primary key
                primary = min(cg_list)
                # Capture per-cgroup breakdown BEFORE merging so the balancer can
                # distribute limits proportionally (keyed by basename for easy lookup).
                per_cg_mem_rss = {
                    os.path.basename(cg): cgroup_data[cg]['mem_rss_total']
                    for cg in cg_list if cg in cgroup_data
                }
                per_cg_cpu = {
                    os.path.basename(cg): cgroup_data[cg]['cpu_total']
                    for cg in cg_list if cg in cgroup_data
                }
                for other in cg_list:
                    if other == primary or other not in cgroup_data:
                        continue
                    d = cgroup_data[other]
                    cgroup_data[primary]['cpu_total'] += d['cpu_total']
                    cgroup_data[primary]['mem_percent_total'] += d['mem_percent_total']
                    cgroup_data[primary]['mem_rss_total'] += d['mem_rss_total']
                    cgroup_data[primary]['io_read_total'] += d['io_read_total']
                    cgroup_data[primary]['io_write_total'] += d['io_write_total']
                    cgroup_data[primary]['io_read_count_total'] += d['io_read_count_total']
                    cgroup_data[primary]['io_write_count_total'] += d['io_write_count_total']
                    # Per-device counts merge per device, not by concatenation: a
                    # multi-cgroup app usually hits the SAME disk from every cgroup, and
                    # the limit path needs one number per disk to write one io.max line.
                    merged_dev = cgroup_data[primary]['io_per_device']
                    for dev, dev_counts in d['io_per_device'].items():
                        target = merged_dev.setdefault(dev, {k: 0 for k in IO_STAT_FIELDS})
                        for field in IO_STAT_FIELDS:
                            target[field] += dev_counts.get(field, 0)
                    cgroup_data[primary]['count'] += d['count']
                    cgroup_data[primary]['pids'] |= d['pids']
                    cgroup_data[primary]['names'] |= d['names']
                    cgroup_data[primary]['cmdlines'] |= d['cmdlines']
                    # Update dominant process: use the entry with the strictly higher metric.
                    # When metrics are equal we keep the primary cgroup's values, which is
                    # already deterministic because primary was chosen as min(cg_list).
                    if d['dominant_metric'] > cgroup_data[primary]['dominant_metric']:
                        cgroup_data[primary]['dominant_metric'] = d['dominant_metric']
                        cgroup_data[primary]['dominant_pid'] = d['dominant_pid']
                        cgroup_data[primary]['dominant_name'] = d['dominant_name']
                        cgroup_data[primary]['dominant_cmdline'] = d['dominant_cmdline']
                    # Attach extra cgroup paths so callers can apply limits to all of them
                    cgroup_data[primary].setdefault('extra_cgroups', []).append(other)
                    del cgroup_data[other]
                # Store the per-cgroup breakdown for proportional limit distribution
                cgroup_data[primary]['per_cgroup_mem_rss'] = per_cg_mem_rss
                cgroup_data[primary]['per_cgroup_cpu'] = per_cg_cpu

        # Step 4: Compute scores based on the selected mode
        processes = []
        for cgroup_path, data in cgroup_data.items():
            if data['count'] > 0:
                # 'io' mode divides by the io.stat window, which closed right after the
                # sleep; default mode divides by the per-PID window. Using one `elapsed`
                # for both would misdate whichever source it did not come from.
                io_window = io_stat_elapsed if mode == 'io' else elapsed
                if io_window <= 0:
                    io_window = elapsed or io_sample_interval
                # IO rates: delta bytes / elapsed seconds → MB/s
                io_read_rate_mb = data['io_read_total'] / io_window / (1024 ** 2)
                io_write_rate_mb = data['io_write_total'] / io_window / (1024 ** 2)
                # IOPS. In 'io' mode these are rios/wios -- requests the device actually
                # saw. In default mode they remain psutil's syscall counts, which is fine
                # there because nothing ranks or caps on them.
                io_read_iops = data['io_read_count_total'] / io_window
                io_write_iops = data['io_write_count_total'] / io_window
                if mode == 'io':
                    # IO mode: rank by total read+write throughput and IOPS
                    # IOPS is scaled down (divided by 1000) to balance with MB/s
                    # Example: 100 MB/s + 5000 IOPS = 100 + 5 = 105
                    score = (io_read_rate_mb + io_write_rate_mb +
                             (io_read_iops + io_write_iops) / 1000)
                else:
                    # Default mode: combined CPU + memory score
                    cpu_total_normalized = data['cpu_total'] / self.cpu_cores
                    score = (
                            dynamic_weights['cpu'] * min(cpu_total_normalized, 100) +
                            dynamic_weights['memory'] * min(data['mem_percent_total'], 100)
                    )

                # dominant_name is the process with the highest individual contribution;
                # fall back to the alphabetically-first name from the set if no dominant
                # was recorded, to ensure consistent output across calls.
                dominant_name = data['dominant_name'] or min(data['names'], default='unknown')
                dominant_cmdline = data['dominant_cmdline'] or min(data['cmdlines'], default='')

                processes.append({
                    'pids': list(data['pids']),
                    'cgroup': cgroup_path,
                    # extra_cgroups is set only for merged multi-process app entries
                    'extra_cgroups': data.get('extra_cgroups', []),
                    # per-cgroup breakdown (basename -> raw value) for proportional limiting;
                    # empty dicts for single-cgroup apps.
                    'per_cgroup_mem_rss': data.get('per_cgroup_mem_rss', {}),
                    'per_cgroup_cpu': data.get('per_cgroup_cpu', {}),
                    'score': round(score, 2),
                    'cpu_avg': round(data['cpu_total'] / self.cpu_cores, 1) if mode == 'default' else 0,
                    'mem_avg': round(data['mem_percent_total'], 1) if mode == 'default' else 0,
                    'mem_rss': round(data['mem_rss_total'] / (1024 ** 3), 2),
                    'io_read_rate': round(io_read_rate_mb, 4),
                    'io_write_rate': round(io_write_rate_mb, 4),
                    'io_read_iops': round(io_read_iops, 1),
                    'io_write_iops': round(io_write_iops, 1),
                    # Per-disk breakdown of this app's I/O, keyed by kernel device name
                    # ("nvme0n1"), each {read_mb_s, write_mb_s, read_iops, write_iops}.
                    # Only populated in 'io' mode. This is what lets a cap target the one
                    # disk the app is hammering instead of every disk on the box, and what
                    # lets the throttle decision compare an app against the device class
                    # it is actually loading.
                    'io_per_disk': self._per_disk_rates(data['io_per_device'], io_window),
                    'names': list(data['names']),
                    'cmdlines': list(data['cmdlines']),
                    'dominant_pid': data['dominant_pid'],
                    'dominant_name': dominant_name,
                    'dominant_cmdline': dominant_cmdline,
                })

        # logger.debug(f"Aggregated processes by cgroup: {processes}")
        # Step 5: Return the highest-scored process information
        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

    @staticmethod
    def _per_disk_rates(per_device, elapsed):
        """Convert ``{"maj:min": counts}`` from io.stat into ``{disk_name: rates}``.

        Several devnos can resolve to the same disk name (a partition and its parent),
        so counts are accumulated per name before the division rather than overwritten.
        """
        if not per_device or elapsed <= 0:
            return {}
        by_disk = {}
        for devno, counts in per_device.items():
            disk = disk_name_for_devno(devno)
            target = by_disk.setdefault(disk, {k: 0 for k in IO_STAT_FIELDS})
            for field in IO_STAT_FIELDS:
                target[field] += counts.get(field, 0)
        return {
            disk: {k: round(v, 4) for k, v in as_io_rates(counts, elapsed).items()}
            for disk, counts in by_disk.items()
        }

    def _get_candidate_processes(self, num, samples, interval, dynamic_weights):
        """Sample candidate processes and return their PIDs and names (scores used for filtering only).

        Applies per-cgroup diversity limiting: after scoring each process in a sampling round,
        caps the number of candidates contributed by the same cgroup (max_per_cgroup) to
        prevent high-concurrency apps (e.g. stress -c 8) from filling all candidate slots with
        workers from the same cgroup, ensuring the candidate set covers enough distinct cgroups.
        """
        candidates = []
        seen_pids = set()  # track already-processed PIDs
        max_per_cgroup = 2  # max candidates per cgroup

        for _ in range(samples):
            current_sample = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent',
                                             'create_time']):
                try:
                    info = proc.info
                    pid = info['pid']

                    # Skip already-processed PIDs or blacklisted processes
                    name_lower = (info.get('name') or '').lower()
                    if (pid in seen_pids or
                            any(b in name_lower for b in (self.config.blacklist or ())) or
                            time.time() - info['create_time'] < 2):
                        continue

                    seen_pids.add(pid)  # mark as processed

                    # Compute weighted real-time score (used for ranking only; not returned)
                    score = (
                            dynamic_weights['cpu'] * min(info['cpu_percent'], 100) +
                            dynamic_weights['memory'] * min(info['memory_percent'], 100)
                    )

                    current_sample.append({
                        'pid': pid,
                        'name': info['name'],
                        'score': score  # used for ranking only
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Select top-num candidates with per-cgroup diversity cap.
            # Sort by score desc then greedily pick processes, capping each cgroup at
            # max_per_cgroup slots.  This prevents one heavy multi-process app (e.g.
            # `stress -c 8`) from monopolising all candidate slots with workers that all
            # share the same cgroup, which would leave _get_top_processes with only one
            # unique cgroup and return just a single result instead of the requested n.
            current_sample_sorted = sorted(current_sample, key=lambda x: -x['score'])
            selected: list = []
            cgroup_count: dict = {}
            for proc in current_sample_sorted:
                cgroup = get_cgroup_path_by_pid(proc['pid']) or str(proc['pid'])
                cnt = cgroup_count.get(cgroup, 0)
                if cnt < max_per_cgroup:
                    selected.append(proc)
                    cgroup_count[cgroup] = cnt + 1
                    if len(selected) >= num:
                        break
            candidates.extend(selected)
            if _ != samples - 1:
                time.sleep(interval)

        return [{'pid': p['pid'], 'name': p['name']} for p in candidates]

    def _get_disk_io_candidate_processes(self, num, interval=0.5):
        """Return the ``num`` processes doing the most disk IO, as [{'pid', 'name'}].

        Deliberately separate from :meth:`_get_candidate_processes` (which ranks by a
        CPU+memory weighted score) rather than a mode flag on it: the two select on
        unrelated signals and share no useful logic beyond the blacklist and the
        per-cgroup diversity cap.

        Ranking is on the read+write byte delta from ``/proc/<pid>/io`` across a single
        ``interval`` window — the same quantity the caller re-measures precisely for the
        surviving cgroups, so the candidate set can never exclude a genuine top consumer.
        A process that only appears in the second snapshot has no baseline and is skipped
        (it would otherwise show its whole lifetime's IO as one interval's worth).

        :param num: number of candidates to return.
        :param interval: seconds between the two ``/proc/<pid>/io`` snapshots.
        """
        blacklist = tuple(self.config.blacklist or ())

        def _snapshot():
            """{pid: (read_bytes + write_bytes, name)} for all readable, non-blacklisted procs."""
            snap = {}
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    name = proc.info.get('name') or ''
                    if any(b in name.lower() for b in blacklist):
                        continue
                    io = proc.io_counters()
                    snap[proc.info['pid']] = (io.read_bytes + io.write_bytes, name)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
                        AttributeError, OSError):
                    continue
            return snap

        t0 = _snapshot()
        time.sleep(interval)
        t1 = _snapshot()

        scored = []
        for pid, (total_end, name) in t1.items():
            start = t0.get(pid)
            if start is None:
                continue  # no baseline this window
            delta = total_end - start[0]
            if delta > 0:
                scored.append({'pid': pid, 'name': name, 'io_delta': delta})

        scored.sort(key=lambda p: -p['io_delta'])

        # Same per-cgroup diversity cap as the default path: without it a high-concurrency
        # writer (fio --numjobs=8) fills every slot with its own workers and the caller is
        # left with a single distinct cgroup instead of the n it asked for.
        max_per_cgroup = 2
        selected = []
        cgroup_count: dict = {}
        for proc in scored:
            cgroup = get_cgroup_path_by_pid(proc['pid']) or str(proc['pid'])
            cnt = cgroup_count.get(cgroup, 0)
            if cnt >= max_per_cgroup:
                continue
            selected.append(proc)
            cgroup_count[cgroup] = cnt + 1
            if len(selected) >= num:
                break

        logger.info(
            "[disk-io] candidates (%.1fs window, top %d of %d active): %s",
            interval, len(selected), len(scored),
            ", ".join(f"{p['name']}({p['pid']})={p['io_delta'] / (1024 ** 2):.1f}MB"
                      for p in selected) or "none",
        )
        return [{'pid': p['pid'], 'name': p['name']} for p in selected]

    def _adjust_weights_by_pressure(self, psi_data):
        """Dynamically adjust weights based on PSI pressure."""
        base_weights = self.config.weights_top
        return {
            'cpu': base_weights['cpu'] * (1 + psi_data.get('cpu', 0)),
            'memory': base_weights['memory'] * (1 + psi_data.get('memory', 0)),
        }

    def _find_systemd_unit(self, pid):
        """Find the systemd scope or service for a process via systemd-cgls."""
        try:
            result = subprocess.run(
                ['systemd-cgls', '--no-page'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            # Find the line containing the given PID and its parent unit
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if f'─{pid} ' in line or f'─{pid}\n' in line:
                    # Walk up to find the nearest enclosing unit (scope or service)
                    for j in range(i, -1, -1):
                        line_content = lines[j]
                        if '.scope' in line_content or '.service' in line_content:
                            # Match lines like "├─session-c20.scope" or "├─fileManage.service"
                            unit_match = re.search(r"─(.*?\.(?:scope|service))", line_content)
                            if unit_match:
                                return unit_match.group(1)

                            # No match; try a looser pattern
                            unit_match = re.search(r"\b([\w-]+\.(?:scope|service))\b", line_content)
                            if unit_match:
                                return unit_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to find systemd unit: {str(e)}")
        return None

    def _extract_readable_app_name(self, scope_name: str) -> str:
        """Convert a systemd scope/service/slice name to a human-readable app name.

        Examples:
          snap.firefox.firefox-0e025d0b.scope  -> Firefox
          gnome-remote-desktop.service         -> Gnome Remote Desktop
          org.gnome.Shell@wayland.service      -> Gnome Shell
          session-c20.scope                    -> Session C20
          app-org.gnome.Terminal.slice         -> Gnome Terminal
        """
        name = scope_name
        # Remove trailing .scope / .service / .slice
        name = re.sub(r'\.(scope|service|slice)$', '', name)
        # Strip leading app- prefix (used in app.slice children, e.g. app-org.gnome.Terminal)
        name = re.sub(r'^app-', '', name)
        # Snap apps: snap.<appname>.<appname>-<uuid> -> appname
        snap_match = re.match(r'^snap\.([^.]+)', name)
        if snap_match:
            return snap_match.group(1).replace('-', ' ').title()
        # Strip @instance suffix (e.g. @wayland, @x11)
        name = re.sub(r'@[^@]+$', '', name)
        # Reverse-domain notation (org.gnome.Shell): take last 2 meaningful parts
        if '.' in name:
            parts = [p for p in name.split('.') if p]
            name = ' '.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        return name.replace('-', ' ').replace('_', ' ').title()

    def _get_docker_container_name(self, scope_name: str):
        """Extract Docker container name from Docker scope ID.

        Docker containers in cgroups have names like:
          docker-<64-char-container-id>.scope
          or just: Docker Def9C0F808Bbc200C4353D8963Cea606B1B327A48957D467D96Cbb5E4F

        Try to resolve the container ID to a human-readable container name using docker inspect.
        """
        import subprocess # nosec

        # Try to extract container ID from scope_name
        # Pattern 1: docker-<container_id>.scope
        match = re.match(r'^docker-([a-f0-9]{64})(?:\.scope)?$', scope_name, re.IGNORECASE)
        if match:
            container_id = match.group(1)
        else:
            # Pattern 2: Docker <container_id> (space separated, case insensitive)
            match = re.match(r'^Docker\s+([a-f0-9]+)', scope_name, re.IGNORECASE)
            if match:
                container_id = match.group(1)
            else:
                return None

        try:
            # Try to get container name using docker inspect
            # Using short container ID (first 12 chars) is usually sufficient
            short_id = container_id[:12]
            result = subprocess.run(
                ['docker', 'inspect', '--format', '{{.Name}}', short_id],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                container_name = result.stdout.strip()
                # Docker names start with /, remove it
                if container_name.startswith('/'):
                    container_name = container_name[1:]
                if container_name:
                    return f"Docker: {container_name}"
        except Exception as e:
            # Only log warning if docker command failed (not just timeout)
            if "docker" in str(e).lower() or "command not found" in str(e).lower():
                logger.warning(f"Docker not available or not installed: {e}")
            else:
                logger.debug(f"Failed to resolve Docker container name for {container_id[:12]}: {e}")

        # Fallback: show shortened container ID
        return f"Docker {container_id[:12]}"

    def try_match_app(self, process_info):
        """Try to match a desktop application or systemd scope for the given process."""
        cgroup = process_info.get('cgroup')

        # 0. For apps with explicit process_names configured, match before the
        #    generic desktop-app lookup so they take priority.
        if self._proc_name_to_app:
            dominant_name = process_info.get('dominant_name', '')
            names = process_info.get('names') or []
            if isinstance(names, set):
                names = list(names)
            check_names = [dominant_name] if dominant_name else names
            for pname in check_names:
                app_id = self._proc_name_to_app.get(pname.lower())
                if app_id:
                    app_cfg = self._multiprocess_apps.get(app_id, {})
                    return {
                        'type': 'configured',
                        'id': app_id,
                        'name': app_cfg.get('name', pname),
                    }

        # 1. Try to match a registered desktop app by process name or exe path
        if self.desktop_apps:
            exe = (process_info.get('exe') or '').strip()
            # Support both aggregated format (names: set/list) and single-process format (name: str)
            names = process_info.get('names')
            if names is None:
                proc_name = process_info.get('name', '')
                names = [proc_name] if proc_name else []
            elif isinstance(names, set):
                names = list(names)

            # When a dominant_name is available (aggregated process groups), restrict name
            # matching to that single name.  Matching against the full names list risks false
            # positives: a root-cgroup or terminal-scope group can contain hundreds of process
            # names (e.g. gnome-calculator) that are unrelated to the actual heavy workload.
            dominant_name = process_info.get('dominant_name', '')
            match_names = [dominant_name] if dominant_name else names

            # Exe first -- it is the one field a process cannot rewrite -- then the process
            # name in both its full and its kernel-truncated form. All exact: whatever
            # matches here decides which app a cgroup's usage is reported under, and so
            # which app's limit that cgroup ends up receiving.
            # 'id' stays the configured app id, which need not name a cgroup; the caller
            # resolves a controlled app to its real cgroup set before writing any limit.
            app_id = self._desktop_exe_to_app.get(exe.lower()) if exe else None
            if not app_id and exe:
                app_id = self._desktop_name_to_app.get(os.path.basename(exe).lower())
            if not app_id:
                for proc_name in match_names:
                    app_id = self._desktop_name_to_app.get((proc_name or '').strip().lower())
                    if app_id:
                        break
            if app_id:
                app = self.desktop_apps.get(app_id) or {}
                return {
                    'type': 'desktop',
                    'id': app_id,
                    'name': app.get('display_name') or app_id,
                }

        # 2. Fallback: extract a readable name from the cgroup path
        if cgroup:
            path_parts = cgroup.rstrip('/').split('/')
            scope_name = path_parts[-1]

            # Check if this is a Docker container
            docker_name = self._get_docker_container_name(scope_name)
            if docker_name:
                return {
                    'type': 'docker',
                    'id': scope_name,
                    'name': docker_name,
                }

            # vte-spawn-*.scope is the scope created by GNOME Terminal (via VTE) for child
            # processes.  The scope itself has no useful name; look one level up to the parent
            # slice (e.g. app-org.gnome.Terminal.slice) to get the terminal app name, then
            # append the dominant process name so the user sees "Gnome Terminal - stress" rather
            # than "Vte Spawn <uuid>".
            if re.match(r'^vte-spawn-', scope_name):
                parent_slice = path_parts[-2] if len(path_parts) >= 2 else ''
                terminal_name = self._extract_readable_app_name(parent_slice) if parent_slice else 'Terminal'
                dominant_name = process_info.get('dominant_name', '')
                display_name = f"{terminal_name} - {dominant_name}" if dominant_name else terminal_name
                return {
                    'type': 'cgroup',
                    'id': scope_name,
                    'name': display_name,
                }

            # Generic systemd containers (session-N.scope, user@N.service, init.scope,
            # user-N.slice, user.slice) are not apps — they group every process in a
            # login session.  Showing them as "Session 1467" hides the real workload,
            # so prefer the dominant process name (e.g. "claude") when available.
            if re.match(
                r'^(session-\d+\.scope|user@\d+\.service|init\.scope|user-\d+\.slice|user\.slice)$',
                scope_name,
            ):
                dominant_name = process_info.get('dominant_name', '')
                # Prefer the same commandline-aware identity rule used by app discovery:
                # interpreter launches (python3 foo.py) should surface foo.py, not python3.
                dominant_cmdline = (process_info.get('dominant_cmdline') or '').strip()
                if dominant_name and dominant_cmdline:
                    try:
                        cmd_tokens = shlex.split(dominant_cmdline)
                    except ValueError:
                        cmd_tokens = dominant_cmdline.split()
                    resolved = derived_process_identity({
                        'name': dominant_name,
                        'exe': process_info.get('exe') or '',
                        'cmdline': cmd_tokens,
                    }).strip()
                    if resolved:
                        dominant_name = resolved
                if dominant_name:
                    return {
                        'type': 'process',
                        'id': dominant_name,
                        'name': dominant_name,
                    }

            # A transient scope (for example tmux-spawn-<uuid>.scope) identifies
            # the sampled cgroup rather than the workload. Prefer a command-line-
            # derived identity when a representative process is available.
            dominant_name = process_info.get('dominant_name', '')
            dominant_cmdline = (process_info.get('dominant_cmdline') or '').strip()
            if dominant_name and dominant_cmdline:
                try:
                    cmd_tokens = shlex.split(dominant_cmdline)
                except ValueError:
                    cmd_tokens = dominant_cmdline.split()
                resolved = derived_process_identity({
                    'name': dominant_name,
                    'exe': process_info.get('exe') or '',
                    'cmdline': cmd_tokens,
                }).strip()
                if resolved:
                    return {
                        'type': 'process',
                        'id': resolved,
                        'name': resolved,
                    }

            return {
                'type': 'cgroup',
                'id': scope_name,
                'name': self._extract_readable_app_name(scope_name)
            }

        # 3. Try systemd-cgls lookup (for processes without cgroup info)
        pids = process_info.get('pids')
        if pids is None:
            pid = process_info.get('pid')
            pids = [pid] if pid else []
        elif isinstance(pids, set):
            pids = list(pids)

        if pids:
            unit = self._find_systemd_unit(pids[0])
            if unit:
                return {
                    'type': 'systemd',
                    'id': unit,
                    'name': self._extract_readable_app_name(unit)
                }

        return None

    def get_top_resource_consumers(self):
        """Return the single highest resource-consuming process and its application metadata."""
        results = []
        reach_threshold = True
        processes = self._get_top_processes(n=1)
        logger.debug(f"Top processes: {processes}")

        # Return empty list if top process doesn't meet minimum resource thresholds.
        # `mem_rss` is in GB (see _get_top_processes), so compare against total memory in GB.
        # Since the minimum thresholds is 30% for uncontrolled apps, larger for controlled apps,
        # we use 25% here to avoid false negatives.
        mem_total_gb = psutil.virtual_memory().total / (1024 ** 3)
        if processes and (processes[0]['cpu_avg'] < 25  # if CPU usage < 25% per core
                  and processes[0]['mem_rss'] < mem_total_gb * 0.25):  # 25% of total memory (GB)
            logger.info(f"Top process - {next(iter(processes[0]['names']), 'unknown')} corresponding "
                        f"app does not meet minimum resource thresholds")
            reach_threshold = False

        for process in processes:
            process_name = process.get('dominant_name') or next(iter(process['names']), "unknown")
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), "unknown")

            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': process.get('dominant_pid') or next(iter(process['pids']), None),
                    'name': process_name,
                    'cmdline': process_cmdline,
                    'score': round(process['score'], 3),
                    'cpu_avg': process['cpu_avg'],
                    'mem_rss': process['mem_rss'],
                    'io_read_rate': process['io_read_rate']
                },
                'app': app_info,
                'cgroup': process.get('cgroup') or '',
                # Full PID list for the sampled cgroup (bash is filtered upstream).
                'pids': list(process['pids']),
                # Unit names of additional cgroups merged into this entry.
                'extra_cgroups': [
                    os.path.basename(c) for c in process.get('extra_cgroups', [])
                ],
                # Per-cgroup memory (bytes) and CPU totals keyed by unit name.
                'per_cgroup_mem_rss': process.get('per_cgroup_mem_rss', {}),
                'per_cgroup_cpu': process.get('per_cgroup_cpu', {}),
            })

        return results, reach_threshold

    def get_top_disk_io_consumers(self, n=1):
        """Return the top ``n`` processes by disk IO and their application metadata.

        ``n`` defaults to 1 (single top consumer).  Callers that must skip
        non-throttleable apps (e.g. a Critical app that happens to be the #1 disk
        user) pass ``n > 1`` so the next candidates are available.  The candidate
        sampling floor in ``_get_top_processes`` (``max(n*3, 9)``) means small ``n``
        values cost the same as ``n=1``.
        """
        results = []
        processes = self._get_top_processes(n=n, mode="io")
        logger.debug(f"Top processes(disk io): {processes}")

        for process in processes:
            # Prefer the dominant (highest-IO) process of the cgroup over an arbitrary
            # member of the name/cmdline sets: a shared scope holds many processes, and the
            # name picked here is what get_app_control_info() matches a priority against —
            # an unrelated one silently resolves to the wrong priority (or to none at all).
            process_name = process.get('dominant_name') or next(iter(process['names']), "unknown")
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), "unknown")

            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': process.get('dominant_pid') or next(iter(process['pids']), None),
                    'name': process_name,
                    'cmdline': process_cmdline,
                    'score': round(process['score'], 3),
                    'io_read_rate': process['io_read_rate'],      # MB/s
                    'io_write_rate': process['io_write_rate'],    # MB/s
                    # IOPS is surfaced alongside MB/s because a 4k random workload is
                    # bandwidth-light but IOPS-heavy: the throttle decision needs both to
                    # tell "worth capping" from "capping this cannot relieve the device".
                    # Both come from cgroup io.stat, so the IOPS is the request count the
                    # device saw -- not a syscall count (see monitor/cgroup.py).
                    'io_read_iops': process['io_read_iops'],
                    'io_write_iops': process['io_write_iops'],
                    # {disk_name: {read_mb_s, write_mb_s, read_iops, write_iops}} -- which
                    # disks this app is actually loading, so a cap can name them instead
                    # of being written to every disk on the box.
                    'io_per_disk': process.get('io_per_disk', {}),
                },
                'app': app_info,
                'cgroup': process.get('cgroup') or '',
                # Full PID list for the sampled cgroup.
                'pids': list(process['pids']),
                'extra_cgroups': [
                    os.path.basename(c) for c in process.get('extra_cgroups', [])
                ],
                'per_cgroup_mem_rss': process.get('per_cgroup_mem_rss', {}),
                'per_cgroup_cpu': process.get('per_cgroup_cpu', {}),
            })

        logger.info(
            "[disk-io] top-%d consumers: %s", n,
            " | ".join(
                f"#{i} {r['process']['name']} app={(r['app'] or {}).get('id')!r} "
                f"rd={r['process']['io_read_rate']:.1f} wr={r['process']['io_write_rate']:.1f} MB/s "
                f"riops={r['process']['io_read_iops']:.0f} wiops={r['process']['io_write_iops']:.0f} "
                f"disks={','.join(r['process']['io_per_disk']) or '-'}"
                for i, r in enumerate(results, 1)
            ) or "none",
        )
        return results

    def _get_gpu_stats_for_pids(self, pids, sample_interval=0.3):
        """Sample Intel GPU engine utilization and memory for a collection of PIDs.

        Reads ``/proc/<pid>/fdinfo`` for every PID at t0, waits
        ``sample_interval`` seconds, then reads again at t1.  Engine busy-time
        deltas are divided by the elapsed interval to produce a utilization
        percentage; memory values are taken from the t1 snapshot.

        Args:
            pids: Iterable of process IDs belonging to one application group.
            sample_interval: Measurement window in seconds (default 0.3 s).

        Returns:
            A dict with:
                ``gpu_util``   - peak engine utilisation % across all engines (0-100).
                ``gpu_mem_mb`` - total GPU memory used in MB.
        """
        _NO_GPU = {'gpu_util': 0.0, 'gpu_mem_mb': 0.0}
        if not pids:
            return _NO_GPU

        # First snapshot - shared seen_client_ids deduplicates across all PIDs
        # in this group so that child processes which inherited a parent's DRM fd
        # don't contribute duplicate engine/memory readings.
        seen_t0 = set()
        t0_data = {}
        for pid in pids:
            data = _read_pid_fdinfo_gpu(pid, seen_t0)
            if data is not None:
                t0_data[pid] = data

        if not t0_data:
            return _NO_GPU

        time.sleep(sample_interval)
        elapsed_ns = sample_interval * 1e9

        # Second snapshot - fresh dedup set so the same pids are processed
        # consistently; only the first PID that exposes a given client_id
        # contributes to the delta and memory totals.
        seen_t1 = set()
        engine_delta = {}
        total_mem_bytes = 0

        for pid in pids:
            t1 = _read_pid_fdinfo_gpu(pid, seen_t1)
            if t1 is None:
                continue
            t0 = t0_data.get(pid)
            if t0:
                _accumulate_engine_delta(engine_delta, t0['engines'], t1['engines'])
            total_mem_bytes += t1['mem_bytes']

        # Peak engine utilisation: prefer Xe cycle-based, fall back to i915 time-based
        gpu_util = 0.0
        winning_engine = None
        for engine, data in engine_delta.items():
            util = 0.0
            if data["total_cycles"] > 0:
                util = (data["cycles"] / data["total_cycles"]) * 100
                logger.debug(
                    f"  [GPU] Xe engine={engine} delta_cy={data['cycles']} "
                    f"total_cy={data['total_cycles']} util={util:.1f}%"
                )
            elif elapsed_ns > 0 and data["time_ns"] > 0:
                util = (data["time_ns"] / elapsed_ns) * 100
                logger.debug(
                    f"  [GPU] i915 engine={engine} delta_ns={data['time_ns']} util={util:.1f}%"
                )
            if util > gpu_util:
                gpu_util = util
                winning_engine = engine

        logger.debug(
            f"  [GPU] pids={list(pids)} winning_engine={winning_engine} "
            f"gpu_util={round(min(gpu_util, 100.0), 1)}% mem_mb={round(total_mem_bytes / (1024 * 1024), 1)}"
        )
        return {
            'gpu_util': round(min(gpu_util, 100.0), 1),
            'gpu_mem_mb': round(total_mem_bytes / (1024 * 1024), 1),
        }

    def get_per_pid_gpu_stats(self, sample_interval=_GPU_SAMPLE_INTERVAL):
        """Per-PID Intel GPU utilisation and memory via fdinfo, split per device.

        Returns {pid: {pdev: {'gpu_util', 'gpu_mem_mb'}}} for PIDs holding a GPU fd.
        """
        t0_data = {}
        for pid in psutil.pids():
            try:
                data = _read_pid_fdinfo_gpu(pid)
            except Exception:
                continue
            if data is not None:
                t0_data[pid] = data

        if not t0_data:
            return {}

        time.sleep(sample_interval)
        elapsed_ns = sample_interval * 1e9

        result = {}
        for pid, t0 in t0_data.items():
            try:
                t1 = _read_pid_fdinfo_gpu(pid)
            except Exception:
                continue
            if t1 is None:
                continue

            devices = {}
            for pdev, d1 in t1['per_pdev'].items():
                d0 = t0['per_pdev'].get(pdev, {'engines': {}, 'mem_bytes': 0})
                delta = {}
                _accumulate_engine_delta(delta, d0['engines'], d1['engines'])
                devices[pdev] = {
                    'gpu_util': _peak_engine_util(delta, elapsed_ns),
                    'gpu_mem_mb': round(d1['mem_bytes'] / (1024 * 1024), 1),
                }
            if devices:
                result[pid] = devices

        return result

    def get_per_pid_io_rates(self):
        """Per-PID disk read/write rates (bytes/s) from io_counters deltas since last call."""
        now = time.time()
        rates = {}
        snapshot = {}
        for p in psutil.process_iter(['pid']):
            pid = p.info['pid']
            try:
                io = p.io_counters()
            except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError):
                continue
            snapshot[pid] = (io.read_bytes, io.write_bytes, now)
            prev = self._prev_pid_io.get(pid)
            if prev:
                dt = now - prev[2]
                if dt > 0:
                    rates[pid] = {
                        'io_read_rate': max(0, io.read_bytes - prev[0]) / dt,
                        'io_write_rate': max(0, io.write_bytes - prev[1]) / dt,
                    }
        self._prev_pid_io = snapshot
        return rates

    def get_app_resource_stats(self, n=10):
        """Return per-application CPU and memory usage data for the App Resources view (no threshold filtering).

        Unlike get_top_resource_consumers, this method:
          - returns the top n apps (default 10) rather than just top-1
          - does not check system pressure thresholds; always returns current usage
          - intended for the Dashboard "App Resources" view
          - attaches GPU engine utilisation and GPU memory info per app (sampled via fdinfo)
        """
        results = []
        processes = self._get_top_processes(n=n)
        # logger.debug(f"App resource stats processes: {processes}")

        # Collect all PIDs grouped by cgroup for a single GPU sampling pass.
        # This avoids N separate sleep intervals for N apps.
        cgroup_pids = {}
        for process in processes:
            cgroup = process['cgroup']
            if cgroup not in cgroup_pids:
                cgroup_pids[cgroup] = []
            cgroup_pids[cgroup].extend(process.get('pids', []))

        # First GPU fdinfo snapshot (t0) - iterate per-cgroup so that PIDs
        # belonging to *different* apps never share a seen_client_ids set.
        # Within each cgroup, the shared set ensures child processes that
        # inherited a parent's DRM fd are not double-counted.
        gpu_t0 = {}
        for cgroup, pids in cgroup_pids.items():
            seen_t0 = set()
            for pid in pids:
                data = _read_pid_fdinfo_gpu(pid, seen_t0)
                if data is not None:
                    gpu_t0[pid] = data

        # Wait the GPU sampling interval
        if gpu_t0:
            time.sleep(_GPU_SAMPLE_INTERVAL)
        elapsed_ns = _GPU_SAMPLE_INTERVAL * 1e9

        # Second GPU fdinfo snapshot (t1) - build per-cgroup stats
        gpu_stats_by_cgroup = {}
        for cgroup, pids in cgroup_pids.items():
            seen_t1 = set()
            engine_delta = {}
            mem_bytes = 0
            for pid in pids:
                t1 = _read_pid_fdinfo_gpu(pid, seen_t1)
                if t1 is None:
                    continue
                t0 = gpu_t0.get(pid)
                if t0:
                    _accumulate_engine_delta(engine_delta, t0['engines'], t1['engines'])
                mem_bytes += t1['mem_bytes']
            gpu_util = 0.0
            for data in engine_delta.values():
                util = 0.0
                if data["total_cycles"] > 0:
                    util = (data["cycles"] / data["total_cycles"]) * 100
                elif elapsed_ns > 0 and data["time_ns"] > 0:
                    util = (data["time_ns"] / elapsed_ns) * 100
                if util > gpu_util:
                    gpu_util = util
            gpu_stats_by_cgroup[cgroup] = {
                'gpu_util': round(min(gpu_util, 100.0), 1),
                'gpu_mem_mb': round(mem_bytes / (1024 * 1024), 1),
            }

        for process in processes:
            process_name = process.get('dominant_name') or next(iter(process['names']), 'unknown')
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), 'unknown')

            app_match = self.try_match_app(process)
            app_id = app_match['id'] if app_match else process_name
            app_name = app_match['name'] if app_match else process_name
            match_type = app_match['type'] if app_match else 'none'
            logger.debug(
                f"  [AppName] process={process_name!r} cgroup={process['cgroup']!r} "
                f"-> app_name={app_name!r} (match_type={match_type})"
            )

            gpu_stats = gpu_stats_by_cgroup.get(process['cgroup'], {'gpu_util': 0.0, 'gpu_mem_mb': 0.0})

            pid_list, is_self, balancer_candidate, status = _process_control_flags(
                process['pids'], process_name, process_cmdline)
            results.append({
                'app_id': app_id,
                'app_name': app_name,
                'pid': next(iter(process['pids']), None),
                'pids': pid_list,
                'process_name': process_name,
                'cmdline': process_cmdline,
                'status': status,
                'is_self': is_self,
                'balancer_candidate': balancer_candidate,
                'cpu_usage': round(process['cpu_avg'] / 100, 4),       # normalize to 0-1
                'memory_mb': round(process['mem_rss'] * 1024, 1),      # GB -> MB
                'io_read_rate': process['io_read_rate'],                # MB/s
                'io_write_rate': process['io_write_rate'],              # MB/s
                'score': round(process['score'], 3),
                'gpu_util': gpu_stats['gpu_util'],                      # % (0-100)
                'gpu_mem_mb': gpu_stats['gpu_mem_mb'],                  # MB
            })

        # Re-sort results to surface GPU-heavy apps that might have low CPU/mem scores.
        # gpu_util (0-100) is weighted by the configured gpu weight so that an app with
        # significant GPU usage is ranked above idle apps even when its CPU+mem score is
        # lower.  The original 'score' field keeps its CPU+mem meaning for display purposes.
        gpu_weight = self.config.weights_top.get('gpu', 1)
        results.sort(
            key=lambda r: r['score'] + gpu_weight * r['gpu_util'],
            reverse=True
        )

        return results

    def get_app_disk_io_stats(self, n=10):
        """Return per-application Disk I/O usage data for the App Resources view (no threshold filtering).

        Unlike get_top_disk_io_consumers, this method:
          - returns the top n apps (default 10) rather than just top-1
          - intended for the Dashboard "App Resources" Disk I/O view
        """
        results = []
        processes = self._get_top_processes(n=n, mode="io")
        # logger.debug(f"App disk I/O stats processes: {processes}")

        for process in processes:
            process_name = process.get('dominant_name') or next(iter(process['names']), 'unknown')
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), 'unknown')

            app_match = self.try_match_app(process)
            # Use resolved app name as display name; fall back to dominant process name
            app_name = app_match['name'] if app_match else process_name

            pid_list, is_self, balancer_candidate, status = _process_control_flags(
                process['pids'], process_name, process_cmdline)
            results.append({
                'pid': next(iter(process['pids']), None),
                'pids': pid_list,
                'name': process_name,
                'app_name': app_name,
                'cmdline': process_cmdline,
                'status': status,
                'is_self': is_self,
                'balancer_candidate': balancer_candidate,
                'io_read_rate': process['io_read_rate'],                # MB/s
                'io_write_rate': process['io_write_rate'],              # MB/s
                'io_read_iops': process['io_read_iops'],                # ops/s (device requests)
                'io_write_iops': process['io_write_iops'],              # ops/s (device requests)
                'io_per_disk': process.get('io_per_disk', {}),          # {disk: {...rates}}
                'score': round(process['score'], 3),
            })

        return results

    def get_total_memory(self):
        """Return total physical memory in MB."""
        mem = psutil.virtual_memory()
        total_memory_mb = round(mem.total / (1024 ** 2), 2)
        return total_memory_mb

    def get_physical_disks(self):
        """Return a list of all physical disk device names."""
        return self._disk_io.get_physical_disks()

    def get_resource_usage(self) -> dict:
        """Return overall system resource utilisation and available capacity."""
        # CPU: core count, utilisation (%)
        cpu_count = psutil.cpu_count(logical=True)
        cpu_usage = psutil.cpu_percent(interval=0.5)  # 0.5-second sample
        cpu_available = 100 - cpu_usage  # remaining CPU (%)

        # Memory: total capacity (GB), utilisation (%), available ratio
        mem = psutil.virtual_memory()
        mem_total_gb = round(mem.total / (1024 **3), 2)
        mem_usage = mem.percent
        mem_available_ratio = round(mem.available / mem.total, 2)  # available memory ratio

        return {
            'cpu': {
                'count': cpu_count,
                'usage': cpu_usage,
                'available': cpu_available,
                'is_busy': cpu_usage > self.config.cpu_busy_threshold
            },
            'memory': {
                'total_gb': mem_total_gb,
                'usage': mem_usage,  # %
                'available_ratio': mem_available_ratio,
                'is_busy': mem_usage > self.config.memory_busy_threshold
            }
        }

    def is_disk_io_stressed(self, device: str = None, threshold: float = None) -> dict:
        """Determine whether disk I/O is under stress (see DiskIOMonitor.is_disk_io_stressed)."""
        return self._disk_io.is_disk_io_stressed(device, threshold)

    def get_disk_pressure_model(self) -> dict:
        """Return the USE-model knobs actually in force: ``config.disk_pressure_model``
        merged over the defaults in disk_pressure.py (see DiskIOMonitor._model)."""
        return self._disk_io._model()

def main():
    """Debug entry point."""
    logger.info("==== Starting Resource Monitor ====")

    monitor = ResourceMonitor()

    try:
        while True:
            results = monitor.get_top_resource_consumers()
            for i, result in enumerate(results, 1):
                logger.debug(f"\n=== Top Resource Consumer #{i} ===")
                logger.debug(f"Process: {result['process']['name']} (PID: {result['process']['pid']})")
                logger.debug(f"CPU: {result['process']['cpu']}% | Memory: {result['process']['memory_mb']}MB")
                logger.debug(f"Score: {result['process']['score']:.2f}")
                logger.debug(f"Cmd: {result['process']['cmdline'][:100]}...")

                if result['app']:
                    logger.debug(f"\nMatched to: {result['app']['name']} ({result['app']['type']})")
                    logger.debug(f"ID: {result['app']['id']}")
                else:
                    logger.debug("\nNo matching application found")

            sleep(5)

    except KeyboardInterrupt:
        logger.info("\nMonitoring stopped by user")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    main()
