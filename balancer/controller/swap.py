# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""L0: swap headroom.  Fuel for the L2 reclaim path.

memory.reclaim can only move anonymous pages into swap, so with swap full (or
memory.swap.max=0) the write returns EAGAIN having done nothing and the whole
of L2 quietly stops working.  This module keeps that from happening.

Two lines of defence, in order:

1. zswap -- a compressed pool held *in RAM*.  Costs no disk writes and catches
   warm pages in microseconds.  Net gain is ``pool x (ratio - 1)``, not 1:1,
   because the pool itself occupies RAM.
2. A swapfile -- true 1:1 relief, and the only thing that helps when the pool
   is full.  Grown ahead of time, because growing it mid-crisis is too late.

Growth normally triggers on *two* watermarks together, well before swap is
exhausted: allocating the file is cheap, so there is no reason to wait.

Exhausted swap alone is also sufficient, and that exception is load-bearing:
the two signals are *anti-correlated*, because every anonymous page swap
absorbs stops counting towards used_eff.  Swap doing its job is precisely what
keeps the memory watermark from being crossed, so an AND gate cannot fire in
the one scenario it exists for -- full swap at moderate memory use.

Watermarks are evaluated against the *worst* sample in a short window, not the
instant one.  Memory under a bursty allocator swings far faster than any poll
interval, so an instantaneous read systematically misses the troughs -- and the
troughs are what OOM-kill.

Every threshold is a ratio of MemTotal or of the disk, never an absolute byte
count, so the same config behaves sensibly on a 4 GB box and a 128 GB one.

Nothing is persisted.  /etc/fstab is never written and the distro's own swap
device is never touched, so a reboot restores the machine exactly;
``release()`` does the same without one.  Every method that changes the system
takes ``dry_run`` and defaults it to True.
"""

import os
import re
import subprocess  # nosec - fixed argv lists, never a shell string
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable, Deque, Dict, List, Optional, Tuple

from utils.logger import get_logger
logger = get_logger(__name__)

MB = 1024 * 1024
GB = 1024 * MB

PROC_MEMINFO = "/proc/meminfo"
PROC_SWAPS = "/proc/swaps"
PROC_MOUNTS = "/proc/mounts"
ZSWAP_PARAMS = "/sys/module/zswap/parameters"

# Filesystems where a swapfile can be grown dynamically.
#
# fallocate on ext4/xfs returns in milliseconds and writes no data, which is
# what makes growth-under-pressure safe.  btrfs needs nodatacow (chattr +C) or
# swapon refuses the file; zfs swapfiles can deadlock the pool under memory
# pressure -- exactly the situation we would be in.  Anything unrecognised is
# treated as unsafe.
DYNAMIC_GROWTH_FS = frozenset({"ext4", "xfs"})
CONDITIONAL_GROWTH_FS = frozenset({"btrfs"})

# Managed swapfiles are named swap-01.img, swap-02.img, ...  Anything in the
# managed directory that does not match is left alone: this pattern is the only
# thing that makes a file ours to swapoff and delete.
MANAGED_NAME_RE = re.compile(r"^swap-(\d{2,})\.img$")
MANAGED_NAME_FMT = "swap-%02d.img"

# Swap priority is NOT something we can choose: swapon(8) only sets
# SWAP_FLAG_PREFER for a *non-negative* priority (SWAP_FLAG_PRIO_MASK cannot
# encode a negative one), so a negative argument is silently dropped and the
# kernel auto-assigns. Our files therefore come up at the same priority as the
# distro's /swap.img and the kernel round-robins across them, interleaving our
# pages with theirs. Two accepted consequences:
#   - swapoff of our file also faults back pages that were never ours. Still
#     bounded and still gated on headroom, just not surgical.
#   - Ownership is decided by directory + MANAGED_NAME_RE, never by priority,
#     so /swap.img remains structurally ineligible for swapoff.
# The real priority is read back from /proc/swaps after swapon and logged. There
# is deliberately no MANAGED_SWAP_PRIORITY constant -- no value would be honoured.

# swapoff has to fault every page back into RAM, so it needs headroom and it can
# take a while.  Both numbers are guards against turning a shrink into an OOM.
SWAPOFF_HEADROOM_RATIO = 1.2
SWAPOFF_TIMEOUT_SEC = 300

# When to give a grown file back.  Deliberately constants and not config keys:
# they are two ends of one hysteresis loop, and an operator who set them
# independently would mostly succeed in making growth and shrink fight.
#
# The shrink watermark sits a full band below the growth one.  Shrinking at the
# same level growth fires at would produce exactly the oscillation the window
# sampling exists to prevent: grow, drop under the line, shrink, cross it again.
SHRINK_HYSTERESIS = 0.15        # shrink below (grow_mem_watermark - this)
SHRINK_IDLE_SEC = 600.0         # ...and only after this long uninterrupted

# Predictive growth.  The two level-based arms below ask "is swap nearly gone
# yet", a question that can only be answered too late -- swap can drain from
# nearly free to nearly full in well under a minute, and the kill lands before
# either arm is true.  The slope arm asks the useful question instead -- "at the
# rate it is draining, how long until it is gone" -- off the same window the
# other arms already sample.
#
# 90 s of lead: enough to cover the monitor loop blocking under critical plus
# the urgent rate limiter, doubled to leave room for a drain that accelerates.
# fallocate costs milliseconds, so erring long is nearly free and erring short
# costs an OOM kill.
GROW_LEAD_SEC = 90.0
# Below this much window span a "slope" is two adjacent samples and noise.
MIN_SLOPE_SPAN_SEC = 5.0
# Memory climbing this fraction of MemTotal within the window means something
# just started allocating hard.  Consumed by the balancer to warm the
# top-consumer cache before the pressure level itself moves -- see
# mem_rise_rate().  0.10 replays as a trigger 15 s ahead of the kill.
MEM_RISE_TRIGGER = 0.10

# 64 MB write chunks for the dd-equivalent fallback path.
FALLBACK_CHUNK = 64 * MB

SWAP_GROWTH_DEFAULTS = {
    "enabled": False,
    # Both watermarks must be crossed.  80% memory alone is normal on a NAS with
    # a healthy page cache; it only matters when swap is also running out.
    "grow_mem_watermark": 0.80,
    "grow_swap_watermark": 0.50,
    # Swap this close to full grows on its own, whatever memory looks like: it
    # is the state that makes memory.reclaim return EAGAIN, and swap absorbing
    # pages is what keeps the memory watermark from being crossed.
    "grow_swap_critical": 0.10,
    # Watermarks are tested against the worst sample in this many seconds, not
    # against the instant read, because memory swings faster than we poll.
    "pressure_window_sec": 30,
    "step_ratio": 0.25,       # one growth step = MemTotal x this
    "max_swap_ratio": 2.0,    # total swap never exceeds MemTotal x this
    "disk_reserve_pct": 10,   # keep the larger of this share of the disk...
    "disk_reserve_min_gb": 20,  # ...and this floor
    "zswap": False,
    "zswap_compressor": "zstd",  # kernel default lzo is ~2:1, zstd ~3:1
    "zswap_pool_percent": 20,
    "path": "/var/lib/smartune/swap",
    "allow_fs": ["ext4", "xfs"],
}


@dataclass(frozen=True)
class MemorySnapshot:
    """The /proc/meminfo fields this module reasons about, in bytes."""
    mem_total: int = 0
    mem_free: int = 0
    mem_available: int = 0
    anon_pages: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def used_effective(self) -> int:
        """Memory that is genuinely unavailable.

        MemTotal - MemAvailable, *not* MemTotal - MemFree: MemAvailable already
        counts reclaimable page cache as available, which is why dropping caches
        barely moves this number while destroying NAS read performance.
        """
        return max(0, self.mem_total - self.mem_available)

    @property
    def used_fraction(self) -> float:
        return self.used_effective / self.mem_total if self.mem_total else 0.0

    @property
    def swap_used(self) -> int:
        return max(0, self.swap_total - self.swap_free)

    @property
    def swap_free_fraction(self) -> float:
        """Share of swap still free.

        With no swap configured at all this is 0.0, not 1.0 -- no swap is the
        most swap-starved state there is, and it must trip the watermark.
        """
        return self.swap_free / self.swap_total if self.swap_total else 0.0

    @property
    def movable_anon(self) -> int:
        """Anonymous memory not already parked in swap: the reclaim ceiling.

        When the shortfall is in slab or page tables instead, this is small and
        no amount of swap helps -- reclaim would just burn the caller's time.
        """
        return max(0, self.anon_pages - self.swap_used)


@dataclass(frozen=True)
class GrowthPlan:
    """What SwapController decided, and every number behind it.

    Built even when nothing will be done, so the dry-run log can be compared
    against what a live run would have done.
    """
    should_grow: bool = False
    reason: str = ""
    grow_bytes: int = 0
    step_bytes: int = 0
    ratio_cap_bytes: int = 0    # MemTotal x max_swap_ratio - SwapTotal
    disk_cap_bytes: int = 0     # free disk minus the reserve
    fstype: str = ""
    fs_supported: bool = False
    swap_cannot_help: bool = False  # shortfall is not in anonymous memory
    snapshot: Optional[MemorySnapshot] = None
    details: Dict[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        snap = self.snapshot or MemorySnapshot()
        return (
            f"mem {snap.used_fraction * 100:.1f}% used, "
            f"swap {snap.swap_free_fraction * 100:.1f}% free "
            f"({snap.swap_used // MB}/{snap.swap_total // MB} MB) -> "
            f"{'grow ' + str(self.grow_bytes // MB) + ' MB' if self.should_grow else 'no growth'}"
            f" [{self.reason}]"
        )


@dataclass(frozen=True)
class SwapDevice:
    path: str
    kind: str       # "file" or "partition"
    size_bytes: int
    used_bytes: int
    priority: int


@dataclass
class ActionResult:
    """Outcome of a step that actually touched the system."""
    ok: bool = False
    dry_run: bool = True
    reason: str = ""
    path: str = ""
    bytes: int = 0
    allocator: str = ""          # "fallocate" or "write"
    steps: List[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.steps.append(message)


class SwapController:
    """Decides on zswap and swapfile headroom, and carries the decision out.

    Every method that changes the system takes ``dry_run`` and defaults it to
    True, so the planning path stays reachable after the execution path lands.
    """

    def __init__(self, config=None, runner: Optional[Callable] = None,
                 clock: Optional[Callable[[], float]] = None):
        self._config = config
        self._runner = runner or self._run_command
        self._clock = clock or time.monotonic
        # (timestamp, snapshot) worst-case window; see sample()/worst_recent().
        self._window: Deque[Tuple[float, MemorySnapshot]] = deque()
        # When memory last dropped below the shrink watermark and stayed there.
        # None means "not relaxed right now"; see sample()/relaxed_for().
        self._relaxed_since: Optional[float] = None

    # -- command execution -------------------------------------------------

    @staticmethod
    def _run_command(argv: List[str], timeout: int = 60) -> Tuple[int, str, str]:
        """Run *argv* with no shell.  Returns (returncode, stdout, stderr)."""
        try:
            done = subprocess.run(argv, capture_output=True, text=True,  # nosec
                                  timeout=timeout, check=False)
            return done.returncode, done.stdout.strip(), done.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except (OSError, ValueError) as exc:
            return 1, "", str(exc)

    # -- config ------------------------------------------------------------

    def growth_config(self) -> dict:
        cfg = dict(SWAP_GROWTH_DEFAULTS)
        policy = getattr(self._config, "limit_policy", None)
        if isinstance(policy, dict):
            user = policy.get("memory", {}).get("swap_growth")
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in SWAP_GROWTH_DEFAULTS})
        return cfg

    @property
    def enabled(self) -> bool:
        return bool(self.growth_config().get("enabled"))

    # -- system reads ------------------------------------------------------

    def read_meminfo(self, path: str = PROC_MEMINFO) -> MemorySnapshot:
        """Parse /proc/meminfo.  All values are kB there; returned in bytes."""
        wanted = {
            "MemTotal": "mem_total", "MemFree": "mem_free",
            "MemAvailable": "mem_available", "AnonPages": "anon_pages",
            "SwapTotal": "swap_total", "SwapFree": "swap_free",
        }
        values: Dict[str, int] = {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    field_name = wanted.get(key.strip())
                    if field_name is None:
                        continue
                    parts = rest.split()
                    if parts:
                        try:
                            values[field_name] = int(parts[0]) * 1024
                        except ValueError:
                            pass
        except OSError as exc:
            logger.error(f"Cannot read {path}: {exc}")
            return MemorySnapshot()
        return MemorySnapshot(**values)

    # -- worst-case window -------------------------------------------------

    def sample(self, snapshot: Optional[MemorySnapshot] = None) -> MemorySnapshot:
        """Record one observation for the worst-case window and return it.

        Call this on the balancer tick.  Parsing /proc/meminfo costs tens of
        microseconds, and it is the only way the window sees the troughs: the
        growth check itself runs once a minute, so a window built from its own
        samples would describe nothing.
        """
        if snapshot is None:
            snapshot = self.read_meminfo()
        if snapshot.mem_total <= 0:
            return snapshot
        now = self._clock()
        self._window.append((now, snapshot))
        horizon = now - float(self.growth_config().get("pressure_window_sec", 30))
        while self._window and self._window[0][0] < horizon:
            self._window.popleft()

        # Shrink timer, tested against the raw sample rather than the window's
        # worst: any single reading above the line restarts the clock, which is
        # what makes "relaxed for ten minutes" mean uninterrupted.
        if snapshot.used_fraction < self.shrink_watermark():
            if self._relaxed_since is None:
                self._relaxed_since = now
        else:
            self._relaxed_since = None
        return snapshot

    def shrink_watermark(self) -> float:
        """Memory level below which a grown file may be handed back."""
        cfg = self.growth_config()
        grow = float(cfg.get("grow_mem_watermark",
                             SWAP_GROWTH_DEFAULTS["grow_mem_watermark"]))
        return max(0.0, grow - SHRINK_HYSTERESIS)

    def relaxed_for(self) -> float:
        """Seconds memory has been continuously below the shrink watermark."""
        if self._relaxed_since is None:
            return 0.0
        return max(0.0, self._clock() - self._relaxed_since)

    def should_shrink(self) -> Tuple[bool, str]:
        """Whether the idle path may release a file now.  ``(allowed, reason)``.

        Only answers "has the pressure been gone long enough".  Whether any
        individual file can be faulted back into RAM is release()'s headroom
        gate, which applies on every path including shutdown.
        """
        if not self.managed_devices():
            return False, "nothing managed to release"
        idle = self.relaxed_for()
        if idle < SHRINK_IDLE_SEC:
            return False, (f"memory relaxed for {idle:.0f}s, need "
                           f"{SHRINK_IDLE_SEC:.0f}s below {self.shrink_watermark():.2f}")
        return True, f"memory below {self.shrink_watermark():.2f} for {idle:.0f}s"

    def _window_span(self) -> Tuple[float, Optional[MemorySnapshot], Optional[MemorySnapshot]]:
        """``(span_seconds, oldest, newest)`` for the current window.

        Returns ``(0.0, None, None)`` when there are not two samples to span.
        Both slope helpers below need exactly this, so it lives in one place.
        """
        if len(self._window) < 2:
            return 0.0, None, None
        (first_at, first), (last_at, last) = self._window[0], self._window[-1]
        return last_at - first_at, first, last

    def swap_eta(self) -> Optional[float]:
        """Seconds until swap is exhausted at the window's drain rate.

        ``None`` when the question does not apply: too little history to call it
        a slope, no swap configured, or swap not draining (a rate of zero or
        negative extrapolates to never).

        Pessimistic on purpose, in the same direction as worst_recent(): the
        numerator is the *lowest* SwapFree in the window rather than the latest
        reading, so a drain that briefly pauses still reports the short answer.
        Being early costs a fallocate; being late costs an OOM kill.
        """
        span, first, last = self._window_span()
        if span < MIN_SLOPE_SPAN_SEC or first is None or last is None:
            return None
        if last.swap_total <= 0:
            return None
        rate = (first.swap_free - last.swap_free) / span
        if rate <= 0:
            return None
        remaining = min(snap.swap_free for _, snap in self._window)
        return max(0.0, remaining / rate)

    def mem_rise_rate(self) -> Optional[float]:
        """How much of MemTotal used_eff gained across the window, as a fraction.

        ``None`` when there is too little history.  Negative when memory is
        being given back, which callers can simply compare against a positive
        threshold.

        This one is not used by plan_growth.  It exists because the pressure
        *score* is a poor early warning -- its memory term is a deliberately
        sharp gate, so it reads 0.19 two seconds before it reads 1.00 -- while
        raw memory usage climbs smoothly and visibly.  The balancer uses it to
        start warming the top-consumer cache before the level moves.
        """
        span, first, last = self._window_span()
        if span < MIN_SLOPE_SPAN_SEC or first is None or last is None:
            return None
        if last.mem_total <= 0:
            return None
        return (last.used_effective - first.used_effective) / last.mem_total

    def worst_recent(self, snapshot: MemorySnapshot) -> MemorySnapshot:
        """*snapshot* merged with the worst values seen in the window.

        Takes the lowest MemAvailable and the lowest SwapFree observed, which
        need not come from the same sample -- this is a pessimistic composite,
        not a historical reading.  That is deliberate: both are gates on whether
        to make more headroom available, and being early costs a fallocate while
        being late costs an OOM kill.

        Everything else (totals, AnonPages) comes from *snapshot*, so the
        derived sizes stay consistent with the present.
        """
        if not self._window:
            return snapshot
        horizon = self._clock() - float(self.growth_config().get("pressure_window_sec", 30))
        recent = [snap for stamp, snap in self._window if stamp >= horizon]
        if not recent:
            return snapshot
        return replace(
            snapshot,
            mem_available=min([snapshot.mem_available] + [s.mem_available for s in recent]),
            swap_free=min([snapshot.swap_free] + [s.swap_free for s in recent]),
        )

    def read_swap_devices(self, path: str = PROC_SWAPS) -> List[SwapDevice]:
        """Parse /proc/swaps.  Sizes are in kB there; returned in bytes."""
        devices: List[SwapDevice] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            logger.error(f"Cannot read {path}: {exc}")
            return devices
        for line in lines[1:]:  # skip the header
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                devices.append(SwapDevice(
                    path=parts[0], kind=parts[1],
                    size_bytes=int(parts[2]) * 1024,
                    used_bytes=int(parts[3]) * 1024,
                    priority=int(parts[4]),
                ))
            except ValueError:
                continue
        return devices

    def filesystem_type(self, target: str, mounts_path: str = PROC_MOUNTS) -> str:
        """Filesystem type of the mount that *target* lives on, or "" if unknown.

        Resolved from /proc/mounts by longest-prefix match rather than by
        shelling out to findmnt: no subprocess, and it works for a path whose
        parent directories do not exist yet.
        """
        try:
            with open(mounts_path, "r", encoding="utf-8") as handle:
                entries = [line.split() for line in handle.read().splitlines()]
        except OSError as exc:
            logger.error(f"Cannot read {mounts_path}: {exc}")
            return ""
        target = os.path.abspath(target)
        best_len, best_type = -1, ""
        for parts in entries:
            if len(parts) < 3:
                continue
            mount_point, fstype = parts[1], parts[2]
            if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
                if len(mount_point) > best_len:
                    best_len, best_type = len(mount_point), fstype
        return best_type

    def disk_space(self, target: str):
        """(total, available) bytes for the filesystem holding *target*.

        Walks up to the nearest existing ancestor, because the configured swap
        directory may not have been created yet.
        """
        probe = os.path.abspath(target)
        while probe != "/" and not os.path.exists(probe):
            probe = os.path.dirname(probe)
        try:
            stat = os.statvfs(probe)
        except OSError as exc:
            logger.error(f"statvfs({probe}) failed: {exc}")
            return 0, 0
        # f_bavail, not f_bfree: the latter counts blocks reserved for root.
        return stat.f_blocks * stat.f_frsize, stat.f_bavail * stat.f_frsize

    # -- planning ----------------------------------------------------------

    def plan_growth(self, snapshot: Optional[MemorySnapshot] = None,
                    disk_total: Optional[int] = None,
                    disk_available: Optional[int] = None,
                    fstype: Optional[str] = None) -> GrowthPlan:
        """Decide whether to grow swap, and by how much.  Reads only.

        The arguments exist so tests can pose a machine shape; in production all
        four are read from the system.
        """
        cfg = self.growth_config()
        swap_dir = str(cfg.get("path", SWAP_GROWTH_DEFAULTS["path"]))
        if snapshot is None:
            snapshot = self.read_meminfo()
        # Judge on the worst of the recent window, not on this instant: the
        # instant is as likely to land on a peak as on a trough, and only the
        # troughs kill anything.
        snapshot = self.worst_recent(snapshot)
        if fstype is None:
            fstype = self.filesystem_type(swap_dir)
        if disk_total is None or disk_available is None:
            disk_total, disk_available = self.disk_space(swap_dir)

        allow_fs = set(cfg.get("allow_fs") or SWAP_GROWTH_DEFAULTS["allow_fs"])
        fs_supported = fstype in (allow_fs & DYNAMIC_GROWTH_FS)

        step = int(snapshot.mem_total * float(cfg["step_ratio"]))
        ratio_cap = max(0, int(snapshot.mem_total * float(cfg["max_swap_ratio"])) - snapshot.swap_total)
        reserve = max(int(disk_total * float(cfg["disk_reserve_pct"]) / 100.0),
                      int(float(cfg["disk_reserve_min_gb"]) * GB))
        disk_cap = max(0, disk_available - reserve)

        # The shortfall is only fixable by swap if it is in anonymous memory.
        # This does not block growth -- growing costs almost nothing -- but it
        # tells L2 not to bother and is worth surfacing to the user.
        deficit = max(0, snapshot.used_effective - int(snapshot.mem_total * float(cfg["grow_mem_watermark"])))
        swap_cannot_help = bool(deficit) and snapshot.movable_anon < deficit

        base = dict(
            step_bytes=step, ratio_cap_bytes=ratio_cap, disk_cap_bytes=disk_cap,
            fstype=fstype, fs_supported=fs_supported, snapshot=snapshot,
            swap_cannot_help=swap_cannot_help,
            details={
                "deficit_bytes": deficit,
                "movable_anon_bytes": snapshot.movable_anon,
                "disk_reserve_bytes": reserve,
                "disk_available_bytes": disk_available,
                "swap_dir": swap_dir,
            },
        )

        if not cfg.get("enabled"):
            return GrowthPlan(reason="swap growth disabled", **base)

        mem_watermark = float(cfg["grow_mem_watermark"])
        swap_watermark = float(cfg["grow_swap_watermark"])
        swap_critical = float(cfg.get("grow_swap_critical",
                                      SWAP_GROWTH_DEFAULTS["grow_swap_critical"]))
        if snapshot.mem_total <= 0:
            return GrowthPlan(reason="meminfo unavailable", **base)

        # Swap almost gone is sufficient on its own.  Waiting for the memory
        # watermark here would mean waiting forever: the fuller swap gets, the
        # more anonymous memory it is holding, and the *lower* used_eff reads.
        swap_exhausted = snapshot.swap_free_fraction <= swap_critical
        # The predictive arm.  Both level arms below describe a state that has
        # already arrived; this one describes one that is arriving.  It needs
        # the memory watermark as well, so that a machine merrily paging a cold
        # working set out to a large swap does not keep growing the file: it is
        # the combination -- memory full AND swap going fast -- that has no
        # ending other than a kill.
        eta = self.swap_eta()
        draining = (snapshot.used_fraction >= mem_watermark
                    and eta is not None and eta <= GROW_LEAD_SEC)
        if swap_exhausted:
            trigger = (f"swap critically low ({snapshot.swap_free_fraction:.2f} "
                       f"<= {swap_critical})")
        elif draining:
            trigger = (f"swap draining: {snapshot.swap_free / MB:.0f} MB free, "
                       f"~{eta:.0f}s to empty (<= {GROW_LEAD_SEC:.0f}s)")
        elif snapshot.used_fraction < mem_watermark:
            return GrowthPlan(
                reason=f"memory below watermark ({snapshot.used_fraction:.2f} < {mem_watermark})",
                **base)
        elif snapshot.swap_free_fraction > swap_watermark:
            return GrowthPlan(
                reason=f"swap still roomy ({snapshot.swap_free_fraction:.2f} > {swap_watermark})",
                **base)
        else:
            trigger = "both watermarks crossed"

        if not fs_supported:
            # Refusing is the safe answer: on btrfs swapon rejects the file
            # without nodatacow, and on zfs it can deadlock the pool.
            hint = "needs nodatacow" if fstype in CONDITIONAL_GROWTH_FS else "unsupported"
            return GrowthPlan(reason=f"filesystem {fstype or 'unknown'} {hint}", **base)

        grow = min(step, ratio_cap, disk_cap)
        if grow <= 0:
            if ratio_cap <= 0:
                reason = f"swap already at the {cfg['max_swap_ratio']}x MemTotal cap"
            else:
                reason = (f"disk reserve not met (free {disk_available // MB} MB, "
                          f"reserve {reserve // MB} MB)")
            return GrowthPlan(reason=reason, **base)

        return GrowthPlan(should_grow=True, grow_bytes=grow, reason=trigger, **base)

    # -- zswap -------------------------------------------------------------

    def zswap_status(self, params_dir: str = ZSWAP_PARAMS) -> Dict[str, str]:
        """Current zswap module parameters, or {} when zswap is not built in."""
        status: Dict[str, str] = {}
        try:
            names = os.listdir(params_dir)
        except OSError:
            return status
        for name in names:
            try:
                with open(os.path.join(params_dir, name), "r", encoding="utf-8") as handle:
                    status[name] = handle.read().strip()
            except OSError:
                continue
        return status

    def plan_zswap(self, params_dir: str = ZSWAP_PARAMS) -> Dict[str, object]:
        """What would be written to enable zswap, without writing it.

        Order matters: the compressor and pool size are set before enabling, so
        the pool is never allocated with the wrong compressor and then resized.
        """
        cfg = self.growth_config()
        status = self.zswap_status(params_dir)
        plan: Dict[str, object] = {
            "available": bool(status),
            "current": status,
            "wanted": {},
            "writes": [],
            "reason": "",
        }
        if not status:
            plan["reason"] = "zswap not available in this kernel"
            return plan
        if not cfg.get("zswap"):
            plan["reason"] = "zswap disabled in config"
            return plan

        wanted = {
            "compressor": str(cfg.get("zswap_compressor")),
            "max_pool_percent": str(int(cfg.get("zswap_pool_percent"))),
            "enabled": "Y",
        }
        plan["wanted"] = wanted
        for key in ("compressor", "max_pool_percent", "enabled"):
            if key in status and status[key] != wanted[key]:
                plan["writes"].append((os.path.join(params_dir, key), wanted[key]))
        plan["reason"] = "already as configured" if not plan["writes"] else "changes pending"
        return plan

    def apply_zswap(self, dry_run: bool = True,
                    params_dir: str = ZSWAP_PARAMS) -> ActionResult:
        """Enable zswap with the configured compressor and pool size.

        Reversible by hand: the previous value of every parameter we touch is
        recorded in ``result.steps`` before the write, so restoring is a matter
        of writing those strings back.  Nothing here is persisted across a
        reboot -- the kernel comes up with its own defaults again.
        """
        plan = self.plan_zswap(params_dir)
        result = ActionResult(dry_run=dry_run, path=params_dir)
        writes = plan.get("writes") or []
        if not writes:
            result.ok = True
            result.reason = str(plan.get("reason") or "nothing to do")
            return result

        current = plan.get("current") or {}
        for path, value in writes:
            key = os.path.basename(path)
            was = current.get(key, "?")
            result.note(f"{key}: {was} -> {value}")
            if dry_run:
                continue
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(value)
            except OSError as exc:
                # A failed compressor write must not be followed by enabling the
                # pool -- that would allocate it with the previous compressor.
                result.reason = f"write {key} failed: {exc}"
                logger.error(f"[swap] zswap {result.reason}")
                return result
        result.ok = True
        result.reason = "dry run" if dry_run else "zswap applied"
        return result

    # -- execution ---------------------------------------------------------

    def managed_dir(self) -> str:
        return str(self.growth_config().get("path", SWAP_GROWTH_DEFAULTS["path"]))

    def managed_files_on_disk(self) -> List[str]:
        """Swapfiles in the managed directory that we are allowed to remove."""
        directory = self.managed_dir()
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        return sorted(os.path.join(directory, n) for n in names
                      if MANAGED_NAME_RE.match(n))

    def managed_devices(self, swaps_path: str = PROC_SWAPS) -> List[SwapDevice]:
        """Active swap devices that are ours.

        Matched on both the directory and the filename pattern, so /swap.img --
        or anything else an administrator turned on -- is never a candidate for
        swapoff.
        """
        directory = os.path.abspath(self.managed_dir())
        ours = []
        for device in self.read_swap_devices(swaps_path):
            head, tail = os.path.split(os.path.abspath(device.path))
            if head == directory and MANAGED_NAME_RE.match(tail):
                ours.append(device)
        return ours

    def next_managed_path(self) -> str:
        """First unused swap-NN.img name in the managed directory."""
        used = set()
        for path in self.managed_files_on_disk():
            match = MANAGED_NAME_RE.match(os.path.basename(path))
            if match:
                used.add(int(match.group(1)))
        index = 1
        while index in used:
            index += 1
        return os.path.join(self.managed_dir(), MANAGED_NAME_FMT % index)

    @staticmethod
    def _allocate(path: str, size: int, allocator: str) -> None:
        """Create *path* at *size* bytes.  Raises OSError on failure.

        The file is created 0600 by open() itself rather than chmod'ed
        afterwards: a swapfile that is briefly world-readable is a swapfile
        whose contents were briefly world-readable.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            if allocator == "fallocate":
                # Milliseconds on ext4/xfs and no data written -- the only
                # allocator safe to run while the machine is already short of
                # memory.  glibc emulates it by writing zeros on filesystems
                # that lack support, which is why we gate on fstype first.
                os.posix_fallocate(fd, 0, size)
            else:
                blank = b"\0" * min(size, FALLBACK_CHUNK)
                remaining = size
                while remaining > 0:
                    chunk = blank if remaining >= FALLBACK_CHUNK else blank[:remaining]
                    remaining -= os.write(fd, chunk)
                os.fsync(fd)
        finally:
            os.close(fd)

    def _try_make_swap(self, path: str, size: int, allocator: str,
                       result: ActionResult) -> bool:
        """allocate -> mkswap -> swapon.  Cleans up after itself on failure.

        The retry wraps this whole chain, not just the allocation: the failure
        that actually happens in the field is fallocate succeeding and swapon
        then rejecting the file (some XFS kernels refuse unwritten extents), so
        retrying the allocator alone would never recover from it.
        """
        try:
            if os.path.exists(path):
                os.remove(path)
            self._allocate(path, size, allocator)
        except OSError as exc:
            result.note(f"{allocator}: allocate failed: {exc}")
            self._discard(path)
            return False

        code, _, err = self._runner(["mkswap", path])
        if code != 0:
            result.note(f"{allocator}: mkswap failed: {err or code}")
            self._discard(path)
            return False

        # No --priority: a negative one is silently ignored (see the note by
        # MANAGED_NAME_RE) and a non-negative one would put our file *ahead* of
        # the distro's, which is the opposite of what we want.  Letting the
        # kernel auto-assign at least keeps it negative.
        code, _, err = self._runner(["swapon", path])
        if code != 0:
            result.note(f"{allocator}: swapon failed: {err or code}")
            self._discard(path)
            return False

        result.note(f"{allocator}: swapon ok at priority {self._actual_priority(path)}")
        return True

    def _actual_priority(self, path: str) -> str:
        """The priority the kernel really gave *path*, per /proc/swaps.

        Read back rather than assumed: the priority we ask for is not the
        priority we get, and the difference decides whether our pages and the
        distro's end up interleaved.
        """
        for device in self.read_swap_devices():
            if device.path == path:
                return str(device.priority)
        return "unknown"

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def grow(self, plan: Optional[GrowthPlan] = None,
             dry_run: bool = True) -> ActionResult:
        """Add one managed swapfile according to *plan*.

        Never touches an existing swap device and never writes /etc/fstab, so a
        reboot undoes everything this does.
        """
        if plan is None:
            plan = self.plan_growth()
        result = ActionResult(dry_run=dry_run, bytes=plan.grow_bytes)

        if not plan.should_grow:
            result.reason = plan.reason or "no growth planned"
            return result

        directory = self.managed_dir()
        # Re-check the filesystem here rather than trusting the plan: the plan
        # may have been made before the directory existed, and the answer for a
        # missing path is the answer for its parent.
        fstype = self.filesystem_type(directory)
        allow_fs = set(self.growth_config().get("allow_fs") or
                       SWAP_GROWTH_DEFAULTS["allow_fs"])
        if fstype not in (allow_fs & DYNAMIC_GROWTH_FS):
            result.reason = f"filesystem {fstype or 'unknown'} not allowed for swapfiles"
            return result

        # And re-check the disk: the plan is a tick old and something else may
        # have filled the volume in the meantime.
        reserve = int(plan.details.get("disk_reserve_bytes", 0))
        _, available = self.disk_space(directory)
        if available - reserve < plan.grow_bytes:
            result.reason = (f"disk shrank since planning (free {available // MB} MB, "
                             f"need {(plan.grow_bytes + reserve) // MB} MB)")
            return result

        path = self.next_managed_path()
        result.path = path

        if dry_run:
            result.ok = True
            result.reason = f"dry run: would add {plan.grow_bytes // MB} MB at {path}"
            result.note(f"mkdir -m 700 {directory}")
            result.note(f"fallocate {plan.grow_bytes // MB} MB -> mkswap -> swapon")
            return result

        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            result.reason = f"cannot prepare {directory}: {exc}"
            logger.error(f"[swap] {result.reason}")
            return result

        for allocator in ("fallocate", "write"):
            if self._try_make_swap(path, plan.grow_bytes, allocator, result):
                result.ok = True
                result.allocator = allocator
                result.reason = (f"added {plan.grow_bytes // MB} MB of swap at {path} "
                                 f"via {allocator}")
                logger.info(f"[swap] {result.reason}")
                return result

        result.reason = f"could not bring up a swapfile at {path}"
        logger.error(f"[swap] {result.reason}: {'; '.join(result.steps)}")
        return result

    def release(self, dry_run: bool = True,
                snapshot: Optional[MemorySnapshot] = None,
                only_unused: bool = False,
                limit: Optional[int] = None) -> List[ActionResult]:
        """Turn off and delete managed swapfiles, when it is safe to do so.

        swapoff has to fault every page in the file back into RAM, so doing it
        unconditionally while memory is tight turns a shrink into an OOM.  Each
        file is released only if MemAvailable covers its used pages with the
        headroom ratio to spare, and the call is bounded by a timeout because
        swapoff on a busy file can run for minutes.

        *only_unused* narrows that to files holding nothing at all, which is the
        shutdown rule: a file at zero used costs literally nothing to hand back,
        while one holding pages would have to fault them into a machine we are
        about to stop watching.  Shrinking a file that is actually in use is the
        idle path's job -- it waits for the pressure to be gone and still checks
        headroom, which is a judgement call we should not be making on the way
        out the door.

        *limit* caps how many files are released in one call, so the idle path
        can give memory back one file at a time instead of in a single
        multi-gigabyte fault storm.

        Files that are ours but no longer in /proc/swaps are just deleted --
        that is the state a reboot leaves behind, since we never write fstab.
        """
        results: List[ActionResult] = []
        if snapshot is None:
            snapshot = self.read_meminfo()

        # Emptiest first: the free ones cost nothing, and taking them first
        # leaves the most headroom for whichever file is looked at next.
        active = sorted(self.managed_devices(), key=lambda d: d.used_bytes)
        # Budget across the whole set: releasing three files in turn needs
        # headroom for all three, not for whichever one is looked at first.
        headroom = snapshot.mem_available
        released = 0
        for device in active:
            result = ActionResult(dry_run=dry_run, path=device.path,
                                  bytes=device.used_bytes)
            if limit is not None and released >= limit:
                result.reason = f"deferred: {released} file(s) released this round"
                results.append(result)
                continue
            if only_unused and device.used_bytes > 0:
                result.reason = (f"in use: {device.used_bytes // MB} MB still held, "
                                 f"leaving it for the idle path")
                results.append(result)
                continue
            needed = int(device.used_bytes * SWAPOFF_HEADROOM_RATIO)
            if headroom < needed:
                result.reason = (f"unsafe: {device.used_bytes // MB} MB in use needs "
                                 f"{needed // MB} MB free, have {headroom // MB} MB")
                results.append(result)
                continue
            if dry_run:
                result.ok = True
                result.reason = f"dry run: would swapoff {device.path}"
                headroom -= needed
                released += 1
                results.append(result)
                continue

            code, _, err = self._runner(["swapoff", device.path],
                                        timeout=SWAPOFF_TIMEOUT_SEC)
            if code != 0:
                result.reason = f"swapoff failed: {err or code}"
                logger.error(f"[swap] {result.reason}")
                results.append(result)
                continue
            headroom -= needed
            released += 1
            result.note("swapoff ok")
            self._discard(device.path)
            result.ok = True
            result.reason = f"released {device.path}"
            logger.info(f"[swap] {result.reason}")
            results.append(result)

        still_active = {os.path.abspath(d.path) for d in self.managed_devices()} \
            if not dry_run else {os.path.abspath(d.path) for d in active}
        for path in self.managed_files_on_disk():
            if os.path.abspath(path) in still_active:
                continue
            result = ActionResult(dry_run=dry_run, path=path,
                                  reason=f"orphan file removed: {path}")
            if not dry_run:
                self._discard(path)
            else:
                result.reason = f"dry run: would remove orphan {path}"
            result.ok = True
            results.append(result)
        return results

    # -- reporting ---------------------------------------------------------

    def log_plan(self, plan: GrowthPlan) -> None:
        """Dry-run output: the decision plus the numbers behind it.

        P2 ships with no execution path, so this log is the deliverable -- it is
        what gets compared against reality before P3 is allowed to act.
        """
        logger.info(f"[swap] {plan.summary()}")
        snap = plan.snapshot or MemorySnapshot()
        logger.info(
            f"[swap]   mem_total={snap.mem_total // MB} MB "
            f"used_eff={snap.used_effective // MB} MB "
            f"anon={snap.anon_pages // MB} MB movable_anon={snap.movable_anon // MB} MB"
        )
        logger.info(
            f"[swap]   step={plan.step_bytes // MB} MB "
            f"ratio_cap={plan.ratio_cap_bytes // MB} MB "
            f"disk_cap={plan.disk_cap_bytes // MB} MB "
            f"fs={plan.fstype or 'unknown'} supported={plan.fs_supported}"
        )
        if plan.swap_cannot_help:
            logger.warning(
                "[swap]   shortfall is not in anonymous memory "
                f"(movable {snap.movable_anon // MB} MB < deficit "
                f"{int(plan.details.get('deficit_bytes', 0)) // MB} MB) -- "
                "more swap will not relieve this; L2 should stand down"
            )
