# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
monitor package – public API for all system monitoring components.

Importing from this package is preferred over importing individual sub-modules
directly, because it decouples callers from the internal file layout and makes
it easy to relocate or rename implementation files in the future.

Usage::

    from monitor import (
        PSIMonitor,
        ResourceMonitor,
        PressureAnalyzer,
        NetworkMonitor,
        WindowDiffHistory,
        snapshot_cgroup_io,
        io_stat_deltas,
    )
"""

from .psi import PSIMonitor
from .cgroup import (
    CGROUP_MOUNT,
    IO_STAT_FIELDS,
    as_io_rates,
    io_stat_deltas,
    read_cgroup_io_stat,
    snapshot_cgroup_io,
)
from .res_monitor import ResourceMonitor
from .pressure import PressureAnalyzer
from .network import NetworkMonitor, WindowDiffHistory

__all__ = [
    "PSIMonitor",
    "ResourceMonitor",
    "PressureAnalyzer",
    "NetworkMonitor",
    "WindowDiffHistory",
    # cgroup v2 io.stat accounting (see monitor/cgroup.py)
    "CGROUP_MOUNT",
    "IO_STAT_FIELDS",
    "as_io_rates",
    "io_stat_deltas",
    "read_cgroup_io_stat",
    "snapshot_cgroup_io",
]