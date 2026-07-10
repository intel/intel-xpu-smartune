# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# PMT telemetry logic derived from intel/linux-npu-driver (MIT License)
# https://github.com/intel/linux-npu-driver

import os
import logging as LOG
import shlex
import subprocess # nosec
import enum

PMT_GUID_MTL = "0x130670b2"   # Meteor Lake telemetry GUID
PMT_GUID_ARL = "0x1306a0b3"   # Arrow Lake telemetry GUID
PMT_GUID_ARL_H = "0x1306a0b2" # Arrow Lake-H telemetry GUID
PMT_GUID_ARL_S = "0x1306a0b4" # Arrow Lake-S telemetry GUID
PMT_GUID_LNL = "0x3072005"    # Lunar Lake telemetry GUID
PMT_GUID_PTL = "0x3086000"    # Panther Lake telemetry GUID


def get_mtl_regs():
    return {
        'VPU_ENERGY': 0x628,
        'SOC_TEMPERATURES': 0x98,
        'VPU_WORKPOINT': 0x68,
        'VPU_MEMORY_BW': 0x0,
    }

def get_arl_regs():
    return get_mtl_regs()

def get_lnl_regs():
    return {
        'VPU_ENERGY': 0x5d0,
        'SOC_TEMPERATURES': 0x70,
        'VPU_WORKPOINT': 0x18,
        'VPU_MEMORY_BW': 0xc18
    }

def get_ptl_regs():
    return {
        'VPU_ENERGY': 0x670,
        'SOC_TEMPERATURES': 0x78,
        'VPU_WORKPOINT': 0x18,
        'VPU_MEMORY_BW': 0xc18
    }

class CpuGen(enum.IntEnum):
    MTL = 0
    ARL = 1
    LNL = 2
    PTL = 3

    def __str__(self):
        if self == CpuGen.MTL:
            return "Meteor Lake"
        if self == CpuGen.ARL:
            return "Arrow Lake"
        if self == CpuGen.LNL:
            return "Lunar Lake"
        if self == CpuGen.PTL:
            return "Panther Lake"
        return ""

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

def fdump(path):
    with open(path, "r") as f:
        return f.read().strip()

class PmtTelemetry:
    def __init__(self):
        self.pmt_root = "/sys/class/intel_pmt"
        self.buffer = None
        self.regs = None
        self.telemetry_path = None
        self.cpu_gen = None

        self.vpu_energy_reg = None
        self.soc_temperatures_reg = None

        # Check if PMT sysfs exists
        if not os.path.exists(self.pmt_root):
            raise RuntimeError(f'PMT sysfs interface not found at {self.pmt_root}')

        for telem_dir in os.listdir(self.pmt_root):
            if not telem_dir.startswith('telem'):
                continue

            telem_path = os.path.join(self.pmt_root, telem_dir)
            guid_path = os.path.join(telem_path, 'guid')
            telemetry_path = os.path.join(telem_path, 'telem')
            size_path = os.path.join(telem_path, 'size')
            offset_path = os.path.join(telem_path, 'offset')

            if not all(os.path.exists(p) for p in [guid_path, telemetry_path, size_path, offset_path]):
                continue

            guid = fdump(guid_path)
            telem_size = int(fdump(size_path))
            telem_offset = int(fdump(offset_path))

            LOG.debug('Found PMT device %s with GUID %s, size %d, offset %d',
                     telem_dir, guid, telem_size, telem_offset)

            self.telemetry_path = telemetry_path
            if guid == PMT_GUID_MTL:
                self.cpu_gen = CpuGen.MTL
                self.regs = get_mtl_regs()
                break
            if guid in (PMT_GUID_ARL, PMT_GUID_ARL_H, PMT_GUID_ARL_S):
                self.cpu_gen = CpuGen.ARL
                self.regs = get_arl_regs()
                break
            if guid == PMT_GUID_LNL:
                self.cpu_gen = CpuGen.LNL
                self.regs = get_lnl_regs()
                break
            if guid == PMT_GUID_PTL:
                self.cpu_gen = CpuGen.PTL
                self.regs = get_ptl_regs()
                break

        if self.cpu_gen is None:
            raise RuntimeError('No CPU telemetry devices found with known GUIDs')

        LOG.debug('CPU generation detected: %s', self.cpu_gen)

    def read(self, offset, msb, lsb):
        """Function get_telem_sample slices bits from buffer buf at the container offset
        and bit masking specified by sample_spec."""
        buf = self.buffer
        # read 8 bytes from buffer from offset and convert it to 64 bit little endian integer
        data = int.from_bytes(buf[offset:offset + 8],
                              byteorder='little')
        # create mask
        msb_mask = 0xffffffffffffffff & ((2 ** (int(msb) + 1)) - 1)
        lsb_mask = 0xffffffffffffffff & ((2 ** (int(lsb))) - 1)
        mask = msb_mask & (~lsb_mask)
        # apply mask and shift right
        value = (data & mask) >> int(lsb)
        return value

    def update_buffer(self):
        with open(self.telemetry_path, 'rb') as fd:
            self.buffer = fd.read()

    def get_freq(self):
        raw = self.read(self.regs['VPU_WORKPOINT'], 7, 0)
        if self.cpu_gen == CpuGen.MTL:
            return 2 * raw / 3 / 10
        return 0.05 * raw

    def get_voltage(self):
        return self.read(self.regs['VPU_WORKPOINT'], 15, 8)

    def get_tile_config(self):
        return self.read(self.regs['VPU_WORKPOINT'], 23, 16)

    def get_npu_temperature(self):
        return self.read(self.regs['SOC_TEMPERATURES'], 47, 40)

    def get_npu_energy(self):
        # Units: joules, Cast: U32.18.14
        val = self.read(self.regs['VPU_ENERGY'], 63, 0)
        int_part = val >> 14
        float_part = (val & ((1 << 14) - 1)) / (1 << 14)
        return int_part + float_part

    def get_noc_bandwidth(self):
        val = self.read(self.regs['VPU_MEMORY_BW'], 31, 0)
        return val / 1e3

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

    # Check if it's intel_vpu driver
    if 'drm-driver:\tintel_vpu' not in fdinfo_content and 'drm-driver:     intel_vpu' not in fdinfo_content:
        return None

    LOG.debug('Found NPU process PID %d', pid)

    # Extract drm-resident-memory from fdinfo (in KiB)
    memory_kib = 0
    for line in fdinfo_content.split('\n'):
        if line.startswith('drm-resident-memory:'):
            try:
                # Format: "drm-resident-memory:	12345 KiB"
                memory_kib = int(line.split()[1])
            except (ValueError, IndexError):
                memory_kib = 0
            break

    return memory_kib

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
