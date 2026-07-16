# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Identify SmartTune's own processes so the dashboard never manages / kills itself.

"Self" spans more than the running Python service: the balancer, the monitor
API and the dashboard dev server (node / esbuild / vite) all live under the
project root.  We recognise them by matching the project root path in a
process's cmdline / exe, plus the running service's own pid pair as a floor.
"""

import os

# Project root = parent of this utils/ directory.
SMARTUNE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_own_process(pid=None, cmdline="", exe="") -> bool:
    """True when the process belongs to SmartTune itself.

    Any of the caller-supplied hints is sufficient; all are optional so both the
    monitor (which has cmdline) and the killer (which has exe) can reuse it.
    """
    if pid is not None and pid in (os.getpid(), os.getppid()):
        return True
    if cmdline and SMARTUNE_ROOT in cmdline:
        return True
    if exe and exe.startswith(SMARTUNE_ROOT):
        return True
    return False
