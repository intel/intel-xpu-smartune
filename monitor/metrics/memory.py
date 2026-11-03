# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""System memory metrics for SmarTune.

* `get_memory_static()`  - DMI inventory: DDR speeds, per-slot devices, total
  capacity, and the theoretical peak-bandwidth ceiling.
* `get_memory_capacity()` - live capacity (used / available / percent plus swap).
* `get_memory_dynamic()` - capacity plus the dashboard's live bandwidth reading.
* `MemoryBandwidthSampler` - live DRAM bandwidth: from SoC PMT telemetry where the
  platform has it (see `pmt_telemetry`), else from the IMC `Sampler`. Also carries
  CPU/GPU egress bandwidth and SoC cache hit/miss (PMT). A reading is a counter
  delta, so each consumer owns an instance (the benchmark sampler has its own);
  the dashboard's shared one behind `get_memory_bandwidth()` is lock-guarded.
* `Sampler` - DRAM bandwidth from the uncore IMC free-running counters. Needs
  root or `perf_event_paranoid <= 0`.
"""

import ctypes
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from monitor.metrics.utils import run_cmd
from monitor.pmt_telemetry import DeviceBandwidth, PmtBandwidthSampler, bandwidth_fields
from utils.logger import get_logger
logger = get_logger(__name__)

# x86_64. This module is Intel-uncore-specific anyway, so there is no other arch
# to care about.
_SYS_PERF_EVENT_OPEN = 298

_EVENT_SOURCE_DIR = "/sys/bus/event_source/devices"

# Known units a PMU's .scale attribute can be expressed in. Every Intel
# platform so far reports data_total in MiB; this table lets a future
# platform change units without losing the metric, while a unit outside this
# table still falls back to skipping the channel rather than guessing.
_UNIT_TO_BYTES = {
    "Bytes": 1,
    "KiB": 1024,
    "MiB": 1024 ** 2,
    "GiB": 1024 ** 3,
}

# The three free-running events each IMC channel exposes under events/data_*.
_EVENT_KINDS = ("total", "read", "write")

_libc = ctypes.CDLL(None, use_errno=True)

# Warn once per process about an unavailable PMU: benchmark/service/sampler.py would
# otherwise log this on every tick.
_unavailable_warned = False


class _PerfEventAttr(ctypes.Structure):
    """struct perf_event_attr, as of Linux 5.13 (sig_data was the last field
    added). Only `type`, `size` and `config` are set; the kernel checks `size`
    to know which fields it may read, so the layout has to stay complete."""

    _fields_ = [
        ("type", ctypes.c_uint),
        ("size", ctypes.c_uint),
        ("config", ctypes.c_ulonglong),
        ("sample_period", ctypes.c_ulonglong),
        ("sample_type", ctypes.c_ulonglong),
        ("read_format", ctypes.c_ulonglong),
        ("flags", ctypes.c_ulonglong),
        ("wakeup_events", ctypes.c_uint),
        ("bp_type", ctypes.c_uint),
        ("config1", ctypes.c_ulonglong),
        ("config2", ctypes.c_ulonglong),
        ("branch_sample_type", ctypes.c_ulonglong),
        ("sample_regs_user", ctypes.c_ulonglong),
        ("sample_stack_user", ctypes.c_uint),
        ("clockid", ctypes.c_int),
        ("sample_regs_intr", ctypes.c_ulonglong),
        ("aux_watermark", ctypes.c_uint),
        ("sample_max_stack", ctypes.c_ushort),
        ("reserved_2", ctypes.c_ushort),
        ("aux_sample_size", ctypes.c_uint),
        ("reserved_3", ctypes.c_uint),
        ("sig_data", ctypes.c_ulonglong),
    ]


@dataclass
class _ImcPmu:
    """One uncore IMC channel's free-running data_{total,read,write} counter."""

    name: str
    kind: str  # one of _EVENT_KINDS
    type_id: int
    cpu: int
    config: int
    # Bytes represented by one counter tick (PMU's .scale attribute converted
    # via its .unit attribute -- see _UNIT_TO_BYTES).
    bytes_per_count: float


@dataclass
class BandwidthSample:
    """One sample(), in GB/s since the previous call."""

    total_gb_s: float
    read_gb_s: float
    write_gb_s: float


# --- DMI (dmidecode -t memory) parsing ------------------------------------------
#
# dmidecode is parsed ONCE, into one key/value record per memory slot
# (`_parse_dmi_slots`).  Every DMI-derived field -- DDR speeds, per-slot device
# inventory, and the bandwidth ceiling.


def _get_dmidecode_memory_output() -> Optional[str]:
    """Single `dmidecode -t memory` call. Needs root; None when unavailable."""
    return run_cmd(["dmidecode", "-t", "memory"])


# DMI is fixed for the life of the process, so the one dmidecode call + parse is
# cached and shared by every consumer (static inventory and the bandwidth
# ceiling). None caches as [] -- without root it will not succeed on a retry.
_dmi_slots_cache: Optional[List[Dict[str, str]]] = None


def _dmi_slots() -> List[Dict[str, str]]:
    """Parsed dmidecode memory slots, read and parsed once per process."""
    global _dmi_slots_cache
    if _dmi_slots_cache is None:
        _dmi_slots_cache = _parse_dmi_slots(_get_dmidecode_memory_output())
    return _dmi_slots_cache


def _parse_dmi_slots(output: Optional[str]) -> List[Dict[str, str]]:
    """One raw key/value dict per `Memory Device` slot, populated or empty.

    This is the module's single parse of the dmidecode text; the extractors below
    read these records instead of re-scanning the output.
    """
    if not output:
        return []
    slots: List[Dict[str, str]] = []
    for section in re.split(r"(?=^Memory Device\s*$)", output, flags=re.MULTILINE):
        if not section.strip().startswith("Memory Device"):
            continue
        kv: Dict[str, str] = {}
        for line in section.splitlines():
            key, sep, val = line.strip().partition(":")
            if sep:
                kv[key.strip()] = val.strip()
        slots.append(kv)
    return slots


def _leading_int(value: Optional[str]) -> Optional[int]:
    """Leading integer of a dmidecode value like '4800 MT/s' or '64 bits'.

    None for "Unknown" / empty, so empty slots drop out of numeric aggregates.
    """
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def _slot_populated(slot: Dict[str, str]) -> bool:
    size = slot.get("Size", "")
    return bool(size) and "No Module Installed" not in size


def _size_gb(size_raw: Optional[str]) -> Optional[float]:
    if not size_raw or "No Module Installed" in size_raw:
        return None
    match = re.match(r"(\d+)\s*(GB|MB|TB)", size_raw, re.IGNORECASE)
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2).upper()
    if unit == "GB":
        return float(val)
    if unit == "MB":
        return round(val / 1024, 2)
    return float(val * 1024)  # TB


def _ddr_speeds(slots: List[Dict[str, str]]) -> List[str]:
    """Distinct `Speed` strings across all slots, in first-seen order."""
    speeds: List[str] = []
    for slot in slots:
        value = slot.get("Speed")
        if value and value not in speeds:
            speeds.append(value)
    return speeds


def _memory_devices(slots: List[Dict[str, str]]) -> Dict[str, Any]:
    """Per-slot inventory: populated device details plus slot and channel counts."""
    if not slots:
        return {"total_slots": None, "populated": 0, "devices": []}

    devices: List[Dict[str, Any]] = []
    channels: set = set()
    for slot in slots:
        if not _slot_populated(slot):
            continue
        bank_locator = slot.get("Bank Locator") or None
        devices.append({
            "locator": slot.get("Locator") or None,
            "bank_locator": bank_locator,
            "size_gb": _size_gb(slot.get("Size")),
            "type": slot.get("Type") or None,
            "speed": slot.get("Speed") or None,
            "configured_speed": slot.get("Configured Memory Speed") or None,
            "form_factor": slot.get("Form Factor") or None,
            "manufacturer": slot.get("Manufacturer") or None,
            "part_number": (slot.get("Part Number") or "").strip() or None,
        })
        # Channel count from Bank Locator values (e.g. "Channel 0 Slot 0").
        ch_match = re.search(r"Channel\s+(\d+)", bank_locator or "", re.IGNORECASE)
        if ch_match:
            channels.add(int(ch_match.group(1)))

    return {
        "total_slots": len(slots),
        "populated": len(devices),
        "channels": len(channels) if channels else None,
        "devices": devices,
    }


def _max_bandwidth_gbs(slots: List[Dict[str, str]]) -> Optional[float]:
    """Theoretical peak DRAM bandwidth in GB/s: max MT/s * total data width / 1000.

    "Configured Memory Speed" is the rate actually in use; plain "Speed" is the
    module's rating and overstates a downclocked DIMM, so it is only a fallback.
    None when the DMI data is missing (no root / no ceiling known).
    """
    speeds = [s for s in (_leading_int(slot.get("Configured Memory Speed")) for slot in slots) if s]
    if not speeds:
        speeds = [s for s in (_leading_int(slot.get("Speed")) for slot in slots) if s]
    # Empty slots report "Data Width: 0 bits" (or nothing at all).
    widths_bits = [w for w in (_leading_int(slot.get("Data Width")) for slot in slots) if w and w > 0]

    if not speeds or not widths_bits:
        return None
    total_width_bytes = sum(widths_bits) / 8.0
    if total_width_bytes <= 0:
        return None
    return round(max(speeds) * total_width_bytes / 1000.0, 2)


def get_memory_static() -> Dict[str, Any]:
    """Static memory inventory for the System Overview: capacity, DIMMs, ceiling.

    Derives every DMI field from the cached dmidecode parse (`_dmi_slots`).
    `max_bandwidth_gb_s` is the theoretical peak (MT/s * data width); None
    without root/DMI.
    """
    mem = psutil.virtual_memory()
    slots = _dmi_slots()
    return {
        "ddr_speeds": _ddr_speeds(slots),
        "total_gb": round(mem.total / (1024 ** 3), 2),
        "swap_total_gb": round(psutil.swap_memory().total / (1024 ** 3), 2),
        "devices": _memory_devices(slots),
        "max_bandwidth_gb_s": _max_bandwidth_gbs(slots),
    }


def _discover_event(device: Path, kind: str, type_id: int, cpu: int) -> _ImcPmu:
    """Build the _ImcPmu for one data_{kind} event on one IMC channel."""
    event_text = (device / "events" / f"data_{kind}").read_text().strip()
    # e.g. "event=0xff,umask=0x10"
    fields = {
        key: int(value, 16)
        for key, value in re.findall(r"(event|umask)=0x([0-9a-fA-F]+)", event_text)
    }
    if "event" not in fields or "umask" not in fields:
        raise ValueError(f"data_{kind} missing event/umask fields")
    unit = (device / "events" / f"data_{kind}.unit").read_text().strip()
    unit_bytes = _UNIT_TO_BYTES.get(unit)
    if unit_bytes is None:
        raise ValueError(f"unrecognized scale unit {unit!r} for data_{kind}")
    scale = float((device / "events" / f"data_{kind}.scale").read_text().strip())
    return _ImcPmu(
        name=f"{device.name}/data_{kind}",
        kind=kind,
        type_id=type_id,
        cpu=cpu,
        config=fields["event"] | (fields["umask"] << 8),
        bytes_per_count=scale * unit_bytes,
    )


def _discover() -> List[_ImcPmu]:
    """Enumerate uncore IMC free-running channels' total/read/write events."""
    pmus: List[_ImcPmu] = []
    try:
        candidates = sorted(Path(_EVENT_SOURCE_DIR).glob("uncore_imc_free_running_*"))
    except OSError:
        return pmus

    for device in candidates:
        try:
            type_id = int((device / "type").read_text().strip())
            # Uncore PMUs are per-socket and must be opened on a CPU that
            # belongs to that socket; cpumask names one such CPU.
            cpu = int((device / "cpumask").read_text().strip().split(",")[0])
        except (OSError, ValueError) as exc:
            logger.debug("Skipping IMC PMU %s: %s", device.name, exc)
            continue

        for kind in _EVENT_KINDS:
            try:
                pmus.append(_discover_event(device, kind, type_id, cpu))
            except (OSError, ValueError) as exc:
                logger.debug("Skipping IMC PMU %s data_%s: %s", device.name, kind, exc)

    return pmus


class Sampler:
    """Owns a set of open IMC counters and turns reads into a bandwidth rate.

    Usage:
        s = Sampler()
        if s.start():
            ...
            bw = s.sample()        # None until a second call establishes a delta
            s.close()

    Each `sample()` consumes the previous reading as its baseline, so one
    Sampler must have exactly one caller.
    """

    def __init__(self) -> None:
        self._pmus: List[_ImcPmu] = []
        self._fds: List[int] = []
        self._last_counts: List[int] = []
        self._last_ts: Optional[float] = None
        self.unavailable_reason: Optional[str] = None
        self.max_bandwidth_gb_s: Optional[float] = None

    def start(self) -> bool:
        """Open the counters. False (with `unavailable_reason` set) if we can't."""
        global _unavailable_warned

        self._pmus = _discover()
        if not self._pmus:
            self.unavailable_reason = (
                f"no uncore_imc_free_running PMU under {_EVENT_SOURCE_DIR}"
            )
            self._warn_once()
            return False

        opened: List[int] = []
        for pmu in self._pmus:
            fd = self._open(pmu)
            if fd < 0:
                for other in opened:
                    os.close(other)
                self._pmus = []
                self._warn_once()
                return False
            opened.append(fd)

        self._fds = opened
        self.max_bandwidth_gb_s = _max_bandwidth_gbs(_dmi_slots())
        try:
            self._last_counts = self._read_counts()
        except OSError as exc:
            self.unavailable_reason = f"reading IMC counters failed: {exc}"
            self.close()
            self._warn_once()
            return False
        self._last_ts = time.monotonic()
        _unavailable_warned = False
        logger.info(
            "Memory bandwidth sampler started on %d IMC channel(s), ceiling=%s GB/s",
            len(self._fds), self.max_bandwidth_gb_s,
        )
        return True

    def _warn_once(self) -> None:
        global _unavailable_warned
        if not _unavailable_warned:
            logger.warning(
                "Memory bandwidth unavailable: %s (suppressing further warnings)",
                self.unavailable_reason,
            )
            _unavailable_warned = True

    def _open(self, pmu: _ImcPmu) -> int:
        attr = _PerfEventAttr()
        attr.type = pmu.type_id
        attr.size = ctypes.sizeof(_PerfEventAttr)
        attr.config = pmu.config
        # pid=-1 + cpu=<n>: system-wide on that CPU, which is the only valid
        # mode for an uncore PMU.
        fd = _libc.syscall(
            ctypes.c_long(_SYS_PERF_EVENT_OPEN),
            ctypes.byref(attr), -1, pmu.cpu, -1, 0,
        )
        if fd < 0:
            err = ctypes.get_errno()
            if err == 13:  # EACCES
                self.unavailable_reason = (
                    "IMC PMU access denied; run as root or lower "
                    "/proc/sys/kernel/perf_event_paranoid"
                )
            else:
                self.unavailable_reason = (
                    f"perf_event_open on {pmu.name} failed: errno={err}"
                )
        return fd

    def _read_counts(self) -> List[int]:
        counts: List[int] = []
        for fd in self._fds:
            raw = os.read(fd, 8)
            counts.append(int.from_bytes(raw, byteorder="little", signed=False))
        return counts

    def sample(self) -> Optional[BandwidthSample]:
        """Bandwidth in GB/s since the previous call, or None if unavailable."""
        if not self._fds or self._last_ts is None:
            return None
        try:
            counts = self._read_counts()
        except OSError as exc:
            logger.debug("IMC counter read failed: %s", exc)
            return None

        now = time.monotonic()
        dt = now - self._last_ts
        if dt <= 0:
            return None

        # max(0, ...) guards the 64-bit wrap; one lost sample beats a negative
        # spike in the median.
        delta_bytes = {kind: 0.0 for kind in _EVENT_KINDS}
        for pmu, previous, current in zip(self._pmus, self._last_counts, counts):
            delta_bytes[pmu.kind] += max(0, current - previous) * pmu.bytes_per_count
        self._last_counts = counts
        self._last_ts = now

        return BandwidthSample(
            total_gb_s=delta_bytes["total"] / dt / 1e9,
            read_gb_s=delta_bytes["read"] / dt / 1e9,
            write_gb_s=delta_bytes["write"] / dt / 1e9,
        )

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        self._last_counts = []
        self._last_ts = None

def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


_dmi_ceiling_done = False
_dmi_ceiling_gb_s: Optional[float] = None


def _dmi_ceiling() -> Optional[float]:
    """Theoretical peak from DMI, looked up once per process (dmidecode is a
    subprocess). Not a sampler: the value is a constant of the installed DIMMs."""
    global _dmi_ceiling_done, _dmi_ceiling_gb_s
    if not _dmi_ceiling_done:
        _dmi_ceiling_done = True
        _dmi_ceiling_gb_s = _max_bandwidth_gbs(_dmi_slots())
    return _dmi_ceiling_gb_s


def _bandwidth_payload(
    pmt: Dict[str, Any], imc: Optional[BandwidthSample], max_gb_s: Optional[float],
) -> Dict[str, Any]:
    """Pick the DRAM source (PMT first, IMC fallback) and build the payload."""
    read = pmt.get("dram_read_gb_s")
    write = pmt.get("dram_write_gb_s")
    source: Optional[str] = None
    if read is not None and write is not None:
        source = "pmt"
    elif imc is not None:
        source = "imc"
        read, write = imc.read_gb_s, imc.write_gb_s
    total = None if source is None else read + write
    return {
        "total_gb_s": None if total is None else round(total, 2),
        "read_gb_s": None if read is None else round(read, 2),
        "write_gb_s": None if write is None else round(write, 2),
        "usage_percent": (round(100.0 * total / max_gb_s, 2)
                          if total is not None and max_gb_s else None),
        "source": source,
        "devices": {"cpu_gb_s": pmt.get("cpu_gb_s"), "gpu_gb_s": pmt.get("gpu_gb_s")},
        "soc_cache_hit_percent": pmt.get("soc_cache_hit_percent"),
        "soc_cache_hit_gb_s": pmt.get("soc_cache_hit_gb_s"),
        "soc_cache_miss_gb_s": pmt.get("soc_cache_miss_gb_s"),
    }


class MemoryBandwidthSampler:
    """Live memory bandwidth as a dashboard-shaped dict.

    DRAM total/read/write come from PMT (TOTAL_READ/WRITE_BANDWIDTH) when the
    platform has it, so they share one snapshot with the CPU/GPU/cache figures;
    otherwise from the uncore IMC PMU.

    The IMC is sampled even while PMT is used (~10 us per call): that keeps its
    delta baseline current, so a fallback mid-run (e.g. PMT attribution disabled
    by its guard) yields a correct first reading.

    A reading is a counter delta, so each consumer owns an instance; sharing one
    would cut the other's sampling window. Not thread-safe on its own.
    """

    def __init__(self, log_sources: bool = False) -> None:
        self._pmt: Optional[PmtBandwidthSampler] = None
        self._imc: Optional[Sampler] = None
        self._log_sources = log_sources
        self.max_bandwidth_gb_s: Optional[float] = None

    def start(self) -> bool:
        """Open whatever sources the platform has. False if neither is usable."""
        pmt = PmtBandwidthSampler()
        self._pmt = pmt if pmt.start() else None
        imc = Sampler()
        self._imc = imc if imc.start() else None
        self.max_bandwidth_gb_s = (self._imc.max_bandwidth_gb_s
                                   if self._imc is not None else _dmi_ceiling())
        return self._pmt is not None or self._imc is not None

    def sample(self) -> Dict[str, Any]:
        """Same shape every call; fields are None on the first call (no baseline)
        or when no source is available."""
        pmt_bw = self._pmt.sample() if self._pmt is not None else None
        imc = self._imc.sample() if self._imc is not None else None
        payload = _bandwidth_payload(bandwidth_fields(pmt_bw), imc, self.max_bandwidth_gb_s)
        if self._log_sources:
            self._log(imc, pmt_bw, payload["source"])
        return payload

    def close(self) -> None:
        if self._imc is not None:
            self._imc.close()
            self._imc = None
        self._pmt = None

    def _log(self, imc: Optional[BandwidthSample], raw: Optional[DeviceBandwidth],
             source: Optional[str]) -> None:
        """Debug aid: IMC PMU vs PMT readings side by side, to compare under load."""
        if imc is None and raw is None:
            return
        dt = self._pmt.last[1] if raw is not None and self._pmt.last else 0.0
        logger.debug(
            "MEMBW source=%s | imc total=%s (r=%s w=%s) | pmt dram=%s (r=%s w=%s) "
            "cpu=%s gpu=%s npu=%s io=%s cache hit=%s miss=%s gpu_idle=%s dt=%.2fs",
            source,
            _fmt(imc.total_gb_s if imc else None),
            _fmt(imc.read_gb_s if imc else None),
            _fmt(imc.write_gb_s if imc else None),
            _fmt(raw.sys_gb_s if raw else None),
            _fmt(raw.sys_read_gb_s if raw else None),
            _fmt(raw.sys_write_gb_s if raw else None),
            _fmt(raw.cpu_gb_s if raw else None), _fmt(raw.gpu_gb_s if raw else None),
            _fmt(raw.npu_gb_s if raw else None), _fmt(raw.io_gb_s if raw else None),
            _fmt(raw.cache_hit_gb_s if raw else None),
            _fmt(raw.cache_miss_gb_s if raw else None),
            raw.gpu_idle if raw else None, dt,
        )


_shared_sampler: Optional[MemoryBandwidthSampler] = None
_shared_sampler_started = False
_shared_lock = threading.Lock()


def get_memory_bandwidth() -> Dict[str, Any]:
    """The dashboard's live memory bandwidth, from one process-wide
    `MemoryBandwidthSampler` (lock-guarded). Other consumers that sample on their
    own schedule (the benchmark sampler) must own their instance instead.
    """
    global _shared_sampler, _shared_sampler_started
    with _shared_lock:
        if not _shared_sampler_started:
            _shared_sampler_started = True
            sampler = MemoryBandwidthSampler(log_sources=True)
            if sampler.start():
                _shared_sampler = sampler
            # Leave _shared_sampler None when no source is usable, so every later
            # call is a cheap no-op returning blanks.
        if _shared_sampler is None:
            return _bandwidth_payload(bandwidth_fields(None), None, _dmi_ceiling())
        return _shared_sampler.sample()


def get_memory_capacity() -> Dict[str, Any]:
    """RAM and swap capacity only; touches no bandwidth sampler."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "usage_percent": round(mem.percent, 2),
        "total_gb": round(mem.total / (1024 ** 3), 2),
        "available_gb": round(mem.available / (1024 ** 3), 2),
        "swap_total_gb": round(swap.total / (1024 ** 3), 2),
        "swap_used_gb": round(swap.used / (1024 ** 3), 2),
        "swap_usage_percent": round(swap.percent, 2),
    }


def get_memory_dynamic() -> Dict[str, Any]:
    """Capacity plus the dashboard's live bandwidth. Advances the shared bandwidth
    sampler, so only the dashboard's collector should call it; anyone else wants
    `get_memory_capacity()`."""
    return {**get_memory_capacity(), "bandwidth": get_memory_bandwidth()}
