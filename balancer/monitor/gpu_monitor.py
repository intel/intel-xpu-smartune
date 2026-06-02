# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import ctypes
import ctypes.util
import os
import stat
import struct
import sys
import time
from pathlib import Path

VERBOSE = False

def vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, file=sys.stderr, **kwargs)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# perf_event_open syscall number (x86_64)
__NR_perf_event_open = 298

# perf read_format flags
PERF_FORMAT_TOTAL_TIME_ENABLED = 1 << 0
PERF_FORMAT_TOTAL_TIME_RUNNING = 1 << 1
PERF_FORMAT_ID = 1 << 2
PERF_FORMAT_GROUP = 1 << 3

# Clock
CLOCK_MONOTONIC = 1

# i915 PMU engine config bit layout
I915_PMU_SAMPLE_BITS = 4
I915_PMU_SAMPLE_INSTANCE_BITS = 8
I915_PMU_CLASS_SHIFT = I915_PMU_SAMPLE_BITS + I915_PMU_SAMPLE_INSTANCE_BITS

I915_SAMPLE_BUSY = 0
I915_SAMPLE_WAIT = 1
I915_SAMPLE_SEMA = 2

# __I915_PMU_OTHER(0) boundary - configs >= this are non-engine counters
_I915_PMU_OTHER_0 = ((0xff << I915_PMU_CLASS_SHIFT) |
                     (0xff << I915_PMU_SAMPLE_BITS) | 0xf) + 1

_I915_PMU_GT_SHIFT = 60

# i915 non-engine counter offsets
_I915_PMU_FREQ_ACT = 0
_I915_PMU_FREQ_REQ = 1
_I915_PMU_INTERRUPTS = 2
_I915_PMU_RC6 = 3

# i915 engine classes
I915_ENGINE_CLASSES = {
    0: "Render/3D",
    1: "Blitter",
    2: "Video",
    3: "VideoEnhance",
    4: "Compute",
}

# Xe engine classes (same values, kept separate for clarity)
XE_ENGINE_CLASSES = {
    0: "Render/3D",
    1: "Blitter",
    2: "Video",
    3: "VideoEnhance",
    4: "Compute",
}

# ---------------------------------------------------------------------------
# perf_event_open syscall wrapper
# ---------------------------------------------------------------------------

class PerfEventAttr(ctypes.Structure):
    """Minimal struct perf_event_attr for PMU counter access."""
    _fields_ = [
        ("type",            ctypes.c_uint32),
        ("size",            ctypes.c_uint32),
        ("config",          ctypes.c_uint64),
        ("sample_period",   ctypes.c_uint64),
        ("sample_type",     ctypes.c_uint64),
        ("read_format",     ctypes.c_uint64),
        ("flags",           ctypes.c_uint64),
        ("wakeup_events",   ctypes.c_uint32),
        ("bp_type",         ctypes.c_uint32),
        ("config1",         ctypes.c_uint64),
        ("config2",         ctypes.c_uint64),
        ("branch_sample_type", ctypes.c_uint64),
        ("sample_regs_user", ctypes.c_uint64),
        ("sample_stack_user", ctypes.c_uint32),
        ("clockid",         ctypes.c_int32),
        ("sample_regs_intr", ctypes.c_uint64),
        ("aux_watermark",   ctypes.c_uint32),
        ("sample_max_stack", ctypes.c_uint16),
        ("__reserved_2",    ctypes.c_uint16),
        ("aux_sample_size", ctypes.c_uint32),
        ("__reserved_3",    ctypes.c_uint32),
        ("sig_data",        ctypes.c_uint64),
    ]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _perf_event_open(type_id, config, group_fd=-1, cpu=0):
    """Raw perf_event_open syscall. Returns fd or raises OSError."""
    attr = PerfEventAttr()
    attr.size = ctypes.sizeof(attr)
    attr.type = type_id
    attr.config = config
    attr.read_format = (PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_GROUP)

    # use_clockid is bit 25 in the perf_event_attr flags bitfield
    attr.flags = (1 << 25)  # use_clockid
    attr.clockid = CLOCK_MONOTONIC

    if group_fd >= 0:
        # When adding to an existing group, don't request GROUP format
        attr.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED

    fd = _libc.syscall(ctypes.c_long(__NR_perf_event_open),
                       ctypes.byref(attr),
                       ctypes.c_int(-1),    # pid = -1 (all processes)
                       ctypes.c_int(cpu),
                       ctypes.c_int(group_fd),
                       ctypes.c_ulong(0))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err),
                      f"perf_event_open(type={type_id}, config=0x{config:x})")
    return fd


def perf_open_group(type_id, config, group_fd):
    """Open a PMU counter, optionally grouped. Tries multiple CPUs on EINVAL."""
    nr_cpus = os.cpu_count() or 1
    last_err = None
    for cpu in range(nr_cpus):
        try:
            return _perf_event_open(type_id, config, group_fd, cpu)
        except OSError as e:
            last_err = e
            if e.errno != 22:  # EINVAL
                raise
    raise last_err


def pmu_read_multi(fd, num_counters):
    """Read grouped PMU counters.

    With PERF_FORMAT_GROUP | PERF_FORMAT_TOTAL_TIME_ENABLED the kernel returns:
        struct { u64 nr; u64 time_enabled; u64 values[nr]; }
    """
    buf_size = (2 + num_counters) * 8
    data = os.read(fd, buf_size)
    n_u64 = len(data) // 8
    values = struct.unpack(f"{n_u64}Q", data)
    if n_u64 < 2:
        vprint(f"  pmu_read_multi: got only {len(data)} bytes, expected {buf_size}")
        return 0, []
    nr = values[0]
    time_enabled = values[1]
    counter_vals = list(values[2:])
    if nr != num_counters:
        vprint(f"  pmu_read_multi: nr={nr}, expected={num_counters}, "
               f"got {len(data)} bytes ({n_u64} u64s)")
    return time_enabled, counter_vals

# ---------------------------------------------------------------------------
# sysfs helpers
# ---------------------------------------------------------------------------

SYSFS_EVENT_SOURCE = "/sys/bus/event_source/devices"


def sysfs_read_int(path):
    """Read an integer from a sysfs file."""
    try:
        return int(Path(path).read_text().strip(), 0)
    except (OSError, ValueError):
        return None


def sysfs_read_float(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def sysfs_read_str(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def get_pmu_type(device):
    """Get PMU type id from /sys/bus/event_source/devices/<device>/type."""
    val = sysfs_read_int(f"{SYSFS_EVENT_SOURCE}/{device}/type")
    if val is None or val == 0:
        raise RuntimeError(f"Cannot read PMU type for device '{device}'")
    return val


def get_event_config(device, event_name):
    """Parse event config from sysfs.

    Format varies by PMU driver:
      - i915/xe: "config=0x<hex>"
      - RAPL:    "event=0x<hex>"
    """
    path = f"{SYSFS_EVENT_SOURCE}/{device}/events/{event_name}"
    text = sysfs_read_str(path)
    if not text:
        return None
    # Handle both "config=0xHEX" and "event=0xHEX"
    if "=0x" in text:
        return int(text.split("=0x")[1], 16)
    if "=" in text:
        try:
            return int(text.split("=")[1], 0)
        except ValueError:
            pass
    return None


def get_event_scale(device, event_name):
    path = f"{SYSFS_EVENT_SOURCE}/{device}/events/{event_name}.scale"
    return sysfs_read_float(path)


def get_event_unit(device, event_name):
    path = f"{SYSFS_EVENT_SOURCE}/{device}/events/{event_name}.unit"
    return sysfs_read_str(path)


def get_format_shift(device, param):
    """Parse format bit shift: config:<start>-<end>."""
    path = f"{SYSFS_EVENT_SOURCE}/{device}/format/{param}"
    text = sysfs_read_str(path)
    if not text:
        return None
    # format: "config:<start>-<end>"
    parts = text.split(":")
    if len(parts) != 2:
        return None
    bits = parts[1].split("-")
    return int(bits[0])

# ---------------------------------------------------------------------------
# Driver detection
# ---------------------------------------------------------------------------

def _card_to_gpu_label(card_name):
    """Map a DRM 'cardN' name to the public 'GPU.N' label via the paired
    renderD node (N = renderD_number - 128).  Matching is by shared PCI
    device.  Falls back to the input name when no render node is found.
    Duplicated (intentionally) with monitor.metrics.gpu_info.card_to_gpu_label
    so that gpu_monitor.py remains runnable as a standalone script
    (`sudo python3 gpu_monitor.py`) without package imports.
    """
    try:
        card_pci = os.path.realpath(f"/sys/class/drm/{card_name}/device")
    except OSError:
        return card_name
    try:
        entries = os.listdir("/sys/class/drm")
    except OSError:
        return card_name
    for name in entries:
        if not name.startswith("renderD"):
            continue
        try:
            render_pci = os.path.realpath(f"/sys/class/drm/{name}/device")
        except OSError:
            continue
        if render_pci == card_pci:
            try:
                return f"GPU.{int(name[len('renderD'):]) - 128}"
            except ValueError:
                return card_name
    return card_name


def detect_gpu_devices():
    """Detect Intel GPU devices and their drivers.

    Returns list of dicts: {card, gpu_label, driver, pci_slot, device_name, is_integrated}

    The 'card' field is the kernel DRM node name (e.g. 'card0') used for sysfs
    path construction.  The 'gpu_label' field is the user-facing name (e.g.
    'GPU.0') that matches OpenCL / Level Zero enumeration order; consumers
    building user-facing output should use gpu_label.
    """
    devices = []
    drm_dir = Path("/sys/class/drm")
    if not drm_dir.exists():
        return devices

    for card_dir in sorted(drm_dir.glob("card[0-9]*")):
        driver_link = card_dir / "device" / "driver"
        if not driver_link.is_symlink():
            continue
        driver = os.path.basename(os.readlink(str(driver_link)))
        if driver not in ("i915", "xe"):
            continue

        pci_slot = None
        uevent = card_dir / "device" / "uevent"
        if uevent.exists():
            for line in uevent.read_text().splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    pci_slot = line.split("=", 1)[1]

        card_name = card_dir.name  # e.g. "card0"

        # Build PMU device name
        if driver == "i915":
            if pci_slot and pci_slot != "0000:00:02.0":
                device_name = "i915_" + pci_slot.replace(":", "_")
            else:
                device_name = "i915"
        else:  # xe
            if pci_slot:
                device_name = "xe_" + pci_slot.replace(":", "_")
            else:
                device_name = "xe"

        # Verify PMU device exists in sysfs
        pmu_path = Path(SYSFS_EVENT_SOURCE) / device_name
        if not pmu_path.exists():
            # Try fallback without PCI slot
            fallback = driver
            if (Path(SYSFS_EVENT_SOURCE) / fallback).exists():
                device_name = fallback
            else:
                # Xe can work without PMU (fdinfo + sysfs mode)
                if driver != "xe":
                    continue

        # Intel iGPU is always at bus 00, device 02 (e.g. 0000:00:02.0)
        is_integrated = False
        if pci_slot:
            segments = pci_slot.split(":")
            if len(segments) >= 3:
                is_integrated = (segments[-2] == "00" and
                                 segments[-1].startswith("02."))

        devices.append({
            "card": card_name,                        # sysfs dir name, e.g. "card0"
            "gpu_label": _card_to_gpu_label(card_name),  # public label, e.g. "GPU.0"
            "driver": driver,
            "pci_slot": pci_slot or "unknown",
            "device_name": device_name,
            "is_integrated": is_integrated,
        })

    return devices

# ---------------------------------------------------------------------------
# sysfs frequency / RC6 reading (no PMU needed)
# ---------------------------------------------------------------------------

def read_sysfs_freq(card_name):
    """Read GPU frequency info from sysfs. Works for both i915 and Xe."""
    base = Path(f"/sys/class/drm/{card_name}")
    freq = {}

    # Xe driver: device/tile*/gt*/freq0/{cur_freq,act_freq,min_freq,max_freq}
    for freq0_path in sorted(base.glob("device/tile*/gt*/freq0")):
        gt_name = freq0_path.parent.name   # "gt0", "gt1", ...
        cur = sysfs_read_int(str(freq0_path / "cur_freq"))
        act = sysfs_read_int(str(freq0_path / "act_freq"))
        min_f = sysfs_read_int(str(freq0_path / "min_freq"))
        max_f = sysfs_read_int(str(freq0_path / "max_freq"))
        if cur is not None:
            freq[gt_name] = {
                "cur_mhz": cur,
                "act_mhz": act,
                "min_mhz": min_f,
                "max_mhz": max_f,
            }

    if freq:
        return freq

    # i915 multi-GT path: card0/gt/gt0/rps_*_freq_mhz
    gt_dir = base / "gt"
    if gt_dir.exists():
        for gt_path in sorted(gt_dir.glob("gt*")):
            gt_name = gt_path.name
            cur = sysfs_read_int(str(gt_path / "rps_cur_freq_mhz"))
            act = sysfs_read_int(str(gt_path / "rps_act_freq_mhz"))
            min_f = sysfs_read_int(str(gt_path / "rps_min_freq_mhz"))
            max_f = sysfs_read_int(str(gt_path / "rps_max_freq_mhz"))
            if cur is not None:
                freq[gt_name] = {
                    "cur_mhz": cur,
                    "act_mhz": act,
                    "min_mhz": min_f,
                    "max_mhz": max_f,
                }
    else:
        # Legacy single-GT path
        for attr_prefix in ("gt_", "rps_"):
            cur = sysfs_read_int(str(base / f"{attr_prefix}cur_freq_mhz"))
            if cur is not None:
                freq["gt0"] = {
                    "cur_mhz": cur,
                    "act_mhz": sysfs_read_int(str(base / f"{attr_prefix}act_freq_mhz")),
                    "min_mhz": sysfs_read_int(str(base / f"{attr_prefix}min_freq_mhz")),
                    "max_mhz": sysfs_read_int(str(base / f"{attr_prefix}max_freq_mhz")),
                }
                break

    return freq


def read_sysfs_rc6(card_name):
    """Read RC6/idle residency from sysfs (i915 and Xe).

    Returns cumulative idle residency in milliseconds, or None.
    """
    base = Path(f"/sys/class/drm/{card_name}")

    # Xe driver: device/tile*/gt*/gtidle/idle_residency_ms
    for idle_path in sorted(base.glob("device/tile*/gt*/gtidle/idle_residency_ms")):
        val = sysfs_read_int(str(idle_path))
        if val is not None:
            return val

    # i915: gt/gt0/rc6_residency_ms
    gt_dir = base / "gt"
    if gt_dir.exists():
        for gt_path in sorted(gt_dir.glob("gt*")):
            val = sysfs_read_int(str(gt_path / "rc6_residency_ms"))
            if val is not None:
                return val

    # i915 legacy path
    for p in ("power/rc6_residency_ms", "gt/rc6_residency_ms"):
        val = sysfs_read_int(str(base / p))
        if val is not None:
            return val

    return None


def read_hwmon_power(card_name):
    """Read power from hwmon (discrete GPUs).

    Returns dict with energy values in microjoules, keyed by label:
      - gpu_uj:  GPU/package power  (label "pkg" or empty)
      - card_uj: Card/board power   (label "card")
    Uses label files to determine which energy channel is which.
    """
    base = Path(f"/sys/class/drm/{card_name}/device/hwmon")
    if not base.exists():
        return None

    for hwmon_dir in base.iterdir():
        energy_vals = []
        for energy_input in sorted(hwmon_dir.glob("energy*_input")):
            val = sysfs_read_int(str(energy_input))
            if val is not None:
                energy_vals.append(val)

        if not energy_vals:
            continue

        if len(energy_vals) == 1:
            return {"gpu_uj": energy_vals[0]}

        # The larger value is total board power (GPU + VRAM + VRM etc.),
        # the smaller is GPU die power. This is hardware-invariant,
        # unlike the label semantics which vary across generations.
        gpu_uj = min(energy_vals)
        card_uj = max(energy_vals)
        return {"gpu_uj": gpu_uj, "card_uj": card_uj}

    return None

# ---------------------------------------------------------------------------
# DRM fdinfo engine utilization (no PMU needed)
# ---------------------------------------------------------------------------

DRM_MAJOR = 226  # Linux DRM device major number

# fdinfo engine short name -> display name
FDINFO_ENGINE_NAMES = {
    "rcs": "Render/3D",
    "bcs": "Blitter",
    "vcs": "Video",
    "vecs": "VideoEnhance",
    "ccs": "Compute",
}


def _fdinfo_display_name(eng_name):
    """Convert fdinfo engine name to display name: ccs0 -> Compute/0, ccs -> Compute."""
    for prefix, display in FDINFO_ENGINE_NAMES.items():
        if eng_name.startswith(prefix):
            inst = eng_name[len(prefix):]
            return f"{display}/{inst}" if inst else display
    return eng_name


def scan_drm_fdinfo_clients(pci_slot):
    """Scan /proc fdinfo for DRM clients of a device, grouped per client.

    Returns {(drm_minor, client_id): {eng_name: {cycles, total_cycles,
    time_ns, capacity}}}.  Each (minor, client_id) tuple is unique per DRM
    client; the same client may be shared across pids via fd duplication and
    must only be counted once, so repeats are deduplicated by that key.

    Matches qmassa's aggregation: utilization is computed per-client and
    summed across clients upstream, which is more accurate than summing raw
    cycles from all clients and dividing by max(total_cycles).
    """
    clients = {}

    def _ensure_eng(client_engs, name):
        if name not in client_engs:
            client_engs[name] = {"cycles": 0, "total_cycles": 0,
                                 "time_ns": 0, "capacity": 1}

    proc = Path("/proc")
    for pid_entry in proc.iterdir():
        if not pid_entry.name.isdigit():
            continue

        fd_dir = pid_entry / "fd"
        fdinfo_dir = pid_entry / "fdinfo"

        try:
            fd_entries = list(fd_dir.iterdir())
        except (OSError, PermissionError):
            continue

        for fd_path in fd_entries:
            try:
                st = fd_path.stat()
            except (OSError, PermissionError):
                continue

            if not stat.S_ISCHR(st.st_mode):
                continue
            if os.major(st.st_rdev) != DRM_MAJOR:
                continue
            drm_minor = os.minor(st.st_rdev)

            fdinfo_path = fdinfo_dir / fd_path.name
            try:
                content = fdinfo_path.read_text()
            except (OSError, PermissionError):
                continue

            pdev_match = False
            client_id = None
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("drm-pdev:"):
                    if line.split(":", 1)[1].strip() == pci_slot:
                        pdev_match = True
                elif line.startswith("drm-client-id:"):
                    try:
                        client_id = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        client_id = None
                if pdev_match and client_id is not None:
                    break

            if not pdev_match or client_id is None:
                continue

            ckey = (drm_minor, client_id)
            if ckey in clients:
                # Same DRM client shared across pids — skip duplicate fdinfo
                continue
            client_engs = {}

            for line in content.splitlines():
                line = line.strip()
                parts = line.split(":", 1)
                if len(parts) < 2:
                    continue
                key, val = parts[0].strip(), parts[1].strip()

                if key.startswith("drm-engine-capacity-"):
                    eng = key[len("drm-engine-capacity-"):]
                    _ensure_eng(client_engs, eng)
                    try:
                        client_engs[eng]["capacity"] = int(val)
                    except ValueError:
                        pass

                elif key.startswith("drm-engine-"):
                    eng = key[len("drm-engine-"):]
                    _ensure_eng(client_engs, eng)
                    try:
                        client_engs[eng]["time_ns"] = int(val.split()[0])
                    except ValueError:
                        pass

                elif key.startswith("drm-total-cycles-"):
                    eng = key[len("drm-total-cycles-"):]
                    _ensure_eng(client_engs, eng)
                    try:
                        client_engs[eng]["total_cycles"] = int(val)
                    except ValueError:
                        pass

                elif key.startswith("drm-cycles-"):
                    eng = key[len("drm-cycles-"):]
                    _ensure_eng(client_engs, eng)
                    try:
                        client_engs[eng]["cycles"] = int(val)
                    except ValueError:
                        pass

            if client_engs:
                clients[ckey] = client_engs

    return clients


# ---------------------------------------------------------------------------
# RAPL power (i915 integrated)
# ---------------------------------------------------------------------------

def rapl_parse(domain):
    """Parse RAPL PMU event. Returns (type, config, scale) or None."""
    rapl_path = "/sys/devices/power"
    type_id = sysfs_read_int(f"{rapl_path}/type")
    if type_id is None:
        return None

    config = get_event_config("power", domain)
    if config is None:
        return None

    scale = get_event_scale("power", domain)
    if scale is None:
        scale = 1.0

    unit = get_event_unit("power", domain)
    return type_id, config, scale, unit

# ---------------------------------------------------------------------------
# MSR-based RAPL (fallback when perf_event energy-gpu is missing)
# ---------------------------------------------------------------------------

# Intel SDM Vol.4 – stable since Sandy Bridge (2011)
MSR_RAPL_POWER_UNIT   = 0x00000606
MSR_PKG_ENERGY_STATUS = 0x00000611   # "energy-pkg"
MSR_PP1_ENERGY_STATUS = 0x00000641   # "energy-gpu" (client platforms only)


class RaplMsr:
    """Read RAPL energy counters directly from MSR registers.

    Requires root and the 'msr' kernel module (modprobe msr).
    """

    def __init__(self):
        if os.geteuid() != 0:
            raise RuntimeError("MSR access requires root")

        msr_path = "/dev/cpu/0/msr"
        if not os.path.exists(msr_path):
            # Try to load the msr module automatically
            import subprocess
            ret = subprocess.run(["modprobe", "msr"],
                                 capture_output=True, timeout=5)
            if ret.returncode != 0 or not os.path.exists(msr_path):
                raise RuntimeError(f"MSR device not found: {msr_path} "
                                   "(modprobe msr failed)")

        self.fd = os.open(msr_path, os.O_RDONLY)

        # Read power-unit register to get energy scale
        pu = self._read(MSR_RAPL_POWER_UNIT)
        energy_unit_bits = (pu >> 8) & 0x1F
        self.scale = 1.0 / (1 << energy_unit_bits)   # Joules per raw unit

        # Verify GPU and PKG MSRs are readable
        self._read(MSR_PP1_ENERGY_STATUS)
        self._read(MSR_PKG_ENERGY_STATUS)

        vprint(f"  RaplMsr: energy_unit_bits={energy_unit_bits}, "
               f"scale={self.scale}")

    def _read(self, offset):
        data = os.pread(self.fd, 8, offset)
        if len(data) != 8:
            raise RuntimeError(f"MSR read failed at 0x{offset:x}")
        return struct.unpack("Q", data)[0]

    def read_energy(self):
        """Returns (gpu_raw_32bit, pkg_raw_32bit)."""
        gpu = self._read(MSR_PP1_ENERGY_STATUS) & 0xFFFFFFFF
        pkg = self._read(MSR_PKG_ENERGY_STATUS) & 0xFFFFFFFF
        return gpu, pkg

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class IGpuPower:
    """iGPU power reading: RAPL perf_event with MSR fallback.

    Tries perf_event 'energy-gpu' first; if not available, falls back to
    reading MSR_PP1_ENERGY_STATUS directly.
    """

    def __init__(self):
        self.method = None        # "perf" or "msr"
        self.rapl_fd = -1
        self.rapl_counters = {}
        self.rapl_num = 0
        self.msr = None

        # Try RAPL perf_event first
        self._try_rapl_perf()

        # If energy-gpu not available via perf, try MSR
        if "energy-gpu" not in self.rapl_counters:
            self._cleanup_rapl()
            self._try_msr()

    def _try_rapl_perf(self):
        for domain in ("energy-gpu", "energy-pkg"):
            parsed = rapl_parse(domain)
            if parsed is None:
                continue
            rapl_type, rapl_config, rapl_scale, rapl_unit = parsed
            try:
                fd = perf_open_group(rapl_type, rapl_config, self.rapl_fd)
                if self.rapl_fd == -1:
                    self.rapl_fd = fd
                self.rapl_counters[domain] = {
                    "idx": self.rapl_num,
                    "scale": rapl_scale,
                    "unit": rapl_unit,
                }
                self.rapl_num += 1
            except OSError:
                pass

        if self.rapl_counters and "energy-gpu" in self.rapl_counters:
            self.method = "perf"
            vprint(f"  IGpuPower: using RAPL perf_event, "
                   f"domains={list(self.rapl_counters.keys())}")

    def _cleanup_rapl(self):
        if self.rapl_fd >= 0:
            os.close(self.rapl_fd)
            self.rapl_fd = -1
        self.rapl_counters.clear()
        self.rapl_num = 0
        self.method = None

    def _try_msr(self):
        try:
            self.msr = RaplMsr()
            self.method = "msr"
            vprint("  IGpuPower: using MSR fallback")
        except (OSError, RuntimeError) as e:
            vprint(f"  IGpuPower: MSR fallback failed: {e}")

    @property
    def available(self):
        return self.method is not None

    def sample(self):
        """Returns dict to merge into the main sample."""
        if self.method == "perf":
            _, vals = pmu_read_multi(self.rapl_fd, self.rapl_num)
            return {"rapl_vals": vals}
        elif self.method == "msr":
            gpu, pkg = self.msr.read_energy()
            return {"msr_gpu": gpu, "msr_pkg": pkg}
        return {}

    def compute_power(self, prev_sample, cur_sample, dt_s):
        """Compute power (watts) from two consecutive samples."""
        power = {}

        if self.method == "perf":
            prev_vals = prev_sample.get("rapl_vals")
            cur_vals = cur_sample.get("rapl_vals")
            if cur_vals and prev_vals:
                for domain, info in self.rapl_counters.items():
                    idx = info["idx"]
                    if idx < len(cur_vals) and idx < len(prev_vals):
                        delta = cur_vals[idx] - prev_vals[idx]
                        watts = (delta * info["scale"]) / dt_s
                        label = "gpu_w" if "gpu" in domain else "pkg_w"
                        power[label] = round(watts, 2)

        elif self.method == "msr":
            if "msr_gpu" in cur_sample and "msr_gpu" in prev_sample:
                # 32-bit counter wraparound
                gpu_delta = (cur_sample["msr_gpu"] - prev_sample["msr_gpu"]) & 0xFFFFFFFF
                pkg_delta = (cur_sample["msr_pkg"] - prev_sample["msr_pkg"]) & 0xFFFFFFFF
                power["gpu_w"] = round((gpu_delta * self.msr.scale) / dt_s, 2)
                power["pkg_w"] = round((pkg_delta * self.msr.scale) / dt_s, 2)

        return power

    def close(self):
        if self.rapl_fd >= 0:
            os.close(self.rapl_fd)
            self.rapl_fd = -1
        if self.msr:
            self.msr.close()
            self.msr = None

# ---------------------------------------------------------------------------
# i915 Backend
# ---------------------------------------------------------------------------

def _i915_pmu_other(gt, x):
    return _I915_PMU_OTHER_0 + x | (gt << _I915_PMU_GT_SHIFT)


class I915Monitor:
    """System-wide GPU monitor for i915 driver."""

    def __init__(self, device_info):
        self.device_name = device_info["device_name"]
        self.card = device_info["card"]                                  # sysfs dir: "card0"
        self.gpu_label = device_info.get("gpu_label", self.card)         # public: "GPU.0"
        self.pci_slot = device_info["pci_slot"]
        self.type_id = get_pmu_type(self.device_name)
        self.is_discrete = not device_info.get("is_integrated", False)

        self.group_fd = -1
        self.counter_idx = 0
        self.engines = []
        self.gt_counters = []  # per-GT freq/rc6
        self.igpu_power = None

        self._discover_engines()
        self._init_pmu()

        # Cache static min/max frequency from sysfs (read once, avoid glob during sampling)
        self._freq_range = {}
        for gt_name, f in read_sysfs_freq(self.card).items():
            self._freq_range[gt_name] = {
                "min_mhz": f.get("min_mhz"),
                "max_mhz": f.get("max_mhz"),
            }

        # Cache RC6 sysfs paths for GTs not covered by PMU.
        # _detect_num_gts() uses PMU interrupt counters which may fail for
        # GT1 even though it exists in sysfs.  For those GTs we read
        # rc6_residency_ms from sysfs and compute RC6 % from delta.
        self._sysfs_rc6_paths = {}
        pmu_gt_names = {f"gt{i}" for i in range(len(self.gt_counters))}
        gt_dir = Path(f"/sys/class/drm/{self.card}/gt")
        for gt_name in self._freq_range:
            if gt_name not in pmu_gt_names:
                rc6_path = gt_dir / gt_name / "rc6_residency_ms"
                if rc6_path.exists():
                    self._sysfs_rc6_paths[gt_name] = str(rc6_path)

    def _discover_engines(self):
        """Enumerate engines from sysfs events directory."""
        events_dir = Path(SYSFS_EVENT_SOURCE) / self.device_name / "events"
        if not events_dir.exists():
            return

        engines = []
        for entry in events_dir.iterdir():
            name = entry.name
            if not name.endswith("-busy"):
                continue
            eng_name = name[:-5]  # strip "-busy"
            if len(eng_name) < 2:
                continue

            busy_config = get_event_config(self.device_name, f"{eng_name}-busy")
            if busy_config is None or busy_config >= _I915_PMU_OTHER_0:
                continue

            eng_class = (busy_config & (_I915_PMU_OTHER_0 - 1)) >> I915_PMU_CLASS_SHIFT
            eng_instance = (busy_config >> I915_PMU_SAMPLE_BITS) & (
                (1 << I915_PMU_SAMPLE_INSTANCE_BITS) - 1)

            wait_config = get_event_config(self.device_name, f"{eng_name}-wait")
            sema_config = get_event_config(self.device_name, f"{eng_name}-sema")

            class_name = I915_ENGINE_CLASSES.get(eng_class, f"class{eng_class}")
            display_name = f"{class_name}/{eng_instance}"

            engines.append({
                "name": eng_name,
                "display_name": display_name,
                "class": eng_class,
                "instance": eng_instance,
                "busy_config": busy_config,
                "wait_config": wait_config,
                "sema_config": sema_config,
            })

        engines.sort(key=lambda e: (e["class"], e["instance"]))
        self.engines = engines
        vprint(f"  [{self.device_name}] Discovered {len(engines)} engines: "
               f"{[e['display_name'] for e in engines]}")

    def _open_counter(self, config):
        """Open a PMU counter in the group. Returns index or -1."""
        try:
            fd = perf_open_group(self.type_id, config, self.group_fd)
            if self.group_fd == -1:
                self.group_fd = fd
            idx = self.counter_idx
            self.counter_idx += 1
            vprint(f"    counter config=0x{config:x} -> idx={idx} fd={fd}")
            return idx
        except OSError as e:
            vprint(f"    counter config=0x{config:x} -> FAILED: {e}")
            return -1

    def _detect_num_gts(self):
        for cnt in range(4):
            config = _i915_pmu_other(cnt, _I915_PMU_INTERRUPTS)
            try:
                fd = perf_open_group(self.type_id, config, -1)
                os.close(fd)
            except OSError:
                return max(cnt, 1)
        return 4

    def _init_pmu(self):
        num_gts = self._detect_num_gts()

        # IRQ counter (first, becomes group leader)
        irq_config = _i915_pmu_other(0, _I915_PMU_INTERRUPTS)
        self.irq_idx = self._open_counter(irq_config)

        # Per-GT frequency and RC6
        for gt in range(num_gts):
            gt_data = {}
            gt_data["freq_req_idx"] = self._open_counter(
                _i915_pmu_other(gt, _I915_PMU_FREQ_REQ))
            gt_data["freq_act_idx"] = self._open_counter(
                _i915_pmu_other(gt, _I915_PMU_FREQ_ACT))
            gt_data["rc6_idx"] = self._open_counter(
                _i915_pmu_other(gt, _I915_PMU_RC6))
            self.gt_counters.append(gt_data)

        # Engine counters
        for eng in self.engines:
            eng["busy_idx"] = self._open_counter(eng["busy_config"])
            if eng["wait_config"] is not None:
                eng["wait_idx"] = self._open_counter(eng["wait_config"])
            else:
                eng["wait_idx"] = -1
            if eng["sema_config"] is not None:
                eng["sema_idx"] = self._open_counter(eng["sema_config"])
            else:
                eng["sema_idx"] = -1

        # iGPU power (RAPL perf_event with MSR fallback)
        if not self.is_discrete:
            self.igpu_power = IGpuPower()

        vprint(f"  [{self.device_name}] PMU init done: {self.counter_idx} counters in group, "
               f"{len(self.engines)} engines, {len(self.gt_counters)} GTs, "
               f"igpu_power={self.igpu_power.method if self.igpu_power else 'none'}")

    def sample(self):
        """Take one PMU sample. Returns raw (timestamp, values)."""
        ts, vals = pmu_read_multi(self.group_fd, self.counter_idx)

        result = {
            "ts": ts,
            "vals": vals,
        }

        # RC6 from sysfs for GTs not covered by PMU
        if self._sysfs_rc6_paths:
            idle = {}
            for gt_name, path in self._sysfs_rc6_paths.items():
                val = sysfs_read_int(path)
                if val is not None:
                    idle[gt_name] = val
            result["idle_ms"] = idle

        # iGPU power (RAPL or MSR)
        if self.igpu_power and self.igpu_power.available:
            result.update(self.igpu_power.sample())

        # hwmon energy for discrete GPU
        if self.is_discrete:
            result["hwmon"] = read_hwmon_power(self.card)

        return result

    def compute(self, prev, cur):
        """Compute metrics from two consecutive samples."""
        dt_ns = cur["ts"] - prev["ts"]
        if dt_ns <= 0:
            return None

        dt_s = dt_ns / 1e9
        result = {
            "driver": "i915",
            "card": self.gpu_label,
            "pci_slot": self.pci_slot,
            "period_ms": dt_ns / 1e6,
            "is_integrated": not self.is_discrete,
        }

        # Per-GT frequency (PMU averaged) and RC6
        # PMU frequency unit is MHz*s; PMU RC6 unit is ns
        frequency = {}

        for gt_idx, gt_data in enumerate(self.gt_counters):
            gt_name = f"gt{gt_idx}"
            gt_freq = {}

            idx = gt_data["freq_req_idx"]
            if idx >= 0 and idx < len(cur["vals"]) and idx < len(prev["vals"]):
                delta = cur["vals"][idx] - prev["vals"][idx]
                gt_freq["req_mhz"] = round(delta / dt_s, 0)

            idx = gt_data["freq_act_idx"]
            if idx >= 0 and idx < len(cur["vals"]) and idx < len(prev["vals"]):
                delta = cur["vals"][idx] - prev["vals"][idx]
                gt_freq["act_mhz"] = round(delta / dt_s, 0)

            sf = self._freq_range.get(gt_name, {})
            if sf.get("min_mhz") is not None:
                gt_freq["min_mhz"] = sf["min_mhz"]
            if sf.get("max_mhz") is not None:
                gt_freq["max_mhz"] = sf["max_mhz"]

            idx = gt_data["rc6_idx"]
            if idx >= 0 and idx < len(cur["vals"]) and idx < len(prev["vals"]):
                delta = cur["vals"][idx] - prev["vals"][idx]
                rc6_pct = min(max((delta / dt_ns) * 100, 0.0), 100.0)
                gt_freq["rc6_pct"] = round(rc6_pct, 1)

            if gt_freq:
                frequency[gt_name] = gt_freq

        # Sysfs fallback: _detect_num_gts() uses PMU interrupt counters which
        # may fail for GT1 even though it exists in sysfs.  For any GT present
        # in _freq_range but missing from the PMU loop above, read the
        # instantaneous frequency from sysfs instead.
        missing_gts = [gt for gt in self._freq_range if gt not in frequency]
        if missing_gts:
            sysfs_snap = read_sysfs_freq(self.card)
            for gt_name in missing_gts:
                gt_sysfs = sysfs_snap.get(gt_name)
                if not gt_sysfs:
                    continue
                gt_freq = {}
                if gt_sysfs.get("cur_mhz") is not None:
                    gt_freq["req_mhz"] = gt_sysfs["cur_mhz"]
                if gt_sysfs.get("act_mhz") is not None:
                    gt_freq["act_mhz"] = gt_sysfs["act_mhz"]
                sf = self._freq_range.get(gt_name, {})
                if sf.get("min_mhz") is not None:
                    gt_freq["min_mhz"] = sf["min_mhz"]
                if sf.get("max_mhz") is not None:
                    gt_freq["max_mhz"] = sf["max_mhz"]
                if gt_freq:
                    frequency[gt_name] = gt_freq

        # RC6 from sysfs for GTs not covered by PMU (same delta pattern as XeMonitor)
        cur_idle = cur.get("idle_ms", {})
        prev_idle = prev.get("idle_ms", {})
        if cur_idle and prev_idle:
            for gt_name in cur_idle:
                if gt_name in prev_idle:
                    delta_ms = cur_idle[gt_name] - prev_idle[gt_name]
                    dt_ms = dt_s * 1000.0
                    if dt_ms > 0:
                        rc6_pct = min(max(delta_ms / dt_ms * 100.0, 0.0), 100.0)
                    else:
                        rc6_pct = 0.0
                    if gt_name not in frequency:
                        frequency[gt_name] = {}
                    frequency[gt_name]["rc6_pct"] = round(rc6_pct, 1)

        if frequency:
            result["frequency"] = frequency

        # Engine utilization
        engines_result = {}
        for eng in self.engines:
            idx = eng["busy_idx"]
            if idx < 0 or idx >= len(cur["vals"]):
                continue
            busy_delta = cur["vals"][idx] - prev["vals"][idx]
            busy_pct = min((busy_delta / dt_ns) * 100, 100.0)

            eng_data = {"busy_pct": round(busy_pct, 2)}

            idx = eng["wait_idx"]
            if idx >= 0 and idx < len(cur["vals"]):
                wait_delta = cur["vals"][idx] - prev["vals"][idx]
                eng_data["wait_pct"] = round(min((wait_delta / dt_ns) * 100, 100.0), 2)

            idx = eng["sema_idx"]
            if idx >= 0 and idx < len(cur["vals"]):
                sema_delta = cur["vals"][idx] - prev["vals"][idx]
                eng_data["sema_pct"] = round(min((sema_delta / dt_ns) * 100, 100.0), 2)

            engines_result[eng["display_name"]] = eng_data

        result["engines"] = engines_result

        # Power: RAPL/MSR for iGPU, hwmon for dGPU
        power = {}
        if self.igpu_power and self.igpu_power.available:
            power = self.igpu_power.compute_power(prev, cur, dt_s)

        if (self.is_discrete and
                cur.get("hwmon") is not None and
                prev.get("hwmon") is not None):
            for key, label in (("gpu_uj", "gpu_w"), ("card_uj", "card_w")):
                v_cur = cur["hwmon"].get(key)
                v_prev = prev["hwmon"].get(key)
                if v_cur is not None and v_prev is not None:
                    delta_uj = v_cur - v_prev
                    if delta_uj >= 0:
                        power[label] = round(delta_uj / 1e6 / dt_s, 2)

        if power:
            result["power"] = power

        return result

    def close(self):
        if self.group_fd >= 0:
            os.close(self.group_fd)
            self.group_fd = -1
        if self.igpu_power:
            self.igpu_power.close()
            self.igpu_power = None

# ---------------------------------------------------------------------------
# Xe Backend
# ---------------------------------------------------------------------------

class XeMonitor:
    """System-wide GPU monitor for Xe driver.

    Default (use_pmu=False): DRM fdinfo for engine utilization (per-client
    percentages summed across clients, qmassa-style), sysfs for frequency,
    hwmon for power.  Does NOT open Xe PMU perf_event counters.

    PMU mode (use_pmu=True): per-instance engine utilization and PMU-based
    frequency.  NOTE: opening Xe PMU counters prevents the GT from entering
    deep idle, which significantly inflates power readings.
    """

    def __init__(self, device_info, use_pmu=False):
        self.device_name = device_info["device_name"]
        self.card = device_info["card"]                                  # sysfs dir: "card0"
        self.gpu_label = device_info.get("gpu_label", self.card)         # public: "GPU.0"
        self.pci_slot = device_info["pci_slot"]
        self.is_discrete = not device_info.get("is_integrated", False)
        self.use_pmu = use_pmu

        self.group_fd = -1
        self.counter_idx = 0
        self.engines = []
        self.gt_freq_counters = []
        self.igpu_power = None

        if self.use_pmu:
            self.type_id = get_pmu_type(self.device_name)
            self._discover_engines()
            self._init_pmu()
            vprint(f"  [{self.device_name}] PMU mode: {len(self.engines)} engines, "
                   f"{len(self.gt_freq_counters)} GT freq counters")
        else:
            vprint(f"  [{self.device_name}] fdinfo + sysfs mode (no PMU)")

        # iGPU power (RAPL perf_event with MSR fallback) — independent of Xe PMU
        if not self.is_discrete:
            self.igpu_power = IGpuPower()
            vprint(f"  [{self.device_name}] igpu_power="
                   f"{self.igpu_power.method if self.igpu_power else 'none'}")

        self._cache_init()

    def _cache_init(self):
        """Cache static sysfs data and hwmon paths at init."""
        base = Path(f"/sys/class/drm/{self.card}")

        # Static min/max frequency and sysfs freq paths (read once)
        self._freq_range = {}
        self._freq_paths = {}  # gt_name -> {cur_freq, act_freq} paths
        for freq0 in sorted(base.glob("device/tile*/gt*/freq0")):
            gt_name = freq0.parent.name
            self._freq_range[gt_name] = {
                "min_mhz": sysfs_read_int(str(freq0 / "min_freq")),
                "max_mhz": sysfs_read_int(str(freq0 / "max_freq")),
            }
            self._freq_paths[gt_name] = {
                "cur_freq": str(freq0 / "cur_freq"),
                "act_freq": str(freq0 / "act_freq"),
            }

        # Idle residency paths for RC6 (sysfs-only mode)
        self._idle_paths = {}
        for idle_path in sorted(base.glob("device/tile*/gt*/gtidle/idle_residency_ms")):
            gt_name = idle_path.parent.parent.name
            self._idle_paths[gt_name] = str(idle_path)

        # Hwmon energy paths (hwmon reads don't wake the GT)
        self._energy_paths = []
        hwmon_base = base / "device" / "hwmon"
        if hwmon_base.exists():
            for hwmon_dir in hwmon_base.iterdir():
                for ep in sorted(hwmon_dir.glob("energy*_input")):
                    self._energy_paths.append(str(ep))

        # Discover engine classes and instance counts from sysfs.
        # Xe fdinfo uses class-level keys (e.g. "ccs" not "ccs0"), so we
        # store class prefixes here plus how many instances each class has.
        self._known_engines = []  # class prefixes, e.g. ["rcs", "bcs", "ccs"]
        self._engine_instances = {}  # class prefix -> instance count
        class_map = {"rcs": 0, "bcs": 1, "vcs": 2, "vecs": 3, "ccs": 4}
        for tile_dir in sorted(base.glob("device/tile*/gt*")):
            engines_dir = tile_dir / "engines"
            if not engines_dir.exists():
                continue
            for ent in engines_dir.iterdir():
                name = ent.name
                for prefix in class_map:
                    if name.startswith(prefix):
                        self._engine_instances[prefix] = \
                            self._engine_instances.get(prefix, 0) + 1
                        break
        self._known_engines = sorted(self._engine_instances.keys(),
                                     key=lambda n: class_map.get(n, 99))

        vprint(f"  [{self.device_name}] cached: freq_range={list(self._freq_range.keys())}, "
               f"idle_paths={list(self._idle_paths.keys())}, "
               f"engines={self._known_engines}, "
               f"energy_paths={len(self._energy_paths)}")

    def _discover_engines(self):
        """Discover Xe engines via sysfs PMU events.

        Xe exposes engine-active-ticks and engine-total-ticks as base events,
        with format params gt, engine_class, engine_instance to build config.
        """
        events_dir = Path(SYSFS_EVENT_SOURCE) / self.device_name / "events"
        if not events_dir.exists():
            return

        active_base = get_event_config(self.device_name, "engine-active-ticks")
        total_base = get_event_config(self.device_name, "engine-total-ticks")
        if active_base is None or total_base is None:
            return

        gt_shift = get_format_shift(self.device_name, "gt")
        class_shift = get_format_shift(self.device_name, "engine_class")
        instance_shift = get_format_shift(self.device_name, "engine_instance")
        if None in (gt_shift, class_shift, instance_shift):
            return
        self._gt_shift = gt_shift

        engines = []
        card_path = Path(f"/sys/class/drm/{self.card}")

        # Scan tile/gt directories for engine presence
        found = set()
        for tile_dir in sorted(card_path.glob("device/tile*/gt*")):
            gt_id = int(tile_dir.name.replace("gt", ""))
            engines_dir = tile_dir / "engines"
            if not engines_dir.exists():
                continue
            class_map = {"rcs": 0, "bcs": 1, "vcs": 2, "vecs": 3, "ccs": 4}
            for ent in engines_dir.iterdir():
                name = ent.name
                for prefix, cls in class_map.items():
                    if name.startswith(prefix):
                        try:
                            inst = int(name[len(prefix):])
                        except ValueError:
                            continue
                        key = (gt_id, cls, inst)
                        if key not in found:
                            found.add(key)
                            engines.append({
                                "gt_id": gt_id, "class": cls, "instance": inst,
                            })

        # Fallback: probe by trying class/instance combos via perf_event_open
        if not engines:
            for gt_id in range(4):
                for cls in range(5):
                    for inst in range(4):
                        param = ((gt_id << gt_shift) |
                                 (cls << class_shift) |
                                 (inst << instance_shift))
                        try:
                            fd = perf_open_group(self.type_id,
                                                 active_base | param, -1)
                            os.close(fd)
                            engines.append({
                                "gt_id": gt_id, "class": cls, "instance": inst,
                            })
                        except OSError:
                            continue

        for eng in engines:
            param = ((eng["gt_id"] << gt_shift) |
                     (eng["class"] << class_shift) |
                     (eng["instance"] << instance_shift))
            eng["active_config"] = active_base | param
            eng["total_config"] = total_base | param
            class_name = XE_ENGINE_CLASSES.get(eng["class"],
                                               f"class{eng['class']}")
            eng["display_name"] = (f"GT:{eng['gt_id']} "
                                   f"{class_name}/{eng['instance']}")

        engines.sort(key=lambda e: (e["gt_id"], e["class"], e["instance"]))
        self.engines = engines

    def _open_counter(self, config):
        try:
            fd = perf_open_group(self.type_id, config, self.group_fd)
            if self.group_fd == -1:
                self.group_fd = fd
            idx = self.counter_idx
            self.counter_idx += 1
            return idx
        except OSError:
            return -1

    def _init_pmu(self):
        for eng in self.engines:
            eng["active_idx"] = self._open_counter(eng["active_config"])
            eng["total_idx"] = self._open_counter(eng["total_config"])

        # Per-GT frequency via PMU
        self.gt_freq_counters = []
        freq_req_base = get_event_config(self.device_name,
                                         "gt-requested-frequency")
        freq_act_base = get_event_config(self.device_name,
                                         "gt-actual-frequency")
        if (freq_req_base is not None and freq_act_base is not None and
                hasattr(self, '_gt_shift')):
            gt_ids = sorted(set(e["gt_id"] for e in self.engines))
            for gt_id in gt_ids:
                gt_param = gt_id << self._gt_shift
                req_idx = self._open_counter(freq_req_base | gt_param)
                act_idx = self._open_counter(freq_act_base | gt_param)
                self.gt_freq_counters.append({
                    "gt_id": gt_id,
                    "req_idx": req_idx,
                    "act_idx": act_idx,
                })

    def _read_hwmon_cached(self):
        """Read hwmon energy from cached paths (no glob/directory traversal)."""
        vals = []
        for p in self._energy_paths:
            v = sysfs_read_int(p)
            if v is not None:
                vals.append(v)
        if not vals:
            return None
        if len(vals) == 1:
            return {"gpu_uj": vals[0]}
        return {"gpu_uj": min(vals), "card_uj": max(vals)}

    def sample(self):
        """Take one sample."""
        result = {"ts": time.monotonic_ns(), "vals": []}

        if self.use_pmu and self.group_fd >= 0:
            ts, vals = pmu_read_multi(self.group_fd, self.counter_idx)
            result["ts"] = ts
            result["vals"] = vals

        # fdinfo mode: fdinfo engines, sysfs frequency
        if not self.use_pmu:
            result["fdinfo_clients"] = scan_drm_fdinfo_clients(self.pci_slot)

            freq = {}
            for gt_name, paths in self._freq_paths.items():
                cur = sysfs_read_int(paths["cur_freq"])
                act = sysfs_read_int(paths["act_freq"])
                if cur is not None:
                    freq[gt_name] = {"cur_mhz": cur, "act_mhz": act}
            result["sysfs_freq"] = freq

        # Per-GT idle residency (sysfs, both modes)
        idle = {}
        for gt_name, path in self._idle_paths.items():
            val = sysfs_read_int(path)
            if val is not None:
                idle[gt_name] = val
        result["idle_ms"] = idle

        # iGPU power (RAPL or MSR)
        if self.igpu_power and self.igpu_power.available:
            result.update(self.igpu_power.sample())

        # dGPU power (hwmon reads don't wake the GT)
        if self.is_discrete and self._energy_paths:
            result["hwmon"] = self._read_hwmon_cached()

        return result

    def compute(self, prev, cur):
        dt_ns = cur["ts"] - prev["ts"]
        if dt_ns <= 0:
            return None
        dt_s = dt_ns / 1e9

        result = {
            "driver": "xe",
            "card": self.gpu_label,
            "pci_slot": self.pci_slot,
            "period_ms": dt_ns / 1e6,
            "is_integrated": not self.is_discrete,
        }

        # Engine utilization
        engines_result = {}
        if self.use_pmu:
            # PMU mode: per-instance engine-active-ticks / engine-total-ticks
            for eng in self.engines:
                a_idx = eng["active_idx"]
                t_idx = eng["total_idx"]
                if (a_idx < 0 or t_idx < 0 or
                        a_idx >= len(cur["vals"]) or t_idx >= len(cur["vals"])):
                    continue
                active_delta = cur["vals"][a_idx] - prev["vals"][a_idx]
                total_delta = cur["vals"][t_idx] - prev["vals"][t_idx]
                if total_delta > 0 and 0 <= active_delta <= total_delta:
                    pct = (active_delta / total_delta) * 100.0
                elif total_delta > 0 and active_delta > total_delta:
                    # PMU driver bug: active ticks exceed total ticks
                    vprint("    %s: PMU anomaly active=%d > total=%d, "
                           "reporting 0%%" % (eng["display_name"],
                                              active_delta, total_delta))
                    pct = 0.0
                else:
                    pct = 0.0
                engines_result[eng["display_name"]] = {"busy_pct": round(pct, 2)}
        else:
            # fdinfo mode: per-client utilization summed across clients,
            # matching qmassa's aggregation (drm_clients.rs:eng_utilization +
            # drm_devices.rs fallback loop).  This is more accurate than
            # summing raw cycles across clients because each client's
            # total_cycles may span a different sub-window of [prev, cur],
            # especially for clients that started or ended during the period.
            cur_clis = cur.get("fdinfo_clients", {})
            prev_clis = prev.get("fdinfo_clients", {})

            # Accumulate per-engine utilization across all clients that were
            # present in BOTH samples (need two samples to have a delta).
            eng_totals = {}   # eng_name -> summed % across clients
            eng_caps = {}     # eng_name -> capacity seen (for num_instances)

            for ckey, cur_engs in cur_clis.items():
                prev_engs = prev_clis.get(ckey)
                if prev_engs is None:
                    continue
                for eng_name, cd in cur_engs.items():
                    pd = prev_engs.get(eng_name)
                    if pd is None:
                        continue
                    cap = cd.get("capacity", 1) or 1
                    eng_caps[eng_name] = max(eng_caps.get(eng_name, 1), cap)

                    cli_pct = 0.0
                    # Prefer cycles (Xe fdinfo)
                    dcy = cd.get("cycles", 0) - pd.get("cycles", 0)
                    dtot = cd.get("total_cycles", 0) - pd.get("total_cycles", 0)
                    if dcy < 0:
                        dcy = 0
                    if dtot > 0 and cd.get("total_cycles", 0) > 0:
                        cli_pct = (dcy * 100.0) / (dtot * cap)
                    # Fallback to time (i915 fdinfo): delta_time / dt_ns
                    elif dt_ns > 0:
                        dns = cd.get("time_ns", 0) - pd.get("time_ns", 0)
                        if dns < 0:
                            dns = 0
                        if dns > 0:
                            cli_pct = (dns * 100.0) / (dt_ns * cap)

                    if cli_pct > 100.0:
                        cli_pct = 100.0
                    eng_totals[eng_name] = eng_totals.get(eng_name, 0.0) + cli_pct

            # Ensure all known engines appear, even if idle
            for eng_name in self._known_engines:
                eng_totals.setdefault(eng_name, 0.0)

            for eng_name, pct in eng_totals.items():
                pct = min(max(pct, 0.0), 100.0)
                display = _fdinfo_display_name(eng_name)
                n_inst = self._engine_instances.get(eng_name, 0)
                if n_inst <= 0:
                    n_inst = eng_caps.get(eng_name, 1)
                engines_result[display] = {
                    "busy_pct": round(pct, 2),
                    "num_instances": n_inst,
                }

        result["engines"] = engines_result

        # Frequency
        if self.use_pmu and self.gt_freq_counters:
            # PMU mode: accumulated MHz counters
            frequency = {}
            for gt_data in self.gt_freq_counters:
                gt_name = f"gt{gt_data['gt_id']}"
                gt_freq = {}
                idx = gt_data["req_idx"]
                if idx >= 0 and idx < len(cur["vals"]) and idx < len(prev["vals"]):
                    delta = cur["vals"][idx] - prev["vals"][idx]
                    gt_freq["req_mhz"] = round(delta / dt_s, 0)
                idx = gt_data["act_idx"]
                if idx >= 0 and idx < len(cur["vals"]) and idx < len(prev["vals"]):
                    delta = cur["vals"][idx] - prev["vals"][idx]
                    gt_freq["act_mhz"] = round(delta / dt_s, 0)
                sf = self._freq_range.get(gt_name, {})
                if sf.get("min_mhz") is not None:
                    gt_freq["min_mhz"] = sf["min_mhz"]
                if sf.get("max_mhz") is not None:
                    gt_freq["max_mhz"] = sf["max_mhz"]
                if gt_freq:
                    frequency[gt_name] = gt_freq
            if frequency:
                result["frequency"] = frequency
        else:
            # sysfs mode: instantaneous frequency snapshot
            sysfs_freq = cur.get("sysfs_freq", {})
            if sysfs_freq:
                frequency = {}
                for gt_name, fdata in sysfs_freq.items():
                    gt_freq = {
                        "req_mhz": fdata.get("cur_mhz"),
                        "act_mhz": fdata.get("act_mhz"),
                    }
                    sf = self._freq_range.get(gt_name, {})
                    if sf.get("min_mhz") is not None:
                        gt_freq["min_mhz"] = sf["min_mhz"]
                    if sf.get("max_mhz") is not None:
                        gt_freq["max_mhz"] = sf["max_mhz"]
                    frequency[gt_name] = gt_freq
                if frequency:
                    result["frequency"] = frequency

        # Per-GT RC6 from idle residency (sysfs, both modes)
        cur_idle = cur.get("idle_ms", {})
        prev_idle = prev.get("idle_ms", {})
        if cur_idle and prev_idle:
            if "frequency" not in result:
                result["frequency"] = {}
            for gt_name in cur_idle:
                if gt_name in prev_idle:
                    delta_ms = cur_idle[gt_name] - prev_idle[gt_name]
                    dt_ms = dt_s * 1000.0
                    if dt_ms > 0:
                        rc6_pct = min(max(delta_ms / dt_ms * 100.0, 0.0), 100.0)
                    else:
                        rc6_pct = 0.0
                    if gt_name not in result["frequency"]:
                        result["frequency"][gt_name] = {}
                    result["frequency"][gt_name]["rc6_pct"] = round(rc6_pct, 1)

        # Power: RAPL/MSR for iGPU, hwmon for dGPU
        power = {}
        if self.igpu_power and self.igpu_power.available:
            power = self.igpu_power.compute_power(prev, cur, dt_s)

        if (self.is_discrete and
                cur.get("hwmon") is not None and
                prev.get("hwmon") is not None):
            for key, label in (("gpu_uj", "gpu_w"), ("card_uj", "card_w")):
                v_cur = cur["hwmon"].get(key)
                v_prev = prev["hwmon"].get(key)
                if v_cur is not None and v_prev is not None:
                    delta_uj = v_cur - v_prev
                    if delta_uj >= 0:
                        power[label] = round(delta_uj / 1e6 / dt_s, 2)

        if power:
            result["power"] = power

        return result

    def close(self):
        if self.group_fd >= 0:
            os.close(self.group_fd)
            self.group_fd = -1
        if self.igpu_power:
            self.igpu_power.close()
            self.igpu_power = None

# ---------------------------------------------------------------------------
# Unified monitor
# ---------------------------------------------------------------------------

class GPUMonitor:
    """Unified GPU monitor supporting multiple Intel GPUs (i915 + Xe)."""

    def __init__(self, xe_pmu=False):
        self.monitors = []
        devices = detect_gpu_devices()
        if not devices:
            raise RuntimeError(
                "No Intel GPU found. Check /sys/class/drm/ and driver status.")

        for dev in devices:
            try:
                if dev["driver"] == "i915":
                    self.monitors.append(I915Monitor(dev))
                elif dev["driver"] == "xe":
                    self.monitors.append(XeMonitor(dev, use_pmu=xe_pmu))
            except (OSError, RuntimeError) as e:
                print(f"Warning: Failed to init {dev['driver']} on "
                      f"{dev['card']}: {e}", file=sys.stderr)

        if not self.monitors:
            raise RuntimeError("Failed to initialize any GPU monitor.")

        self._prev_samples = None

    def sample_once(self, interval_s=1.0):
        """Take two samples separated by interval_s, return computed metrics."""
        samples1 = [m.sample() for m in self.monitors]
        time.sleep(interval_s)
        samples2 = [m.sample() for m in self.monitors]

        results = []
        for mon, s1, s2 in zip(self.monitors, samples1, samples2):
            r = mon.compute(s1, s2)
            if r:
                results.append(r)
        return results

    def start_sampling(self):
        """Take initial sample for continuous mode."""
        self._prev_samples = [m.sample() for m in self.monitors]

    def sample_delta(self):
        """Take a sample and compute delta from previous. Call start_sampling() first."""
        cur_samples = [m.sample() for m in self.monitors]
        results = []
        for mon, prev, cur in zip(self.monitors, self._prev_samples, cur_samples):
            r = mon.compute(prev, cur)
            if r:
                results.append(r)
        self._prev_samples = cur_samples
        return results

    def close(self):
        for m in self.monitors:
            m.close()
