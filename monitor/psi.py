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
