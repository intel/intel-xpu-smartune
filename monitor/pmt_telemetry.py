# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# PMT telemetry logic derived from intel/linux-npu-driver (MIT License)
# https://github.com/intel/linux-npu-driver

"""Single entry point for SoC Punit PMT telemetry (/sys/class/intel_pmt/telem*).

Everything that reads PMT goes through this module:
* `PLATFORMS` maps a telem GUID to a `Platform`: the NPU register map plus the
  bandwidth counters, with offsets/units from the Intel-PMT XML for that GUID
  (https://github.com/intel/Intel-PMT, xml/<platform>/). A counter a platform
  lacks is absent, so the matching value is None.
* `PmtTelemetry` reads one snapshot of the node and decodes NPU power /
  temperature / frequency / tile config / NoC bandwidth.
* `PmtBandwidthSampler` attributes memory bandwidth to CPU / GPU / NPU and reads
  the SoC cache and DRAM counters; `bandwidth_fields()` turns a reading into a dict.

Where the counters sit (one memory access walks down this path):

    device-internal caches (CPU L1/L2/LLC, GPU L3, NPU SRAM)   - not visible
      -> device egress: SANTA/IDIBR (CPU), MemSWITCH (GPU + NPU)
      -> MUFASA, the SoC memory-side cache shared by all devices: hit / miss
         (both count reads and writes)
      -> memory controller: TOTAL_READ/WRITE_BANDWIDTH (= the IMC PMU total)
         ~= miss + write-back of evicted dirty lines; equals miss when the hit
         ratio is low, up to ~8 GB/s above it on write-heavy, cache-friendly load

Bandwidth attribution:
* cpu = SANTA (CCF) + IDIBR (IDI) read+write traffic.
* npu = VPU_MEMORY_BW, a 32-bit cumulative counter.
* gpu, on platforms without per-GPU egress counters, first match wins:
  1. GPU idle (GT RC6 and media C6 residency both >= 95%): 0. MemSWITCH may still
     carry disk/swap DMA or display traffic, none of which is the GPU's.
  2. Normal: MemSWITCH - npu - idle IO floor. MemSWITCH carries GPU *and* NPU (and
     other IO) traffic, so the NPU share is subtracted. The GT NOC counter only
     sees about half of the GPU traffic and is not used.
  3. Divergence episode on a verified platform (see the guard below):
     MUFASA hit + miss - cpu - npu - floor, flagged `gpu_estimated`. Heavy
     non-cached IO such as swap/NVMe DMA inflates MemSWITCH but bypasses MUFASA,
     so the cache total excludes it. Coarse (the two totals agree only within
     ~20%, and subtracting the usually larger cpu share amplifies that), so it is
     only meaningful while the GPU is busy.
"""

import dataclasses
import enum
import glob
import os
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

# Attributed egress traffic (cpu + MemSWITCH) may differ from MUFASA hit + miss
# by this fraction before a sample counts as bad.
# under load, up to 1.12 near idle.
_GUARD_TOLERANCE = 0.2
# Ignore the guard below this MUFASA traffic: ratios are noise at near-idle.
_GUARD_MIN_GB_S = 1.0
_GUARD_BAD_SAMPLES = 5

# GT_RC6_RESIDENCY / MEDIA_C6_RESIDENCY count crystal ticks.
_XTAL_HZ = 38.4e6
# Both GT RC6 and media C6 at or above this -> GPU idle; a sample then updates
# the IO floor. Media load leaves GT RC6 at ~0.98, so media C6 is required too.
_GPU_IDLE_RESIDENCY = 0.95
# A residency ratio above this means the tick rate is wrong for the platform;
# the GPU state is then treated as unknown rather than idle.
_RESIDENCY_SANE_MAX = 1.1
# Exponential smoothing weight of a new idle IO-floor observation.
_FLOOR_EMA_ALPHA = 0.2


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
    # (TOTAL_READ_BANDWIDTH, TOTAL_WRITE_BANDWIDTH): DRAM traffic at the memory
    # controller, same as the IMC PMU. Exactly two entries, read first, or empty.
    sys_offsets: Tuple[int, ...] = ()
    unit_bytes: int = 32
    # Bytes per count for the cpu/io/gpu counters, where it differs from
    # `unit_bytes` (MTL: NoC request counters are 64B, TOTAL_READ/WRITE 32B).
    # None = same as `unit_bytes`.
    egress_unit_bytes: Optional[int] = None
    # Per-GPU egress counters on platforms that have them. When set, `gpu` is their
    # sum directly: no `io` figure, no NPU subtraction, no idle floor.
    gpu_offsets: Tuple[int, ...] = ()
    # MUFASA (SoC memory-side cache) hit / miss counters, 64-bit cumulative,
    # `cache_unit_bytes` per count. None where the platform lacks them; such a
    # platform has no runtime guard and no hit ratio.
    cache_hit_offset: Optional[int] = None
    cache_miss_offset: Optional[int] = None
    cache_unit_bytes: int = 64
    # GT_RC6_RESIDENCY and MEDIA_C6_RESIDENCY (crystal ticks). When present the
    # IO floor is learned from GPU-idle samples; otherwise it stays fixed.
    gpu_idle_offsets: Tuple[int, ...] = ()
    # Idle IO-path traffic from other devices; subtracted when deriving GPU so an
    # idle GPU reads ~0. Initial value only where `gpu_idle_offsets` is set.
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

# MTL/ARL: VPU_MEMORY_BW is in KB (bw_KB = count / 1e3 MB), not KiB.
#
# The MTL XML (guid 0x130670b2) defines per-IP NoC request counters, 64-bit and
# 64B per count ("almost all NoC requests are 64B"), next to the 32B
# TOTAL_READ/WRITE_BANDWIDTH pair. Offsets are the cumulative container layout
# (it reproduces the known VPU_ENERGY 0x628, TOTAL_WRITE 0x7d8 and TOTAL_READ 0x7e0):
#   CPU : NOC_BW_IDI2CMI0/1                    0x728 0x730
#   GPU : NOC_BW_GCD0/1 + NOC_BW_MEDIA0/1      0x698 0x6a0 + 0x6e8 0x6f0
# The GPU has its own counters here, so it is summed directly instead of derived as
# MemSWITCH - NPU - floor (PTL/LNL). No MUFASA, so no hit ratio and no runtime
# guard.
_MTL_EGRESS = dict(
    cpu_offsets=(0x728, 0x730),
    gpu_offsets=(0x698, 0x6a0, 0x6e8, 0x6f0),
    egress_unit_bytes=64,
)
_MTL = Platform(
    name="Meteor/Arrow Lake", cpu_gen=CpuGen.MTL, verified=False,
    regs=_MTL_REGS, noc_bw_unit_bytes=1000,
    sys_offsets=(0x7e0, 0x7d8),
    **_MTL_EGRESS,
)
_ARL = Platform(
    name="Meteor/Arrow Lake", cpu_gen=CpuGen.ARL, verified=False,
    regs=_MTL_REGS, noc_bw_unit_bytes=1000,
    sys_offsets=(0x7e0, 0x7d8),
    **_MTL_EGRESS,
)
_LNL = Platform(
    name="Lunar Lake", cpu_gen=CpuGen.LNL, verified=False,
    regs=_LNL_REGS,
    cpu_offsets=(0x620, 0x628, 0x630, 0x638),
    io_offsets=(0x640, 0x648),
    sys_offsets=(0x778, 0x770),
)
# PTL's XML leaves the bandwidth block unnamed (HW_COUNTER_MMIO_OFFSET_8988..89D0),
# but it is the same Punit MMIO range as WCL's MUFASA/SANTA/IDIBR/MEMSWITCH and
# behaves the same on hardware: CPU load moves only 0x6c0-0x6d8 (sum within ~4% of
# IMC), GPU/media/NPU load moves only 0x6e0/0x6e8. The XML types the MUFASA pair
# as 32B, but hit + miss only matches egress traffic at 64B.
_PTL = Platform(
    name="Panther Lake", cpu_gen=CpuGen.PTL, verified=True,
    regs=_PTL_REGS,
    cpu_offsets=(0x6c0, 0x6c8, 0x6d0, 0x6d8),  # = SANTA RD/WR, IDIBR RD/WR
    io_offsets=(0x6e0, 0x6e8),                 # = MEMSWITCH RD/WR
    sys_offsets=(0x818, 0x810),                # TOTAL_READ/WRITE_BANDWIDTH
    cache_hit_offset=0x6a0,                    # = MUFASA_HIT_BW
    cache_miss_offset=0x6a8,                   # = MUFASA_MISS_BW
    gpu_idle_offsets=(0x4d0, 0x4d8),           # GT_RC6 / MEDIA_C6_RESIDENCY
    io_floor_gb_s=0.0,                         # measured idle MemSWITCH ~0.06
)
_WCL = Platform(
    name="Wildcat Lake", cpu_gen=CpuGen.WCL, verified=True,
    regs=_PTL_REGS,
    cpu_offsets=(0x6c0, 0x6c8, 0x6d0, 0x6d8),  # SANTA RD/WR, IDIBR RD/WR
    io_offsets=(0x6e0, 0x6e8),                 # MEMSWITCH RD/WR
    sys_offsets=(0x818, 0x810),                # TOTAL_READ/WRITE_BANDWIDTH
    cache_hit_offset=0x6a0,                    # MUFASA_HIT_BW (tbw_64B)
    cache_miss_offset=0x6a8,                   # MUFASA_MISS_BW (tbw_64B)
    gpu_idle_offsets=(0x4d0, 0x4d8),           # GT_RC6 / MEDIA_C6_RESIDENCY
    io_floor_gb_s=1.91,                        # seed; measured once, source unconfirmed
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
        # Per the Intel-PMT XML, VPU_TEMP moved down a byte;
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
    # PMT's DRAM read / write at the memory controller.
    sys_read_gb_s: Optional[float] = None
    sys_write_gb_s: Optional[float] = None
    # Raw MemSWITCH (GPU + NPU + other IO) before the NPU / floor subtraction.
    io_gb_s: Optional[float] = None
    # MUFASA memory-side cache hit / miss traffic.
    cache_hit_gb_s: Optional[float] = None
    cache_miss_gb_s: Optional[float] = None
    # True/False when GT RC6 and media C6 residency are readable and sane.
    gpu_idle: Optional[bool] = None
    # gpu_gb_s was estimated from the SoC cache total instead of MemSWITCH.
    gpu_estimated: bool = False

    @property
    def sys_gb_s(self) -> Optional[float]:
        if self.sys_read_gb_s is None or self.sys_write_gb_s is None:
            return None
        return self.sys_read_gb_s + self.sys_write_gb_s

    @property
    def cache_total_gb_s(self) -> Optional[float]:
        if self.cache_hit_gb_s is None or self.cache_miss_gb_s is None:
            return None
        return self.cache_hit_gb_s + self.cache_miss_gb_s

    @property
    def cache_hit_percent(self) -> Optional[float]:
        total = self.cache_total_gb_s
        if not total:
            return None
        return 100.0 * self.cache_hit_gb_s / total


def _delta64(prev: bytes, cur: bytes, off: int) -> int:
    return (read_bits(cur, off, 63, 0) - read_bits(prev, off, 63, 0)) & _MASK64


def _gpu_idle(platform: Platform, prev: bytes, cur: bytes, dt: float) -> Optional[bool]:
    """GPU idle when every RC6/C6 residency covers most of the window."""
    if not platform.gpu_idle_offsets:
        return None
    ratios = [_delta64(prev, cur, off) / (dt * _XTAL_HZ) for off in platform.gpu_idle_offsets]
    if any(r > _RESIDENCY_SANE_MAX for r in ratios):
        return None
    return all(r >= _GPU_IDLE_RESIDENCY for r in ratios)


def compute_bandwidth(
    platform: Platform, prev: bytes, cur: bytes, dt: float,
    io_floor_gb_s: Optional[float] = None,
) -> DeviceBandwidth:
    """Pure function: two telem snapshots `dt` seconds apart -> per-device GB/s.

    `io_floor_gb_s` overrides the platform's floor (the sampler passes its learned one).
    """
    def counted(offsets: Tuple[int, ...], unit: int = platform.unit_bytes) -> float:
        return sum(_delta64(prev, cur, off) for off in offsets) * unit / dt / 1e9

    egress = platform.egress_unit_bytes or platform.unit_bytes
    cpu = counted(platform.cpu_offsets, egress) if platform.cpu_offsets else None
    sys_read = sys_write = None
    if platform.sys_offsets:
        read_off, write_off = platform.sys_offsets
        sys_read = counted((read_off,))
        sys_write = counted((write_off,))

    noc = platform.regs['VPU_MEMORY_BW']
    counts = (read_bits(cur, noc, 31, 0) - read_bits(prev, noc, 31, 0)) & _MASK32
    npu = counts * platform.noc_bw_unit_bytes / dt / 1e9

    gpu_idle = _gpu_idle(platform, prev, cur, dt)
    io: Optional[float] = None
    gpu: Optional[float] = None
    if platform.gpu_offsets:
        gpu = counted(platform.gpu_offsets, egress)
    elif platform.io_offsets:
        floor = platform.io_floor_gb_s if io_floor_gb_s is None else io_floor_gb_s
        io = counted(platform.io_offsets, egress)
        # GT and media both parked: the GPU is not touching memory, whatever else
        # (disk/swap DMA, display) MemSWITCH is carrying.
        gpu = 0.0 if gpu_idle else max(0.0, io - npu - floor)

    hit = miss = None
    if platform.cache_hit_offset is not None and platform.cache_miss_offset is not None:
        hit = counted((platform.cache_hit_offset,), platform.cache_unit_bytes)
        miss = counted((platform.cache_miss_offset,), platform.cache_unit_bytes)

    return DeviceBandwidth(
        cpu_gb_s=cpu, gpu_gb_s=gpu, npu_gb_s=npu,
        sys_read_gb_s=sys_read, sys_write_gb_s=sys_write, io_gb_s=io,
        cache_hit_gb_s=hit, cache_miss_gb_s=miss,
        gpu_idle=gpu_idle,
    )


def gpu_from_cache(bw: DeviceBandwidth, io_floor_gb_s: float) -> Optional[float]:
    """GPU share estimated as SoC cache total - CPU - NPU - floor.

    Used when MemSWITCH carries IO the cache never sees (swap/NVMe DMA), which the
    usual MemSWITCH - NPU - floor would count as GPU. Coarse: the cache and egress
    totals agree only within ~20%, and subtracting the (usually larger) CPU share
    amplifies that, so it is only meaningful when the GPU itself is busy.
    """
    cache = bw.cache_total_gb_s
    if cache is None or bw.cpu_gb_s is None:
        return None
    return max(0.0, cache - bw.cpu_gb_s - (bw.npu_gb_s or 0.0) - io_floor_gb_s)


def _implausible(bw: DeviceBandwidth) -> bool:
    """True when egress traffic (cpu + MemSWITCH) and MUFASA hit + miss diverge.

    Both measure the same traffic at the same level, so a large gap means wrong
    offsets/units. No MUFASA counters (or no cpu/io counters) -> never implausible.
    """
    cache = bw.cache_total_gb_s
    if cache is None or bw.cpu_gb_s is None or bw.io_gb_s is None:
        return False
    if cache < _GUARD_MIN_GB_S:
        return False
    return abs((bw.cpu_gb_s + bw.io_gb_s) / cache - 1.0) > _GUARD_TOLERANCE


def _bandwidth_span(platform: Platform) -> int:
    offsets = (list(platform.cpu_offsets) + list(platform.io_offsets)
               + list(platform.gpu_offsets) + list(platform.sys_offsets) + list(platform.gpu_idle_offsets)
               + [o for o in (platform.cache_hit_offset, platform.cache_miss_offset)
                  if o is not None]
               + [platform.regs['VPU_MEMORY_BW']])
    return max(offsets) + 8


class PmtBandwidthSampler:
    def __init__(self) -> None:
        self._node: Optional[TelemNode] = None
        self._prev: Optional[bytes] = None
        self._prev_ts: Optional[float] = None
        self._bad_samples = 0
        self._disabled = False
        # Verified platform in a divergence episode: GPU share withheld until it ends.
        self._diverging = False
        # Idle IO-path floor; seeded from the platform, learned from GPU-idle samples.
        self.io_floor_gb_s = 0.0
        # Last good reading and its window, for the debug log only.
        self.last: Optional[Tuple[DeviceBandwidth, float]] = None

    def start(self) -> bool:
        nodes = find_telem_nodes()
        if not nodes:
            logger.info("PMT bandwidth unavailable: no telem node with a known GUID")
            return False
        node = nodes[0]
        # telem is root-only; checking here keeps a non-root process from logging
        # a read failure on every sample.
        if not os.access(node.path, os.R_OK):
            logger.info("PMT bandwidth unavailable: no read permission for %s", node.path)
            return False
        self._node = node
        self.io_floor_gb_s = node.platform.io_floor_gb_s
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
        dt = now - prev_ts
        bw = compute_bandwidth(self._node.platform, prev, buf, dt, self.io_floor_gb_s)

        platform = self._node.platform
        self._bad_samples = self._bad_samples + 1 if _implausible(bw) else 0
        diverging = self._bad_samples >= _GUARD_BAD_SAMPLES
        if diverging and not platform.verified:
            # Unverified layout: a lasting gap most likely means wrong offsets/units.
            self._disabled = True
            logger.warning(
                "PMT bandwidth attribution disabled: %s (verified=False) egress traffic "
                "keeps diverging from the MUFASA hit+miss total; its offsets/units are "
                "probably wrong for this platform",
                platform.name,
            )
            return None
        if diverging:
            # Verified layout: the gap is the workload, typically swap/NVMe DMA that
            # MemSWITCH counts but the SoC cache never sees. Only the GPU share is
            # derived from MemSWITCH; CPU, cache and DRAM are direct counters and
            # stay valid. An idle GPU already reads 0; a busy one is re-estimated
            # from the cache total. Recovers on the first good sample.
            if not self._diverging:
                logger.warning(
                    "PMT bandwidth: %s egress traffic diverges from the MUFASA hit+miss "
                    "total (likely non-cached IO such as swap); GPU bandwidth estimated "
                    "from the SoC cache until it recovers", platform.name,
                )
            if not bw.gpu_idle:
                bw = dataclasses.replace(
                    bw, gpu_gb_s=gpu_from_cache(bw, self.io_floor_gb_s), gpu_estimated=True)
        elif self._diverging:
            logger.info("PMT bandwidth: %s egress traffic consistent again; GPU bandwidth "
                        "back to MemSWITCH", platform.name)
        self._diverging = diverging
        if not self._bad_samples:
            # Don't learn the floor from a suspect sample.
            bw = self._learn_io_floor(bw)
        self.last = (bw, dt)
        return bw

    def _learn_io_floor(self, bw: DeviceBandwidth) -> DeviceBandwidth:
        """On a GPU-idle sample, fold its IO traffic into the floor (gpu is already 0)."""
        if not bw.gpu_idle or bw.io_gb_s is None:
            return bw
        observed = max(0.0, bw.io_gb_s - (bw.npu_gb_s or 0.0))
        self.io_floor_gb_s += _FLOOR_EMA_ALPHA * (observed - self.io_floor_gb_s)
        return bw


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def bandwidth_fields(bw: Optional[DeviceBandwidth]) -> Dict[str, Optional[float]]:
    """A `PmtBandwidthSampler.sample()` reading as a plain dict (all None for no
    reading): CPU/GPU egress bandwidth, SoC memory-side cache hit/miss and DRAM
    read/write.

    The NPU's own figure is `noc_bandwidth_mib_per_s` from the NPU telemetry;
    here it is only used to derive the GPU share.
    """
    if bw is None:
        return {
            "cpu_gb_s": None, "gpu_gb_s": None, "gpu_estimated": False, "soc_cache_hit_percent": None,
            "soc_cache_hit_gb_s": None, "soc_cache_miss_gb_s": None,
            "dram_read_gb_s": None, "dram_write_gb_s": None,
        }
    hit = bw.cache_hit_percent
    return {
        "cpu_gb_s": _round(bw.cpu_gb_s),
        "gpu_gb_s": _round(bw.gpu_gb_s),
        "gpu_estimated": bw.gpu_estimated,
        "soc_cache_hit_percent": None if hit is None else round(hit, 1),
        "soc_cache_hit_gb_s": _round(bw.cache_hit_gb_s),
        "soc_cache_miss_gb_s": _round(bw.cache_miss_gb_s),
        # Unrounded: the caller rounds after summing read + write.
        "dram_read_gb_s": bw.sys_read_gb_s,
        "dram_write_gb_s": bw.sys_write_gb_s,
    }
