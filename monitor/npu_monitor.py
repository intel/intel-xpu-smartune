# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# PMT telemetry logic derived from intel/linux-npu-driver (MIT License)
# https://github.com/intel/linux-npu-driver

import os
import logging as LOG
import shlex
import subprocess # nosec

def run_command(command, timeout=None):
    if isinstance(command, str):
        command = shlex.split(command)
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                              check=True, shell=False, encoding='ascii', errors='ignore', cwd=None)
    except subprocess.TimeoutExpired as err:
        if not err.stdout:
            err.stdout = ''
        return subprocess.CompletedProcess(command, 1, err.stdout)
    except subprocess.CalledProcessError as err:
        if not err.stdout:
            err.stdout = ''
        return subprocess.CompletedProcess(command, err.returncode, err.stdout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 0, "Executable not found")

_UNIT_TO_KIB = {'': 1 / 1024, 'KiB': 1, 'MiB': 1024, 'GiB': 1024 * 1024}

def parse_npu_fdinfo(content):
    """Parse one fdinfo file; return (client_id, resident_kib) for an intel_vpu fd, else None.

    resident_kib is `drm-resident-memory`, normalised to KiB: the buffers this
    client holds with backing pages (system memory). It is what the process
    *holds*, not whether it is running on the NPU. The DRM core picks the unit
    per value: "4096", "12345 KiB" or "64 MiB". client_id is None when absent.
    """
    driver = None
    client_id = None
    memory_kib = 0
    for line in content.splitlines():
        key, _, val = line.partition(':')
        key, val = key.strip(), val.strip()
        if key == 'drm-driver':
            driver = val
        elif key == 'drm-client-id':
            try:
                client_id = int(val)
            except ValueError:
                pass
        elif key == 'drm-resident-memory':
            fields = val.split()
            try:
                unit = fields[1] if len(fields) > 1 else ''
                memory_kib = int(int(fields[0]) * _UNIT_TO_KIB[unit])
            except (ValueError, IndexError, KeyError):
                memory_kib = 0
    if driver != 'intel_vpu':
        return None
    return client_id, memory_kib

def check_fdinfo_for_intel_vpu(pid, fd_name):
    """Check if a file descriptor belongs to intel_vpu driver and extract memory info."""
    fdinfo_path = f'/proc/{pid}/fdinfo/{fd_name}'
    if not os.path.exists(fdinfo_path):
        return None

    try:
        with open(fdinfo_path, 'r') as f:
            fdinfo_content = f.read()
    except (OSError, PermissionError):
        return None

    LOG.debug('FDInfo for PID %d FD %s: %s', pid, fd_name, fdinfo_content)

    parsed = parse_npu_fdinfo(fdinfo_content)
    if parsed is None:
        return None

    LOG.debug('Found NPU process PID %d', pid)
    return parsed[1]

def get_process_command(pid, fallback_command):
    """Get the full command line for a process."""
    try:
        with open(f'/proc/{pid}/cmdline', 'r') as cmd_file:
            cmdline = cmd_file.read().replace('\0', ' ').strip()
            return cmdline if cmdline else fallback_command
    except (OSError, PermissionError):
        return fallback_command

def process_pid_fds(pid, fallback_command):
    """Process file descriptors for a PID to find intel_vpu usage."""
    proc_fd_dir = f'/proc/{pid}/fd'
    if not os.path.exists(proc_fd_dir):
        return None

    try:
        fd_list = os.listdir(proc_fd_dir)
    except (OSError, PermissionError):
        return None

    for fd_name in fd_list:
        try:
            fd_link = os.readlink(os.path.join(proc_fd_dir, fd_name))
            if '/dev/accel/accel' not in fd_link:
                continue

            memory_kib = check_fdinfo_for_intel_vpu(pid, fd_name)
            if memory_kib is not None:
                command = get_process_command(pid, fallback_command)
                return {'pid': pid, 'command': command, 'memory_kib': memory_kib}
        except (OSError, PermissionError):
            continue

    return None

def get_npu_processes(dev_file):  # pylint: disable=too-many-branches
    """Get list of processes using the NPU by checking fdinfo for intel_vpu driver."""
    processes = []

    # Run lsof to get list of PIDs using /dev/accel/accel[N]
    result = run_command(['lsof', dev_file])
    if result.returncode != 0:
        return processes

    lines = result.stdout.strip().split('\n')
    if len(lines) <= 1:  # Only header or empty
        return processes

    # Parse lsof output and collect unique PIDs
    seen_pids = set()
    for line in lines[1:]:  # Skip header
        parts = line.split()
        if len(parts) < 2:
            continue

        try:
            pid = int(parts[1])
        except (ValueError, IndexError):
            continue

        if pid in seen_pids:
            continue

        seen_pids.add(pid)
        fallback_command = parts[0]

        proc_info = process_pid_fds(pid, fallback_command)
        if proc_info:
            processes.append(proc_info)

    return processes
