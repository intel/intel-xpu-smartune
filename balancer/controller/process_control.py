# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import signal

import psutil

from utils.logger import logger
from utils.self_ident import is_own_process

# Never signal init / very low PIDs.
_MIN_KILLABLE_PID = 2


def _resolve_process(pid):
    """Validate the pid and return (name, cmdline, exe) hints, or (err, None, None).

    Returns a tuple where a non-None second element means success; when the pid is
    invalid or protected the first element is the human-readable refusal message.
    """
    if not isinstance(pid, int) or pid < _MIN_KILLABLE_PID:
        return f"Refusing to signal pid {pid}", None, None

    name, cmdline, exe = str(pid), "", ""
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        cmdline = " ".join(proc.cmdline())
        exe = proc.exe()
    except psutil.NoSuchProcess:
        return f"Process {pid} no longer exists", None, None
    except psutil.AccessDenied:
        pass

    if is_own_process(pid, cmdline, exe):
        return f"Refusing to signal the SmartTune service itself (pid {pid})", None, None

    return name, cmdline, exe


def kill_process(pid, force=False):
    """Send SIGTERM (or SIGKILL when force) to a PID. Returns (ok, message)."""
    name, cmdline, _ = _resolve_process(pid)
    if cmdline is None:
        return False, name  # name holds the refusal message

    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return False, f"Process {pid} no longer exists"
    except PermissionError:
        return False, f"Permission denied to signal process {pid}"

    action = "Killed" if force else "Terminated"
    logger.info(f"{action} process {name} (pid={pid})")
    return True, f"{action} {name} (pid={pid})"


def suspend_process(pid, resume=False):
    """Freeze (SIGSTOP) or resume (SIGCONT) a PID. Returns (ok, message)."""
    name, cmdline, _ = _resolve_process(pid)
    if cmdline is None:
        return False, name  # name holds the refusal message

    try:
        os.kill(pid, signal.SIGCONT if resume else signal.SIGSTOP)
    except ProcessLookupError:
        return False, f"Process {pid} no longer exists"
    except PermissionError:
        return False, f"Permission denied to signal process {pid}"

    action = "Resumed" if resume else "Suspended"
    logger.info(f"{action} process {name} (pid={pid})")
    return True, f"{action} {name} (pid={pid})"
