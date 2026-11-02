# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# PMT telemetry logic derived from intel/linux-npu-driver (MIT License)
# https://github.com/intel/linux-npu-driver

"""Single entry point for SoC Punit PMT telemetry (/sys/class/intel_pmt/telem*).

Everything that reads PMT goes through this module:
* `PLATFORMS` maps a telem GUID to a `Platform`: the NPU register map plus the
  bandwidth counters, with offsets/units from the Intel-PMT XML for that GUID
  (https://github.com/intel/Intel-PMT, xml/<platform>/). A counter a platform
  lacks is absent, so the matching value is None, never guessed. Adding a
  platform means adding one `Platform` entry here.
* `PmtTelemetry` reads one snapshot of the node and decodes NPU power /
  temperature / frequency / tile config / NoC bandwidth.
* `get_device_bandwidth()` attributes memory bandwidth to CPU / GPU / NPU.

Bandwidth attribution (validated on Wildcat Lake against the IMC system total,
within ~3%):
* cpu = SANTA (CCF) + IDIBR (IDI) read+write traffic.
* npu = VPU_MEMORY_BW, a 32-bit cumulative counter.
* gpu = MemSWITCH (IO path) - npu - idle IO floor. MemSWITCH carries GPU *and*
  NPU (and a little other IO) traffic, so the NPU share is subtracted. The GT NOC
  counter only sees about half of the GPU traffic and is not used.

Only platforms with `verified=True` have been checked on hardware; the others
come from the XML alone. Platforms that carry system totals are guarded at
runtime: if the attributed sum keeps exceeding the PMT system total, attribution
is switched off with one warning.

These are attributed estimates, not exact per-device DRAM traffic. Reading telem
needs root. A bandwidth reading is a counter delta, so the first call yields
None; the shared sampler behind `get_device_bandwidth()` is lock-guarded.
"""

import enum
import glob
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple

from utils.logger import get_logger
logger = get_logger(__name__)

PMT_GUID_MTL = "0x130670b2"   # Meteor Lake telemetry GUID
PMT_GUID_ARL = "0x1306a0b3"   # Arrow Lake telemetry GUID
PMT_GUID_ARL_H = "0x1306a0b2" # Arrow Lake-H telemetry GUID
PMT_GUID_ARL_S = "0x1306a0b4" # Arrow Lake-S telemetry GUID
PMT_GUID_LNL = "0x3072005"    # Lunar Lake telemetry GUID
PMT_GUID_PTL = "0x3086000"    # Panther Lake telemetry GUID
PMT_GUID_WCL = "0x308d000"    # Wildcat Lake telemetry GUID

_PMT_ROOT = "/sys/class/intel_pmt"

_MASK64 = (1 << 64) - 1
_MASK32 = (1 << 32) - 1

# VPU_ENERGY is U32.18.14 joules in a free-running 32-bit counter: it wraps
# every 2**18 J.
ENERGY_COUNTER_WRAP_J = float(1 << 18)

# Attributed sum may exceed the PMT system total by this factor (counters are sampled
# at slightly different instants) before it counts as a bad sample.
_GUARD_RATIO = 1.3
# Ignore the guard below this system bandwidth: ratios are noise at near-idle.
_GUARD_MIN_SYS_GB_S = 1.0
_GUARD_BAD_SAMPLES = 5


class CpuGen(enum.IntEnum):
    MTL = 0
    ARL = 1
    LNL = 2
    PTL = 3
    WCL = 4

    def __str__(self):
        names = {
            CpuGen.MTL: "Meteor Lake",
            CpuGen.ARL: "Arrow Lake",
            CpuGen.LNL: "Lunar Lake",
            CpuGen.PTL: "Panther Lake",
            CpuGen.WCL: "Wildcat Lake",
        }
        return names[self]


@dataclass(frozen=True)
class Platform:
    name: str
    cpu_gen: CpuGen
    verified: bool
    # NPU registers (64-bit containers; bit slices are applied by PmtTelemetry).
    regs: Dict[str, int]
    # VPU_MEMORY_BW is a 32-bit cumulative counter; bytes per count.
    noc_bw_unit_bytes: int = 1024
    # 64-bit cumulative bandwidth counters, `unit_bytes` per count. Empty where
    # the platform's XML does not define them.
    cpu_offsets: Tuple[int, ...] = ()
    io_offsets: Tuple[int, ...] = ()
    sys_offsets: Tuple[int, ...] = ()
    unit_bytes: int = 32
    # Idle IO-path traffic from other devices; subtracted when deriving GPU so an
    # idle GPU reads ~0. Only measured on Wildcat Lake.
    io_floor_gb_s: float = 0.0


_MTL_REGS = {
    'VPU_ENERGY': 0x628,
    'SOC_TEMPERATURES': 0x98,
    'VPU_WORKPOINT': 0x68,
    'VPU_MEMORY_BW': 0x0,
}
_LNL_REGS = {
    'VPU_ENERGY': 0x5d0,
    'SOC_TEMPERATURES': 0x70,
    'VPU_WORKPOINT': 0x18,
    'VPU_MEMORY_BW': 0xc18,
}
_PTL_REGS = {
    'VPU_ENERGY': 0x670,
    'SOC_TEMPERATURES': 0x78,
    'VPU_WORKPOINT': 0x18,
    'VPU_MEMORY_BW': 0xc18,
}

# MTL/ARL: VPU_MEMORY_BW is in KB (bw_KB = count / 1e3 MB), not KiB, and the XML
# has no per-device counters: only NPU and system total.
_MTL = Platform(
    name="Meteor/Arrow Lake", cpu_gen=CpuGen.MTL, verified=False,
    regs=_MTL_REGS, noc_bw_unit_bytes=1000,
    sys_offsets=(0x7e0, 0x7d8),
)
_ARL = Platform(
    name="Meteor/Arrow Lake", cpu_gen=CpuGen.ARL, verified=False,
    regs=_MTL_REGS, noc_bw_unit_bytes=1000,
    sys_offsets=(0x7e0, 0x7d8),
)
_LNL = Platform(
    name="Lunar Lake", cpu_gen=CpuGen.LNL, verified=False,
    regs=_LNL_REGS,
    cpu_offsets=(0x620, 0x628, 0x630, 0x638),
    io_offsets=(0x640, 0x648),
    sys_offsets=(0x778, 0x770),
)
# PTL's XML has no SANTA/IDIBR/MEMSWITCH counters: only NPU and system total.
_PTL = Platform(
    name="Panther Lake", cpu_gen=CpuGen.PTL, verified=False,
    regs=_PTL_REGS,
    sys_offsets=(0x818, 0x810),
)
_WCL = Platform(
    name="Wildcat Lake", cpu_gen=CpuGen.WCL, verified=True,
    regs=_PTL_REGS,
    cpu_offsets=(0x6c0, 0x6c8, 0x6d0, 0x6d8),  # SANTA RD/WR, IDIBR RD/WR
    io_offsets=(0x6e0, 0x6e8),                 # MEMSWITCH RD/WR
    sys_offsets=(0x818, 0x810),                # TOTAL_READ/WRITE_BANDWIDTH
    io_floor_gb_s=1.91,
)

PLATFORMS: Dict[str, Platform] = {
    PMT_GUID_WCL: _WCL,
    PMT_GUID_LNL: _LNL,
    PMT_GUID_PTL: _PTL,
    PMT_GUID_MTL: _MTL,
    PMT_GUID_ARL: _ARL,
    PMT_GUID_ARL_H: _ARL,
    PMT_GUID_ARL_S: _ARL,
}


class TelemNode(NamedTuple):
    dir: str             # .../telemN
    path: str            # .../telemN/telem
    guid: str
    platform: Platform
    pci: Optional[str]   # PCI address of the owning device, e.g. 0000:00:0b.0


def _fdump(path: str) -> str:
    with open(path, "r") as f:
        return f.read().strip()


def _pci_of(telem_dir: str) -> Optional[str]:
    """telemN/device -> intel_vsec.telemetry.N -> parent PCI function."""
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(
            os.path.join(telem_dir, "device"))))
    except OSError:
        return None
    return parent if ":" in parent else None


def find_telem_nodes() -> List[TelemNode]:
    """Every telem node whose GUID is in `PLATFORMS`."""
    nodes: List[TelemNode] = []
    for d in sorted(glob.glob(os.path.join(_PMT_ROOT, "telem*"))):
        try:
            guid = _fdump(os.path.join(d, "guid")).lower()
        except OSError:
            continue
        platform = PLATFORMS.get(guid)
        if platform is not None:
            nodes.append(TelemNode(d, os.path.join(d, "telem"), guid, platform, _pci_of(d)))
    return nodes


def read_bits(buf: bytes, offset: int, msb: int, lsb: int) -> int:
    """Bits msb:lsb of the little-endian 64-bit word at `offset`."""
    if len(buf) < offset + 8:
        raise ValueError(f'Telemetry buffer too short to read offset {offset:#x}')
    data = int.from_bytes(buf[offset:offset + 8], byteorder='little')
    mask = ((1 << (int(msb) + 1)) - 1) & ~((1 << int(lsb)) - 1)
    return (data & mask) >> int(lsb)


class PmtTelemetry:
    """NPU view of the first telem node with a known GUID."""

    def __init__(self):
        self.buffer = None
        self.platform = None
        self.regs = None
        self.cpu_gen = None
        self.telemetry_path = None

        # Check if PMT sysfs exists
        if not os.path.exists(_PMT_ROOT):
            raise RuntimeError(f'PMT sysfs interface not found at {_PMT_ROOT}')

        node = next((n for n in find_telem_nodes()
                     if all(os.path.exists(os.path.join(n.dir, p)) for p in ('size', 'offset'))),
                    None)
        if node is None:
            raise RuntimeError('No CPU telemetry devices found with known GUIDs')

        telem_size = int(_fdump(os.path.join(node.dir, 'size')))
        self.node = node
        self.platform = node.platform
        self.regs = node.platform.regs
        self.cpu_gen = node.platform.cpu_gen
        self.telemetry_path = node.path
        logger.debug('Found PMT device %s with GUID %s, size %d', node.dir, node.guid, telem_size)

        needed = max(self.regs.values()) + 8
        if telem_size < needed:
            raise RuntimeError(f'Telemetry region too small for {self.cpu_gen}: '
                               f'{telem_size} bytes, need {needed}')

        # The telem node is root-only; fail here so callers fall back to sysfs
        # instead of erroring out on the first update_buffer().
        if not os.access(self.telemetry_path, os.R_OK):
            raise RuntimeError(f'No read permission for {self.telemetry_path}')

        logger.debug('CPU generation detected: %s', self.cpu_gen)

    @property
    def noc_bandwidth_wrap_mib(self) -> float:
        """MiB the 32-bit NoC bandwidth counter covers before it wraps."""
        return (1 << 32) * self.platform.noc_bw_unit_bytes / (1 << 20)

    def read(self, offset, msb, lsb):
        """Slice bits msb:lsb from the last snapshot at the 64-bit container `offset`."""
        buf = self.buffer
        if buf is None:
            raise ValueError(f'Telemetry buffer too short to read offset {offset:#x}')
        return read_bits(buf, offset, msb, lsb)

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
        # Intel-PMT XML: TILE_CONFIG is 16 bits wide on MTL, 8 bits on LNL and later.
        if self.cpu_gen in (CpuGen.MTL, CpuGen.ARL):
            return self.read(self.regs['VPU_WORKPOINT'], 31, 16)
        return self.read(self.regs['VPU_WORKPOINT'], 23, 16)

    def get_npu_temperature(self):
        # Per the Intel-PMT XML, VPU_TEMP moved down a byte on PTL/WCL;
        # 47:40 there is Media_TEMP.
        if self.cpu_gen in (CpuGen.PTL, CpuGen.WCL):
            return self.read(self.regs['SOC_TEMPERATURES'], 39, 32)
        return self.read(self.regs['SOC_TEMPERATURES'], 47, 40)

    def get_npu_energy(self):
        # Units: joules, Cast: U32.18.14 -- the value is 32 bits wide inside a
        # 64-bit container (upstream intel-npu-smi reads 31:0 too).
        val = self.read(self.regs['VPU_ENERGY'], 31, 0)
        int_part = val >> 14
        float_part = (val & ((1 << 14) - 1)) / (1 << 14)
        return int_part + float_part

    def get_noc_bandwidth(self):
        # Cumulative traffic counter (Intel-PMT tbw_1024B / tbw_KB); returned in
        # MiB. Upstream divides by 1e3 instead.
        val = self.read(self.regs['VPU_MEMORY_BW'], 31, 0)
        return val * self.platform.noc_bw_unit_bytes / (1 << 20)


# --- per-device memory bandwidth attribution -------------------------------

@dataclass(frozen=True)
class DeviceBandwidth:
    cpu_gb_s: Optional[float]
    gpu_gb_s: Optional[float]
    npu_gb_s: Optional[float]
    # PMT's own system total; only used to sanity-check the attribution.
    sys_gb_s: Optional[float] = None


def compute_bandwidth(
    platform: Platform, prev: bytes, cur: bytes, dt: float,
) -> DeviceBandwidth:
    """Pure function: two telem snapshots `dt` seconds apart -> per-device GB/s."""
    def counted(offsets: Tuple[int, ...]) -> float:
        total = 0
        for off in offsets:
            total += (read_bits(cur, off, 63, 0) - read_bits(prev, off, 63, 0)) & _MASK64
        return total * platform.unit_bytes / dt / 1e9

    cpu = counted(platform.cpu_offsets) if platform.cpu_offsets else None
    sys_total = counted(platform.sys_offsets) if platform.sys_offsets else None

    noc = platform.regs['VPU_MEMORY_BW']
    counts = (read_bits(cur, noc, 31, 0) - read_bits(prev, noc, 31, 0)) & _MASK32
    npu = counts * platform.noc_bw_unit_bytes / dt / 1e9

    gpu: Optional[float] = None
    if platform.io_offsets:
        gpu = max(0.0, counted(platform.io_offsets) - npu - platform.io_floor_gb_s)

    return DeviceBandwidth(cpu_gb_s=cpu, gpu_gb_s=gpu, npu_gb_s=npu, sys_gb_s=sys_total)


def _implausible(bw: DeviceBandwidth) -> bool:
    """True when the attributed sum clearly exceeds the PMT system total."""
    if bw.sys_gb_s is None or bw.sys_gb_s < _GUARD_MIN_SYS_GB_S:
        return False
    attributed = sum(v for v in (bw.cpu_gb_s, bw.gpu_gb_s, bw.npu_gb_s) if v is not None)
    return attributed > bw.sys_gb_s * _GUARD_RATIO


def _bandwidth_span(platform: Platform) -> int:
    offsets = (list(platform.cpu_offsets) + list(platform.io_offsets)
               + list(platform.sys_offsets) + [platform.regs['VPU_MEMORY_BW']])
    return max(offsets) + 8


class PmtBandwidthSampler:
    def __init__(self) -> None:
        self._node: Optional[TelemNode] = None
        self._prev: Optional[bytes] = None
        self._prev_ts: Optional[float] = None
        self._bad_samples = 0
        self._disabled = False
        # Last good reading and its window, for the debug log only.
        self.last: Optional[Tuple[DeviceBandwidth, float]] = None

    def start(self) -> bool:
        nodes = find_telem_nodes()
        if not nodes:
            logger.info("PMT bandwidth unavailable: no telem node with a known GUID")
            return False
        self._node = nodes[0]
        logger.info(
            "PMT bandwidth: %s (guid %s, pci %s, verified=%s)",
            self._node.platform.name, self._node.guid, self._node.pci,
            self._node.platform.verified,
        )
        return True

    def _read(self) -> Optional[bytes]:
        try:
            with open(self._node.path, "rb") as f:
                return f.read()
        except OSError as exc:
            logger.warning("PMT bandwidth read failed: %s", exc)
            return None

    def sample(self) -> Optional[DeviceBandwidth]:
        if self._disabled:
            return None
        buf = self._read()
        now = time.monotonic()
        if buf is None:
            self._prev = None
            return None
        if len(buf) < _bandwidth_span(self._node.platform):
            self._disabled = True
            logger.warning(
                "PMT bandwidth disabled: telem for %s is %d bytes, too small for its counters",
                self._node.platform.name, len(buf),
            )
            return None
        prev, prev_ts = self._prev, self._prev_ts
        self._prev, self._prev_ts = buf, now
        if prev is None or prev_ts is None or now <= prev_ts:
            return None
        bw = compute_bandwidth(self._node.platform, prev, buf, now - prev_ts)
        self.last = (bw, now - prev_ts)

        self._bad_samples = self._bad_samples + 1 if _implausible(bw) else 0
        if self._bad_samples >= _GUARD_BAD_SAMPLES:
            self._disabled = True
            logger.warning(
                "PMT bandwidth attribution disabled: %s (verified=%s) keeps "
                "exceeding the PMT system total; its offsets/units are probably wrong "
                "for this platform", self._node.platform.name, self._node.platform.verified,
            )
            return None
        return bw


_shared: Optional[PmtBandwidthSampler] = None
_shared_started = False
_shared_lock = threading.Lock()


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def last_pmt_sample() -> Optional[Tuple[DeviceBandwidth, float]]:
    """(raw reading incl. PMT system total, window seconds) of the latest sample."""
    return _shared.last if _shared is not None else None


def get_device_bandwidth() -> Dict[str, Optional[float]]:
    """CPU/GPU bandwidth as a plain dict; fields are None when unavailable.

    The NPU's own figure is `noc_bandwidth_mib_per_s` from the NPU telemetry;
    it is only used here to derive the GPU share.
    """
    global _shared, _shared_started
    blank: Dict[str, Optional[float]] = {"cpu_gb_s": None, "gpu_gb_s": None}
    with _shared_lock:
        if not _shared_started:
            _shared_started = True
            sampler = PmtBandwidthSampler()
            if sampler.start():
                _shared = sampler
        if _shared is None:
            return blank
        bw = _shared.sample()
    if bw is None:
        return blank
    return {"cpu_gb_s": _round(bw.cpu_gb_s), "gpu_gb_s": _round(bw.gpu_gb_s)}
