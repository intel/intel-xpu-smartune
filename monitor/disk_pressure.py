# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Disk I/O pressure: raw sampling, per-disk USE-based pressure, aggregate, and the
UI-facing busy-ratio.

Model (see docs / design discussion):
  * Per-disk pressure ``P_disk`` in [0, 1] from the USE method -- latency (await),
    saturation (avg queue depth) and utilisation, each squashed by a media-aware
    sigmoid so a value only reads as pressure relative to what that device class can
    sustain (a 20 ms await is normal for an HDD, catastrophic for an NVMe).
  * Aggregate ``disk_combined`` via a noisy-OR of the mean and the worst disk, so a
    single saturated disk dominates without a lone spike pinning the score to 1.0.
  * ``DiskIOMonitor.is_disk_io_stressed`` reports USE-only *saturation*
    (``is_saturated``) -- no task-stall context, and never a throttle decision. The
    authoritative level is produced in the pressure tick, where
    ``PressureAnalyzer.classify_disk_pressure`` gates ``disk_combined`` with the
    (self-inflicted-discounted) io PSI -- see monitor_api._update_pressure_level.

All bands come from ``config.disk_thresholds``, which is separate from the system
``config.thresholds`` and has no fallback to it (see config/config.yaml).
"""

import math
import os
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments
# with shell=False (default). No untrusted shell execution or string
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import time
from typing import Any, Dict, List

import psutil

from config.config import b_config
from utils.logger import logger

# --- Per-disk pressure model constants ---------------------------------------
# These are DEFAULTS. Every one of them can be overridden live from
# ``config.disk_pressure_model`` (see _model()) so the model can be re-calibrated against
# measurements without a code change -- which is the whole point while the shape of the
# model is still being validated on real hardware.
#
# Sub-signal weights. Labels map 1:1, in order, to the metrics below:
#   latency -> await(ms), queue -> avg queue depth (aqu), util -> %util.
# Latency dominates (what users actually feel); util is only a tie-breaker because
# a high-parallelism device can sit at 100% util with plenty of headroom.
_W_LATENCY = 0.55
_W_QUEUE = 0.35
_W_UTIL = 0.10
_SIGMOID_K = 8.0  # steepness; larger = sharper transition around the half-point

# "_half" = the metric value at which that sub-signal reads 0.5 (the sigmoid centre).
# EVERY half-point below is per media class. That is where "busy" is made relative to what
# a device class can sustain -- a 20 ms await is routine for an HDD and catastrophic for an
# NVMe. Because the resulting P_disk is already normalised this way, the *busy* threshold
# applied to it (disk_thresholds.medium) is deliberately media-AGNOSTIC: P_disk = 0.6 means
# "60% of the way to this device class's own pain point" whatever the device is. Making that
# threshold per-media too would apply the media correction twice.
# `unknown` is where a device that matches no rule lands, and it deliberately carries the
# HDD numbers: HDD is the most TOLERANT class (largest half-points), so an unrecognised
# device reads as "not under pressure" instead of being judged by NVMe-class latency and
# reporting saturation permanently. It is also the fallback for a media class missing from
# any map below, so adding a class here without filling in all three maps degrades to HDD
# behaviour rather than raising inside the pressure tick.
MEDIA_CLASSES = ('hdd', 'sata_ssd', 'nvme', 'usb', 'mmc', 'unknown')
# %util that reads as 0.5. Media-dependent because util measures "time with >=1 IO in
# flight", which says nothing about how much concurrency was left: a single-actuator HDD at
# 80% util is near saturation, while an NVMe with a 1023-deep queue can sit at 100% util
# with one IO outstanding and enormous headroom. So the more parallel the device, the higher
# the util reading has to be before it counts.
_UTIL_HALF = {'hdd': 80.0, 'sata_ssd': 85.0, 'nvme': 95.0, 'usb': 80.0,
              'mmc': 80.0, 'unknown': 80.0}
# Below this utilisation (%) a disk is treated as (ramping toward) idle and its await/queue
# sub-signals are scaled down: with almost no IO in flight, a few background ops (a log flush)
# can show multi-ms await that is noise, not pressure. Media-AGNOSTIC on purpose: %util is
# already a normalised time fraction, so "almost nothing was in flight" means the same thing
# on every device -- unlike the half-points, where the same raw latency means opposite things.
_ACTIVITY_UTIL_PCT = 20.0
# await half-points cannot be probed reliably (idle disks issue no IO), so they are
# media-class defaults, in ms.  `usb` covers removable mass storage, whose write latency is
# routinely tens of ms -- judging it by the SATA-SSD 5 ms point would report a healthy thumb
# drive as permanently saturated. `mmc` (eMMC/SD) sits between the two.
_AWAIT_HALF_MS = {'hdd': 20.0, 'sata_ssd': 5.0, 'nvme': 1.0, 'usb': 30.0,
                  'mmc': 10.0, 'unknown': 20.0}
# queue-depth half-points -- the avg queue depth at which the queue sub-signal reads 0.5.
# These are "starts hurting" points, deliberately well below a device's advertised max
# concurrency (which is capacity, not pain): sustained queueing at these depths already means
# work is waiting. Used as the default AND as a ceiling on any device-derived value.
# `usb` bridges typically expose queue_depth=1, so any sustained queue means waiting; `mmc`
# only gained command queueing in eMMC 5.1, so most parts still behave close to depth 1.
_QUEUE_HALF_DEFAULT = {'hdd': 3.0, 'sata_ssd': 16.0, 'nvme': 32.0, 'usb': 1.0,
                       'mmc': 2.0, 'unknown': 3.0}


def _for_media(table: Dict[str, float], media: str) -> float:
    """Per-media lookup that falls back to ``unknown`` (== HDD) for an uncovered class."""
    value = table.get(media)
    return table['unknown'] if value is None else value


# {disk_name: media_class}. Cached forever because it describes the hardware; a disk that
# appears later (hotplug) simply misses once and is probed then.
_DISK_MEDIA: Dict[str, str] = {}


def media_for_disk(disk: str) -> str:
    """Media class of a whole disk, probed from sysfs and cached.

    Module-level rather than a :class:`DiskIOMonitor` method because the throttle path
    needs it too -- an io.max cap has to be sized against what the device can deliver,
    and it should not have to own a pressure monitor to ask.
    """
    media = _DISK_MEDIA.get(disk)
    if media is None:
        rotational = _read_int(f"/sys/block/{disk}/queue/rotational")
        removable = _read_int(f"/sys/block/{disk}/removable")
        # The symlink target carries the bus topology, e.g.
        # ../devices/pci.../usb1/1-2/1-2:1.0/host6/... -- the only reliable way to spot a
        # USB bridge that reports itself as a plain non-rotational SCSI disk.
        dev_path = os.path.realpath(f"/sys/block/{disk}")
        media = _classify_media(disk, rotational, removable, dev_path)
        _DISK_MEDIA[disk] = media
        logger.debug(f"disk media {disk}: {media} (rotational={rotational} "
                     f"removable={removable} path={dev_path})")
    return media


def _classify_media(disk: str, rotational, removable, dev_path: str) -> str:
    """Media class from sysfs facts. Pure function so it is testable without real devices.

    Order matters. Removable/USB is checked BEFORE the rotational flag because USB mass
    storage reports ``rotational=0`` and would otherwise be classed as a SATA SSD and held
    to SSD-class latency -- the single worst misclassification available here, since the
    two differ by an order of magnitude in what counts as slow. It is also checked before
    ``mmcblk`` so that a card in a reader is judged by the slower removable numbers, while
    a soldered-down eMMC gets the ``mmc`` ones.

    Anything whose ``rotational`` flag could not be read (virtio, device-mapper, md) is
    ``unknown`` rather than a guess -- see the note at :data:`MEDIA_CLASSES` for why that
    class carries the HDD numbers.

    :param disk: kernel device name (e.g. "nvme0n1", "sda", "vdb").
    :param rotational: ``queue/rotational`` as int, or None when unreadable.
    :param removable: ``removable`` as int, or None when unreadable.
    :param dev_path: resolved sysfs path of the device, matched for a USB ancestor.
    """
    if disk.startswith('nvme'):
        return 'nvme'
    if removable == 1 or '/usb' in (dev_path or '').lower():
        return 'usb'
    if disk.startswith('mmcblk'):
        return 'mmc'
    if rotational == 1:
        return 'hdd'
    if rotational == 0:
        return 'sata_ssd'  # non-rotational, non-NVMe, non-removable, non-eMMC
    return 'unknown'

# noisy-OR: how strongly the single worst disk dominates the aggregate (0..1).
_MAX_P_WEIGHT = 0.8


def _sigmoid_pressure(x: float, x_half: float, k: float = _SIGMOID_K) -> float:
    """Logistic squash to [0, 1]: 0.5 at ``x == x_half``, rising above it. Continuous
    and differentiable, so the derived pressure does not step across a hard threshold."""
    if x_half <= 0:
        return 0.0
    z = -k * (x - x_half) / x_half
    z = max(-60.0, min(60.0, z))  # guard exp() against overflow
    return 1.0 / (1.0 + math.exp(z))


def _read_int(path: str):
    """Read a single integer from a sysfs file, or None if unavailable/unparsable."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# {"maj:min": "nvme0n1"} for every block device, cached because it describes the
# hardware. Rebuilt on a lookup miss rather than on a timer, which is what makes
# hotplug work: a USB disk plugged in after start-up misses once, triggers a rebuild,
# and is resolved from then on.
_DEVNO_TO_DISK: Dict[str, str] = {}


def _build_devno_map() -> Dict[str, str]:
    """Scan ``/sys/block`` for ``maj:min`` -> whole-disk name.

    Partitions are mapped to their PARENT disk, not to themselves. cgroup ``io.stat``
    charges I/O to the whole-disk devno on current kernels (verified: writing to
    ``/tmp`` on ``nvme0n1p2`` shows up under ``259:0``), but a kernel that charged the
    partition would otherwise produce a device this code cannot name -- and the two
    consumers of the name, ``io.max`` and the media classification, are both whole-disk
    concepts anyway.
    """
    mapping: Dict[str, str] = {}
    try:
        disks = os.listdir('/sys/block')
    except OSError as e:
        logger.debug("cannot list /sys/block: %s", e)
        return mapping

    for disk in disks:
        base = f"/sys/block/{disk}"
        try:
            with open(f"{base}/dev") as f:
                mapping[f.read().strip()] = disk
        except OSError:
            continue
        # Partitions are the subdirectories carrying their own `dev` file.
        try:
            for entry in os.listdir(base):
                try:
                    with open(f"{base}/{entry}/dev") as f:
                        mapping[f.read().strip()] = disk  # parent disk, deliberately
                except OSError:
                    continue
        except OSError:
            continue
    return mapping


def disk_name_for_devno(devno: str) -> str:
    """Kernel disk name for a ``"maj:min"`` string, or the devno itself if unknown.

    Returning the raw devno instead of None keeps it usable as a dict key downstream:
    an unnameable device still needs to be counted, just not classified.
    """
    global _DEVNO_TO_DISK
    name = _DEVNO_TO_DISK.get(devno)
    if name is None:
        _DEVNO_TO_DISK = _build_devno_map()
        name = _DEVNO_TO_DISK.get(devno)
    return name or devno


class DiskIOMonitor:
    """Sample per-disk I/O and derive a USE-based pressure per disk and in aggregate."""

    def _model(self) -> Dict[str, Any]:
        """Live view of ``config.disk_pressure_model``, merged over the module defaults.

        Read on every use rather than cached so a value edited in config.yaml takes effect
        on the next tick -- these are calibration knobs, and needing a service restart to
        try one makes calibration painful. Any key may be omitted; only what is present
        overrides. Missing/malformed entries fall back silently to the default, so a typo
        degrades to shipped behaviour instead of crashing the pressure loop.
        """
        cfg = getattr(self.config, 'disk_pressure_model', None) or {}

        def _num(key, default):
            v = cfg.get(key)
            return float(v) if isinstance(v, (int, float)) else default

        def _media_map(key, defaults):
            """Resolve a per-media half-point map. A bare number is accepted and applied to
            every media class -- these keys were device-agnostic scalars before, and an
            operator writing one number should get that number, not a silent fallback."""
            v = cfg.get(key)
            if isinstance(v, (int, float)):
                return {m: float(v) for m in defaults}
            if not isinstance(v, dict):
                return defaults
            return {m: (float(v[m]) if isinstance(v.get(m), (int, float)) else d)
                    for m, d in defaults.items()}

        sw = cfg.get('sub_weights') if isinstance(cfg.get('sub_weights'), dict) else {}

        def _w(key, default):
            v = sw.get(key)
            return float(v) if isinstance(v, (int, float)) else default

        return {
            'w_latency': _w('latency', _W_LATENCY),
            'w_queue': _w('queue', _W_QUEUE),
            'w_util': _w('util', _W_UTIL),
            'sigmoid_k': _num('sigmoid_k', _SIGMOID_K),
            'util_half_pct': _media_map('util_half_pct', _UTIL_HALF),
            'activity_util_pct': _num('activity_util_pct', _ACTIVITY_UTIL_PCT),
            'await_half_ms': _media_map('await_half_ms', _AWAIT_HALF_MS),
            'queue_half': _media_map('queue_half', _QUEUE_HALF_DEFAULT),
            'max_p_weight': _num('max_p_weight', _MAX_P_WEIGHT),
        }

    def __init__(self, config=None):
        self.config = config or b_config
        self.prev_io = psutil.disk_io_counters(perdisk=True)
        # field 14 of /proc/diskstats (weighted time doing IO, ms) -- the basis for a
        # true iostat aqu-sz; psutil does not expose it.
        self.prev_weighted = self._read_diskstats_weighted()
        self.prev_time = time.time()
        # Per-disk media/half-point profile, probed once from sysfs and cached (never
        # re-read on the hot path).
        self._profiles: Dict[str, Dict[str, Any]] = {}

    def get_physical_disks(self) -> List[str]:
        """Return a list of all physical disk device names."""
        cmd = ["lsblk", "-d", "-o", "NAME,TYPE", "-n"]
        try:
            output = subprocess.check_output(cmd, text=True).strip()

            disks = []
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "disk":
                    disks.append(parts[0])

            return disks

        except subprocess.CalledProcessError:
            return []

    @staticmethod
    def _read_diskstats_weighted() -> Dict[str, int]:
        """Return {disk: weighted_io_ms} from /proc/diskstats field 14 (time_in_queue).

        This weighted busy time, divided by the wall interval, yields the average queue
        depth (iostat aqu-sz) -- the honest saturation signal. Missing/short lines are
        skipped so a malformed row never breaks the sample.
        """
        result: Dict[str, int] = {}
        try:
            with open('/proc/diskstats') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 14:
                        try:
                            result[parts[2]] = int(parts[13])  # field 14 (0-indexed 13)
                        except ValueError:
                            continue
        except OSError:
            pass
        return result

    def _disk_profile(self, disk: str) -> Dict[str, Any]:
        """Media class and sigmoid half-points for a disk.

        Only the sysfs probes are cached (media class and the device's advertised queue
        depth) -- those describe the hardware and cannot change at runtime. The half-points
        themselves are resolved from :meth:`_model` on every call so a config edit takes
        effect on the next tick instead of after a restart.

        Media is detected dynamically by :func:`_classify_media`; the queue half-point
        prefers the device's own advertised concurrency (``device/queue_depth``) and falls
        back to a media-class default. await/util half-points are always media-class
        defaults (idle disks can't be measured).
        """
        probe = self._profiles.get(disk)
        if probe is None:
            probe = {
                'media': media_for_disk(disk),
                # device/queue_depth is the hardware's max concurrency (capacity), not the
                # depth at which latency starts to suffer -- on NVMe it can be 128-1023.
                'queue_depth': _read_int(f"/sys/block/{disk}/device/queue_depth"),
            }
            self._profiles[disk] = probe
            logger.debug(f"disk probe {disk}: {probe}")

        model = self._model()
        media = probe['media']
        if media == 'hdd':
            # A single actuator: any sustained queue means waiting, regardless of the
            # block layer's advertised depth.
            queue_half = _for_media(model['queue_half'], 'hdd')
        else:
            # Use the advertised depth only to make a low-concurrency device MORE
            # sensitive, never to push the half-point above the media pain ceiling. A
            # genuine depth of 1 (USB bridges, some virtual disks) counts: granting it the
            # class default would judge a device that can hold one request in flight as if
            # it could hold sixteen.
            qd = probe['queue_depth']
            default = _for_media(model['queue_half'], media)
            queue_half = min(float(qd), default) if qd and qd >= 1 else default

        return {
            'media': media,
            'await_half_ms': _for_media(model['await_half_ms'], media),
            'queue_half': queue_half,
            'util_half_pct': _for_media(model['util_half_pct'], media),
            'activity_util_pct': model['activity_util_pct'],
        }

    def _collect_disk_io_stats(self) -> dict:
        """
        Collect raw per-disk IO statistics for all disks: utilisation, read/write
        speed, IOPS, plus the derived ``await_ms`` (avg latency) and ``aqu`` (avg queue
        depth) used by the pressure model. For internal use.
        :return:
        {
            "disk_io": {
                "nvme0n1": {
                    "utilization": 45.2,
                    "read_kb_per_sec": 1024.0,
                    "write_kb_per_sec": 512.0,
                    "read_iops": 128.0,
                    "write_iops": 64.0,
                    "await_ms": 0.4,
                    "aqu": 1.2,
                },
                ...
            }
        }
        """
        disks = self.get_physical_disks()
        curr_io = psutil.disk_io_counters(perdisk=True)
        curr_weighted = self._read_diskstats_weighted()
        curr_time = time.time()

        prev_io = self.prev_io if isinstance(self.prev_io, dict) else {}
        prev_weighted = self.prev_weighted if isinstance(self.prev_weighted, dict) else {}
        time_elapsed = curr_time - self.prev_time

        merged_result = {}
        for disk in disks:
            curr = curr_io.get(disk)
            prev = prev_io.get(disk)
            if not curr or not prev or time_elapsed <= 0:
                merged_result[disk] = {
                    'utilization': 0.0,
                    'read_kb_per_sec': 0.0,
                    'write_kb_per_sec': 0.0,
                    'read_iops': 0.0,
                    'write_iops': 0.0,
                    'await_ms': 0.0,
                    'aqu': 0.0,
                }
                continue

            read_kb = (curr.read_bytes - prev.read_bytes) / 1024
            write_kb = (curr.write_bytes - prev.write_bytes) / 1024
            read_kb_per_sec = max(0.0, read_kb / time_elapsed)
            write_kb_per_sec = max(0.0, write_kb / time_elapsed)
            read_ops = curr.read_count - prev.read_count
            write_ops = curr.write_count - prev.write_count
            read_iops = max(0.0, read_ops / time_elapsed)
            write_iops = max(0.0, write_ops / time_elapsed)

            # Prefer device busy_time/io_time if available; fallback to read+write time.
            prev_busy = getattr(prev, 'busy_time', None)
            curr_busy = getattr(curr, 'busy_time', None)
            if prev_busy is None or curr_busy is None:
                prev_busy = getattr(prev, 'io_time', None)
                curr_busy = getattr(curr, 'io_time', None)

            if prev_busy is not None and curr_busy is not None:
                busy_delta_ms = curr_busy - prev_busy
            else:
                busy_delta_ms = (curr.read_time - prev.read_time) + (curr.write_time - prev.write_time)

            utilization = min(100.0, max(0.0, 100.0 * busy_delta_ms / (time_elapsed * 1000.0)))

            # await = avg service latency per IO (ms). read_time/write_time are the
            # cumulative ms fields 7/11 of /proc/diskstats; their delta over the ops
            # completed in the interval is the per-IO latency.
            io_time_delta_ms = max(0.0, (curr.read_time - prev.read_time) + (curr.write_time - prev.write_time))
            ops_delta = read_ops + write_ops
            await_ms = io_time_delta_ms / ops_delta if ops_delta > 0 else 0.0

            # aqu = avg queue depth = Δ(weighted io ms) / Δt (Little's law). Uses the
            # true time_in_queue field; falls back to 0 when diskstats is unavailable.
            weighted_delta_ms = max(0.0, curr_weighted.get(disk, 0) - prev_weighted.get(disk, 0))
            aqu = weighted_delta_ms / (time_elapsed * 1000.0) if time_elapsed > 0 else 0.0

            merged_result[disk] = {
                'utilization': round(utilization, 2),
                'read_kb_per_sec': round(read_kb_per_sec, 2),
                'write_kb_per_sec': round(write_kb_per_sec, 2),
                'read_iops': round(read_iops, 2),
                'write_iops': round(write_iops, 2),
                'await_ms': round(await_ms, 3),
                'aqu': round(aqu, 3),
            }

        self.prev_io = curr_io
        self.prev_weighted = curr_weighted
        self.prev_time = curr_time
        return {'disk_io': merged_result}

    def _disk_pressure(self, disk: str, stats: dict) -> float:
        """Per-disk USE pressure ``P_disk`` in [0, 1] from await/queue/util."""
        prof = self._disk_profile(disk)
        m = self._model()
        k = m['sigmoid_k']
        f_lat = _sigmoid_pressure(stats['await_ms'], prof['await_half_ms'], k)
        f_queue = _sigmoid_pressure(stats['aqu'], prof['queue_half'], k)
        f_util = _sigmoid_pressure(stats['utilization'], prof['util_half_pct'], k)
        # await/aqu are only trustworthy when the disk is actually busy. A near-idle disk with a
        # few multi-ms background ops must NOT read as pressure -- on a 1 ms-half-point NVMe that
        # alone would pin the latency term. Utilisation is a weak HIGH-end pressure signal (a
        # parallel device saturates util with headroom) but a reliable LOW-end activity gate:
        # scale latency/queue by it so an idle disk reads ~0 regardless of per-op latency.
        act_half = prof['activity_util_pct']
        activity = min(1.0, stats['utilization'] / act_half) if act_half > 0 else 1.0
        return activity * (m['w_latency'] * f_lat + m['w_queue'] * f_queue) + m['w_util'] * f_util

    def evaluate(self) -> dict:
        """USE-based per-disk pressure and noisy-OR aggregate. No PSI / task-stall
        context -- this is the raw disk-subsystem saturation, consumed both by the
        USE-only ``is_disk_io_stressed`` fallback and, in the pressure tick, by the
        PSI gate that decides the final level.

        :return:
            {
                "disk_combined": float,   # noisy-OR aggregate in [0, 1]
                "max_p": float,           # worst single disk
                "avg_p": float,           # mean across disks
                "stressed_disks": [str],  # disks whose P_disk reached the busy band
                "details": {disk: {<raw stats>, pressure, disk_type, is_busy}}
            }
        """
        disk_stats = self._collect_disk_io_stats()["disk_io"]
        # Disk bands only -- never the system `thresholds` (see config.disk_thresholds).
        busy_p = self.config.disk_thresholds['medium']

        scored = []
        for disk, stats in disk_stats.items():
            p = self._disk_pressure(disk, stats)
            prof = self._disk_profile(disk)
            scored.append((p, disk, {**stats, 'pressure': round(p, 4),
                                     'disk_type': prof['media'], 'is_busy': p >= busy_p}))

        # Busiest disk first, everywhere: this ordering flows into the log line, the API
        # payload and the UI list. Alphabetical order buries the one disk that matters
        # behind idle ones (sda, sdb, then the saturated nvme0n1).
        scored.sort(key=lambda e: -e[0])
        details = {disk: d for _, disk, d in scored}
        pressures = [p for p, _, _ in scored]
        stressed_disks = [disk for _, disk, d in scored if d['is_busy']]

        if pressures:
            avg_p = sum(pressures) / len(pressures)
            max_p = max(pressures)
            disk_combined = 1.0 - (1.0 - avg_p) * (1.0 - max_p * self._model()['max_p_weight'])
        else:
            avg_p = max_p = disk_combined = 0.0

        # Not logged here: `details` is handed to classify_disk_pressure, which prints the
        # sub-signals and the level it derived from them as a single [disk-level] line.
        return {
            'disk_combined': round(disk_combined, 4),
            'max_p': round(max_p, 4),
            'avg_p': round(avg_p, 4),
            'stressed_disks': stressed_disks,
            'details': details,
        }

    def is_disk_io_stressed(self, device: str = None, threshold: float = None) -> dict:
        """USE-only disk-IO **saturation** report (no task-stall/PSI context).

        The returned flag is named ``is_saturated``, not ``is_stressed``, on purpose: it
        says the disk subsystem is working near its limit, NOT that the system is being
        hurt or that anything should be throttled. Throttling is decided exclusively by
        the PSI-gated level from ``PressureAnalyzer.classify_disk_pressure`` in the
        pressure tick, which can read *lower* than this (a saturated but non-stalling
        disk) or *higher* (saturation plus a system-wide stall).

        Used by callers outside the pressure tick (e.g. the dynamic-info fallback when the
        pressure monitor is unavailable). ``threshold`` is accepted for a
        backward-compatible signature and ignored (util is now one sub-signal, not a gate).

        :param device: restrict the report to a single disk when given.
        :return:
            {
                "is_saturated": bool,
                "stressed_disks": list[str],
                "disk_combined": float,
                "details": {disk: {..., pressure, disk_type, is_busy}}
            }
        """
        ev = self.evaluate()

        details = ev['details']
        stressed_disks = ev['stressed_disks']
        disk_combined = ev['disk_combined']
        if device:
            details = {device: details[device]} if device in details else {}
            stressed_disks = [d for d in stressed_disks if d == device]
            disk_combined = details.get(device, {}).get('pressure', 0.0)

        high = self.config.disk_thresholds['high']

        return {
            "is_saturated": disk_combined >= high,
            "stressed_disks": stressed_disks,
            "disk_combined": round(disk_combined, 4),
            "details": details,
        }


def compute_disk_pressure(disk_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate the per-disk view for the dashboard.

    Severity and breadth are reported separately:
      * ``busy_level`` follows the PSI-gated disk-IO level from the pressure tick
        (``disk_stats['level']``) when available -- so "one saturated disk stalling the
        system" reads as HIGH/CRITICAL even though only one of many disks is busy. It
        falls back to the busy-disk *ratio* banding only when no gated level is present
        (e.g. the USE-only path with no pressure tick).
      * ``busy_ratio`` / ``busy_pct`` / ``busy_disks`` describe *how many* disks are
        affected (breadth), independent of severity.
    """
    disk_io = disk_stats.get('disk_io')
    if not isinstance(disk_io, dict) or not disk_io:
        return {
            "busy_disks": [],
            "total_disks": 0,
            "busy_ratio": None,
            "busy_pct": None,
            "busy_level": "NO DATA",
            "pressure_pct": None,
        }

    busy_disks: List[str] = []
    total_disks = 0
    for disk_name, detail in disk_io.items():
        if not isinstance(detail, dict):
            continue
        total_disks += 1
        if detail.get("is_busy"):
            busy_disks.append(disk_name)

    busy_count = len(busy_disks)
    busy_ratio = busy_count / total_disks if total_disks > 0 else None
    busy_pct = busy_ratio * 100.0 if busy_ratio is not None else None

    gated_level = disk_stats.get('level')
    gated_score = disk_stats.get('score')
    if gated_level and gated_level != "unknown":
        # Severity from the PSI-gated tick level.
        busy_level = gated_level.upper()
    elif total_disks == 0 or busy_ratio is None:
        busy_level = "NO DATA"
    else:
        # Fallback: band the busy-disk ratio (no gated level available).
        _th = b_config.disk_thresholds
        if busy_ratio < _th.get("low", 0.4):
            busy_level = "LOW"
        elif busy_ratio < _th.get("medium", 0.6):
            busy_level = "MEDIUM"
        elif busy_ratio < _th.get("high", 0.8):
            busy_level = "HIGH"
        else:
            busy_level = "CRITICAL"

    return {
        "busy_disks": busy_disks,
        "total_disks": total_disks,
        "busy_ratio": round(busy_ratio, 4) if busy_ratio is not None else None,
        "busy_pct": round(busy_pct, 2) if busy_pct is not None else None,
        "busy_level": busy_level,
        "pressure_pct": round(gated_score * 100.0, 2) if isinstance(gated_score, (int, float)) else None,
    }
