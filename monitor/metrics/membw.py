# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""System memory bandwidth via the uncore IMC free-running counters.

This is the one hardware metric SmarTune's other collectors do not provide:
`cpu.get_memory_dynamic()` reports capacity (used / available / percent), which
says nothing about how hard the memory controller is actually working.  For
model benchmarking that distinction matters -- a quantized model that fits in
cache and one that streams weights from DRAM can show identical utilization and
wildly different bandwidth.

Reads `uncore_imc_free_running_*/events/data_total` directly through
perf_event_open.  These are free-running counters: they always tick, so there is
no enable/disable and no per-process attribution -- the reading is system-wide,
which is what a benchmark host wants.

Requires either root or `/proc/sys/kernel/perf_event_paranoid <= 0`.  When the
PMU is absent (non-Intel, or a kernel that does not expose it) or permission is
denied, `Sampler.start()` returns False and `sample()` yields None; callers are
expected to record a blank rather than fail.

Not wired into the System Overview API yet -- `benchmark/service/sampler.py` is the only
consumer.  The shape here (explicit start/sample/close on an instance the caller
owns) is deliberate: unlike the module-level collectors in this package, a
bandwidth reading is a counter delta, so two callers sharing one instance would
each consume the other's baseline.
"""

import ctypes
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from utils.logger import logger

# x86_64. This module is Intel-uncore-specific anyway, so there is no other arch
# to care about.
_SYS_PERF_EVENT_OPEN = 298

_EVENT_SOURCE_DIR = "/sys/bus/event_source/devices"

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
    """One uncore IMC channel's free-running data_total counter."""

    name: str
    type_id: int
    cpu: int
    config: int
    # Bytes represented by one counter tick (from the PMU's .scale attribute,
    # expressed in MiB by the kernel).
    scale_mib: float


def estimate_max_bandwidth_gbs() -> Optional[float]:
    """Theoretical peak DRAM bandwidth in GB/s, from DMI.

    MT/s * total_data_width_bytes / 1000.  Used to express a live reading as a
    percentage of what the hardware can do -- 40 GB/s means nothing without
    knowing whether the ceiling is 50 or 500.

    Needs `dmidecode`, which needs root. Returns None when unavailable rather
    than 0.0, so callers can tell "no ceiling known" from "no bandwidth".
    """
    try:
        result = subprocess.run(
            ["dmidecode", "-t", "memory"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("dmidecode unavailable for bandwidth ceiling: %s", exc)
        return None

    text = result.stdout or ""
    if not text:
        return None

    # "Configured Memory Speed" is the rate actually in use; plain "Speed" is
    # the module's rating and overstates a downclocked DIMM.
    speeds = [int(v) for v in re.findall(r"Configured Memory Speed:\s+(\d+)\s+MT/s", text)]
    if not speeds:
        speeds = [int(v) for v in re.findall(r"Speed:\s+(\d+)\s+MT/s", text)]
    # Empty slots report "Data Width: 0 bits" (or nothing at all).
    widths_bits = [int(v) for v in re.findall(r"Data Width:\s+(\d+)\s+bits", text)]
    widths_bits = [v for v in widths_bits if v > 0]

    if not speeds or not widths_bits:
        return None

    total_width_bytes = sum(widths_bits) / 8.0
    if total_width_bytes <= 0:
        return None
    return round(max(speeds) * total_width_bytes / 1000.0, 2)


def _discover() -> List[_ImcPmu]:
    """Enumerate uncore IMC free-running channels exposing a data_total event."""
    pmus: List[_ImcPmu] = []
    try:
        candidates = sorted(Path(_EVENT_SOURCE_DIR).glob("uncore_imc_free_running_*"))
    except OSError:
        return pmus

    for device in candidates:
        try:
            event_text = (device / "events" / "data_total").read_text().strip()
            # e.g. "event=0xff,umask=0x10"
            fields = {
                key: int(value, 16)
                for key, value in re.findall(r"(event|umask)=0x([0-9a-fA-F]+)", event_text)
            }
            if "event" not in fields or "umask" not in fields:
                continue
            pmus.append(_ImcPmu(
                name=device.name,
                type_id=int((device / "type").read_text().strip()),
                # Uncore PMUs are per-socket and must be opened on a CPU that
                # belongs to that socket; cpumask names one such CPU.
                cpu=int((device / "cpumask").read_text().strip().split(",")[0]),
                config=fields["event"] | (fields["umask"] << 8),
                scale_mib=float((device / "events" / "data_total.scale").read_text().strip()),
            ))
        except (OSError, ValueError) as exc:
            logger.debug("Skipping IMC PMU %s: %s", device.name, exc)
            continue

    return pmus


class Sampler:
    """Owns a set of open IMC counters and turns reads into a bandwidth rate.

    Usage:
        s = Sampler()
        if s.start():
            ...
            gb_s = s.sample()      # None until a second call establishes a delta
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
        self.max_bandwidth_gb_s = estimate_max_bandwidth_gbs()
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

    def sample(self) -> Optional[float]:
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
        delta_mib = sum(
            max(0, current - previous) * pmu.scale_mib
            for pmu, previous, current in zip(self._pmus, self._last_counts, counts)
        )
        self._last_counts = counts
        self._last_ts = now

        return (delta_mib * 1024 * 1024) / dt / 1e9

    def close(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
        self._last_counts = []
        self._last_ts = None
