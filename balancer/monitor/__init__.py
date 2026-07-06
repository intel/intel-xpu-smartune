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
        CgroupMonitor,
        PressureAnalyzer,
        NetworkMonitor,
        WindowDiffHistory,
        AppIntercept,
    )
"""

from .psi import PSIMonitor
from .res_monitor import ResourceMonitor
from .cgroup import CgroupMonitor
from .pressure import PressureAnalyzer
from .network import NetworkMonitor, WindowDiffHistory
from .app_intercept import AppIntercept

__all__ = [
    "PSIMonitor",
    "ResourceMonitor",
    "CgroupMonitor",
    "PressureAnalyzer",
    "NetworkMonitor",
    "WindowDiffHistory",
    "AppIntercept",
]