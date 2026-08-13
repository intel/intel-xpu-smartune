# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import time
from collections import defaultdict
from typing import Dict, Optional


class PSIMonitor:
    """Singleton PSI monitor that exposes current system pressure data."""
    # Singleton instance
    _instance: Optional['PSIMonitor'] = None
    # PSI file paths
    _PRESSURE_FILES = {
        'cpu': "/proc/pressure/cpu",
        'memory': "/proc/pressure/memory",
        'io': "/proc/pressure/io"
    }
    # Trigger config: (some threshold (ms), window (sec))
    _TRIGGER_CONFIG = {
        'cpu': (100, 5),
        'memory': (1, 5),
        'io': (100, 5)
    }

    def __new__(cls):
        """Singleton constructor: ensures only one instance exists globally."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # Initialise resources only on first instantiation
            cls._instance._fds = {}
            cls._instance._last_total = {}
            # History for the self-inflicted-fraction path (system + per-cgroup),
            # kept separate from the trigger-driven window above.
            cls._instance._frac_last = {}
            # EWMA state for the smoothed self-inflicted fraction, per resource
            # (see get_self_inflicted_fraction for why smoothing is needed).
            cls._instance._frac_smoothed = {}
            # Last (time, total) for the io 'full' delta rate (see get_io_full_pressure).
            # Isolated from the 'some'/self-fraction state above.
            cls._instance._io_full_last = None
            cls._instance._pressure_history = defaultdict(list)
            cls._instance._last_pressure = {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}
            cls._instance._window_sec = 5
            # Set up file descriptors and trigger conditions
            cls._instance._setup_resources()
        return cls._instance

    def _setup_resources(self):
        """Open PSI file descriptors and configure trigger conditions (called once at init)."""
        try:
            # Open PSI files for reading (read-write + non-blocking)
            for resource, path in self._PRESSURE_FILES.items():
                self._fds[resource] = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            # Configure trigger conditions
            for resource, fd in self._fds.items():
                self._setup_trigger(fd, resource)
        except OSError as e:
            raise RuntimeError(f"PSI resource initialisation failed: {str(e)}") from e

    def _setup_trigger(self, fd: int, resource: str):
        """Write the PSI trigger string for the given resource file descriptor."""
        some_ms, window_sec = self._TRIGGER_CONFIG[resource]
        # Trigger format: some <threshold (µs)> <window (µs)>
        trigger = f"some {some_ms * 1000} {window_sec * 1000000}\n"
        os.write(fd, trigger.encode())
        os.lseek(fd, 0, os.SEEK_SET)  # reset file pointer

    def _parse_total(self, data: str) -> int:
        """Extract the cumulative 'total' stall time (µs) from a PSI data string."""
        for line in data.split('\n'):
            if line.startswith('some'):
                return int(line.split('total=')[-1])
        return 0

    def _get_resource_pressure(self, resource: str) -> float:
        """Compute current pressure for a single resource in the range [0, 1]."""
        fd = self._fds[resource]
        now = time.time()
        os.lseek(fd, 0, os.SEEK_SET)

        try:
            data = os.read(fd, 1024).decode()
        except OSError as e:
            raise RuntimeError(f"Failed to read {resource} PSI data: {str(e)}") from e

        current_total = self._parse_total(data)
        # First read: initialise history and return 0
        if resource not in self._last_total:
            self._last_total[resource] = (now, current_total)
            return 0.0

        # Pressure = (total_delta in seconds) / elapsed_seconds
        last_time, last_total = self._last_total[resource]
        time_delta = now - last_time
        total_delta = current_total - last_total

        if time_delta <= 0:
            pressure = 0.0
        else:
            pressure = (total_delta / 1_000_000) / time_delta  # µs → s
            pressure = max(0.0, min(pressure, 1.0))  # clamp to [0, 1]

        # Update history
        self._last_total[resource] = (now, current_total)
        self._pressure_history[resource].append((now, pressure))
        self._last_pressure[resource] = pressure
        # Evict data points outside the rolling window
        self._clean_old_data(resource)
        return pressure

    def _clean_old_data(self, resource: str):
        """Remove history entries outside the rolling window for the given resource."""
        cutoff = time.time() - self._window_sec
        self._pressure_history[resource] = [
            (t, p) for t, p in self._pressure_history[resource] if t >= cutoff
        ]
        # Backfill with the last known pressure when the window is empty to avoid gaps
        if not self._pressure_history[resource] and self._last_pressure[resource] > 0:
            self._pressure_history[resource].append((cutoff + 0.1, self._last_pressure[resource]))

    def _get_window_average(self, resource: str) -> float:
        """Return the rolling-window average pressure for the given resource."""
        history = self._pressure_history[resource]
        return sum(p for _, p in history) / len(history) if history else 0.0

    def get_current_pressure(self) -> Dict[str, float]:
        """
        Public API: return current rolling-window average pressure for each resource.
        Returns: {'cpu': 0.xx, 'memory': 0.xx, 'io': 0.xx}
        """
        # Refresh pressure data for all resources
        for resource in self._PRESSURE_FILES.keys():
            self._get_resource_pressure(resource)
        # Return window averages
        return {
            'cpu': round(self._get_window_average('cpu'), 2),
            'memory': round(self._get_window_average('memory'), 2),
            'io': round(self._get_window_average('io'), 2)
        }

    _CGROUP_MOUNT = "/sys/fs/cgroup"
    # EWMA smoothing factor for the self-inflicted fraction, in (0, 1]. The raw
    # fraction is a single-interval ratio (Σ cgroup_rate / system_rate) and is
    # therefore noisy tick-to-tick; smoothing across ticks damps the swing before it
    # feeds the discount. Smaller = smoother but slower to react; ~0.3 keeps roughly a
    # 3-4 sample memory at a ~5s cadence.
    _FRAC_EWMA_ALPHA = 0.3

    @staticmethod
    def _parse_some_total(path: str) -> Optional[int]:
        """Return the cumulative ``some total`` stall time (µs) from a PSI file, or None."""
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("some"):
                        return int(line.split("total=")[-1])
        except (OSError, ValueError):
            return None
        return None

    def _some_total_delta_rate(self, key, path: str) -> Optional[float]:
        """Instantaneous ``some`` pressure in [0, 1] from cumulative ``total`` deltas,
        using the same total-delta method as system PSI (see _get_resource_pressure).

        ``key`` scopes the history so system and cgroup readings taken in the same call
        share one consistent interval. Returns None on the first read (no interval yet)
        or when the file is unavailable.
        """
        now = time.time()
        total = self._parse_some_total(path)
        if total is None:
            return None
        last = self._frac_last.get(key)
        self._frac_last[key] = (now, total)
        if last is None:
            return None
        time_delta = now - last[0]
        if time_delta <= 0:
            return None
        rate = (total - last[1]) / 1_000_000 / time_delta  # µs → s
        return max(0.0, min(rate, 1.0))

    def get_self_inflicted_fraction(self, cgroup_rel_paths) -> Dict[str, float]:
        """Estimate how much of each resource's system pressure is self-inflicted by the
        set of currently rate-limited cgroups, as a fraction in [0, 1] per resource.

        For each resource: ``min(1, (Σ cgroup_rate) / system_rate)`` over the given cgroups,
        both taken from the ``some``-pressure total-delta method on the cgroup's
        ``<res>.pressure`` file vs the system PSI file (unit-independent ratio). Attribution
        is pressure-based on purpose: a throughput/usage share was tried and was worse for
        stall-driven loads (e.g. ``stress --io`` generates IO-wait via ``sync()`` while
        moving almost no bytes, so a byte share collapsed to ~0 and under-discounted). When
        the already-limited apps are the sole source of a resource's stalls the fraction
        approaches 1; when other (unlimited) tasks also stall it drops, leaving their (real)
        pressure intact. Aggregating over all limited cgroups means once every top consumer
        is limited their combined pressure is discounted, so the score stops over-reporting
        and restore can proceed. Returns all-zero (no discount) when unavailable.

        The per-interval ratio is noisy, so each resource's fraction is EWMA-smoothed across
        calls (see ``_FRAC_EWMA_ALPHA``). A resource with no fresh measurement this call
        carries forward its last smoothed value rather than collapsing to 0, so a momentary
        sampling gap does not briefly drop the discount and spike the score. The scorer only
        consumes the CPU/IO fractions (hard-limited resources); memory is governed instead by
        the availability-driven scarcity gate (PressureAnalyzer._mem_scarcity_gate).

        Accepts a single path or a list of paths.
        """
        if not cgroup_rel_paths:
            return {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}
        if isinstance(cgroup_rel_paths, str):
            cgroup_rel_paths = [cgroup_rel_paths]

        # Raw single-interval fraction per resource; only populated when a valid
        # system rate and at least one cgroup rate are available this call.
        raw = {}
        for res in ('cpu', 'io'):
            sys_rate = self._some_total_delta_rate(('sys', res), self._PRESSURE_FILES[res])
            cg_sum, have_cg = 0.0, False
            for path in cgroup_rel_paths:
                cg_rate = self._some_total_delta_rate(
                    (path, res),
                    os.path.join(self._CGROUP_MOUNT, path.lstrip('/'), f"{res}.pressure"))
                if cg_rate is not None:
                    cg_sum += cg_rate
                    have_cg = True
            if sys_rate and have_cg:
                raw[res] = min(1.0, cg_sum / sys_rate)

        alpha = self._FRAC_EWMA_ALPHA
        fraction = {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}
        for res in ('cpu', 'memory', 'io'):
            if res in raw:
                prev = self._frac_smoothed.get(res)
                cur = raw[res] if prev is None else alpha * raw[res] + (1.0 - alpha) * prev
                self._frac_smoothed[res] = cur
                fraction[res] = cur
            else:
                # No fresh sample: hold the last smoothed value (0.0 if never measured).
                fraction[res] = self._frac_smoothed.get(res, 0.0)
        return fraction

    @staticmethod
    def _parse_full_total(path: str) -> Optional[int]:
        """Return the cumulative ``full total`` stall time (µs) from a PSI file, or None.

        ``full`` means every non-idle task is stalled on the resource -- i.e. the
        system as a whole cannot make progress. Separate from the ``some`` parser so
        the existing 'some'/self-fraction path stays untouched.
        """
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("full"):
                        return int(line.split("total=")[-1])
        except (OSError, ValueError):
            return None
        return None

    def get_io_full_pressure(self) -> float:
        """Instantaneous io ``full`` pressure in [0, 1] from cumulative-total deltas.

        This is the system-wide-blocking signal the disk-pressure gate uses to detect
        the critical band (all non-idle tasks stalled on IO). Kept fully independent of
        the ``some`` window-average path and the self-inflicted-fraction state: its own
        ``_io_full_last`` timestamp, its own fresh read of /proc/pressure/io. Returns 0.0
        on the first read (no interval yet) or when the file is unavailable.
        """
        now = time.time()
        total = self._parse_full_total(self._PRESSURE_FILES['io'])
        if total is None:
            return 0.0
        last = self._io_full_last
        self._io_full_last = (now, total)
        if last is None:
            return 0.0
        time_delta = now - last[0]
        if time_delta <= 0:
            return 0.0
        rate = (total - last[1]) / 1_000_000 / time_delta  # µs → s
        return max(0.0, min(rate, 1.0))

    def cleanup(self):
        """Release resources: close all PSI file descriptors (call on program exit)."""
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        # Reset the singleton (useful in tests)
        PSIMonitor._instance = None

    def __del__(self):
        """Destructor: ensure resources are released."""
        self.cleanup()
