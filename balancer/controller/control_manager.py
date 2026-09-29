# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from utils.logger import get_logger
logger = get_logger(__name__)
from monitor.monitor_api import SystemPressureMonitor

from controller.controller import Controller
from controller.cpu import CPUController
from controller.memory import (
    DEFAULT_RECLAIM_SWAPPINESS,
    MemoryController,
    ReclaimOutcome,
)
from controller.governor import GovernorController
from controller.swap import ActionResult, GrowthPlan, SwapController
from config.config import b_config

# Defaults for limit_policy.memory.reclaim (L2).  Shipped disabled: the balancer
# is not wired to this yet, and enabling it needs the swap headroom from L0.
RECLAIM_DEFAULTS = {
    "enabled": False,
    # The only goal L2 pursues, and it is a system-level one: used_eff below this
    # share of MemTotal.  Not "each app under its own memory.high" -- that cap can
    # be far below the app's working set, so aiming at it either fails or
    # over-reclaims.
    "target_mem_pct": 80,
    # Free memory is the gate, PSI is the gain: how much memory is gone decides
    # *whether* to act, how much it hurts decides *how hard*.
    "psi_full_scale": 0.10,
    "gain_min": 0.01,
    "gain_max": 0.05,
    "swappiness": DEFAULT_RECLAIM_SWAPPINESS,
    # Measured ~1 ms per MB of anon reclaim, all of it blocking the caller, so
    # the work is chunked and run on the executor rather than on the tick.
    "chunk_mb": 200,
    # Skip targets that are still running: a busy cgroup faulted its pages back
    # in within two seconds, wasting the block we paid for.
    "idle_cpu_threshold": 0.05,
    # Skip cache-heavy targets.  swappiness cannot protect page cache (it tunes
    # the scan ratio, not an absolute preference), so the gate lives here.
    "max_file_cache_fraction": 0.5,
    "cooldown_sec": 30,
}

# Pre-reclaim L0 check skip factor.  The check can only answer "no growth
# needed" when free swap already exceeds what is about to be pushed out, and
# paying for it means blocking on the single-worker L0 executor -- possibly
# behind a periodic growth that is mid-fallocate.  The margin is there because
# the cgroup is not the only thing swapping while we work.
PRE_RECLAIM_SWAP_MARGIN = 1.5


class ControlManager:
    def __init__(self):
        self.config = b_config
        self.system_pressure_monitor = SystemPressureMonitor(self.config)
        self.res = self.system_pressure_monitor.res

        self.controller = Controller()
        self.cpu = CPUController(self.config.cgroup_mount)
        self.memory = MemoryController(self.config.cgroup_mount)
        self.governor = GovernorController()
        self.swap = SwapController(self.config)

        self._executor = ThreadPoolExecutor(max_workers=1)

    def register_critical_state_listener(self, callback) -> None:
        """Register a callback invoked when system pressure enters or leaves critical.

        Forwarded to the underlying SystemPressureMonitor.  See
        SystemPressureMonitor.register_critical_state_listener for details.
        """
        self.system_pressure_monitor.register_critical_state_listener(callback)

    def register_level_change_listener(self, callback) -> None:
        """Register a callback invoked on every system/disk-IO pressure level change.

        Forwarded to the underlying SystemPressureMonitor.  See
        SystemPressureMonitor.register_level_change_listener for details.
        """
        self.system_pressure_monitor.register_level_change_listener(callback)

    def set_limited_app_dominant(self, is_dominant: bool, dominant_cgroup: str = None):
        """ Set whether the limited app is dominant, and its cgroup path for PSI discounting. """
        self.system_pressure_monitor.set_limited_app_dominant(is_dominant, dominant_cgroup)

    @property
    def current_level(self) -> str:
        """ Cached system pressure level string, no logging (hot-path safe). """
        return self.system_pressure_monitor.current_level

    @property
    def is_memory_pre_oom(self) -> bool:
        """ Memory sits in the pre-OOM band -- an earlier warning than the level.
        Hot-path safe (cached bool). See SystemPressureMonitor.is_memory_pre_oom. """
        return self.system_pressure_monitor.is_memory_pre_oom

    def get_current_pressure_level(self) -> tuple:
        """ Get system pressure level, score, disk pressure status, and PSI data. """
        return self.system_pressure_monitor.get_current_pressure_level()

    def consume_peak_pressure_level(self) -> tuple:
        """ Return the highest pressure level seen since the last call and reset the peak.
        Used by the balancer loop to avoid missing transient critical spikes. """
        return self.system_pressure_monitor.consume_peak_pressure_level()

    def get_disk_io_stress(self) -> dict:
        """ Cached disk-IO stress details from the last pressure tick, incl. `stressed_disks`.
        The balancer uses it to scope an io.max write to the disks actually under pressure. """
        return self.system_pressure_monitor.get_disk_io_stress()

    def update_network_pressure_level(self, network_data):
        """ Get network pressure level based on network data. """
        return self.system_pressure_monitor.update_network_pressure_level(network_data)

    # ------------------------------------------------------------------
    # L2: proactive reclaim (memory.reclaim)
    #
    # L1 (memory.high) pins a cgroup *at* its limit and makes the target pay for
    # it in the allocation path.  L2 pushes it *below* the limit and the caller
    # pays instead.  The two are complementary, which is why reclaim runs after
    # memory.high rather than in place of it.
    # ------------------------------------------------------------------

    def reclaim_config(self) -> dict:
        """limit_policy.memory.reclaim merged over RECLAIM_DEFAULTS."""
        cfg = dict(RECLAIM_DEFAULTS)
        policy = getattr(self.config, "limit_policy", None)
        if isinstance(policy, dict):
            user = policy.get("memory", {}).get("reclaim")
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in RECLAIM_DEFAULTS})
        return cfg

    @property
    def reclaim_enabled(self) -> bool:
        return bool(self.reclaim_config().get("enabled"))

    def cpu_busy_fraction(self, cgroup: str, prev_cpu_usec: Optional[int],
                          elapsed_sec: float) -> Tuple[Optional[float], Optional[int]]:
        """CPU cores consumed by *cgroup* since *prev_cpu_usec*.

        Returns ``(fraction, cpu_usec_now)``.  The fraction is None on the first
        sample or when cpu.stat is unreadable -- the caller should treat that as
        "not known to be idle" and skip reclaim.  ``cpu_usec_now`` is returned
        so the caller can store it for the next tick.
        """
        now = self.memory.read_cpu_usec(cgroup)
        if now is None:
            return None, None
        if prev_cpu_usec is None or elapsed_sec <= 0 or now < prev_cpu_usec:
            return None, now
        return (now - prev_cpu_usec) / (elapsed_sec * 1_000_000.0), now

    def sample_memory(self) -> None:
        """Record one /proc/meminfo observation for the L0/L2 worst-case window.

        Called from the monitor loop on every pass, so the window sees the
        troughs a bursty allocator digs between the minute-cadence checks that
        consult it.  Never raises: a missed sample only costs resolution.
        """
        try:
            self.swap.sample()
        except Exception as exc:
            logger.debug(f"meminfo sample failed: {exc}")

    def memory_pressure(self) -> float:
        """System memory PSI (``some``) as a fraction in [0, 1].

        Deliberately separate from the composite pressure score: that score
        decides whether to limit anything at all, this one only scales how hard
        L2 pulls once the free-memory gate has already opened.
        """
        try:
            return float(self.system_pressure_monitor.psi.get_current_pressure()
                         .get("memory", 0.0))
        except Exception as exc:                      # monitor may be mid-restart
            logger.debug(f"memory PSI unavailable: {exc}")
            return 0.0

    def system_memory_deficit(self) -> Tuple[int, dict]:
        """Bytes of memory that have to be freed to meet the L2 target.

        Returns ``(deficit_bytes, facts)``; zero means the system is already
        under target and L2 must not run at all, however hard any individual app
        is pressing on its own memory.high.
        """
        cfg = self.reclaim_config()
        # The worst of the recent window, not this instant -- same reason L0
        # uses it (see SwapController.worst_recent).  A bursty allocator makes
        # the instantaneous reading a coin flip, and sizing the deficit off a
        # peak means reclaiming nothing on the tick that mattered.
        snapshot = self.swap.worst_recent(self.swap.read_meminfo())
        target = int(snapshot.mem_total * float(cfg.get("target_mem_pct", 80)) / 100.0)
        deficit = max(0, snapshot.used_effective - target)
        return deficit, {
            "mem_total_bytes": snapshot.mem_total,
            "used_effective_bytes": snapshot.used_effective,
            "target_bytes": target,
            "used_fraction": round(snapshot.used_fraction, 4),
            "movable_anon_bytes": snapshot.movable_anon,
            "swap_free_fraction": round(snapshot.swap_free_fraction, 4),
        }

    def reclaim_gain(self, psi_memory: Optional[float] = None) -> float:
        """Share of a candidate's usage to reclaim this round, in [gain_min, gain_max].

        Interpolated on memory PSI.  A system that is 85% full but not stalling
        gets the gentle end; one that is stalling gets the hard end.  Both ends
        are small on purpose: reclaim costs the caller real blocking time, and a
        round that overshoots has to be paid back as refaults.
        """
        cfg = self.reclaim_config()
        low = float(cfg.get("gain_min", 0.01))
        high = float(cfg.get("gain_max", 0.05))
        scale = float(cfg.get("psi_full_scale", 0.10)) or 1.0
        if psi_memory is None:
            psi_memory = self.memory_pressure()
        ratio = max(0.0, min(1.0, psi_memory / scale))
        return low + (high - low) * ratio

    def should_reclaim(self, cgroup: str, busy_fraction: Optional[float]) -> Tuple[bool, str]:
        """Cgroup-local gates for L2.  Returns ``(allowed, reason)``.

        Caller-side gates (source == "auto", cpu_mem_limited, memory.events.high
        rising, cooldown, EAGAIN backoff) stay in the balancer, which owns that
        state.  Everything decidable from the cgroup itself is decided here.
        """
        cfg = self.reclaim_config()
        if not cfg.get("enabled"):
            return False, "reclaim disabled"
        if not self.memory.exists(cgroup):
            return False, "cgroup gone"

        if busy_fraction is None:
            return False, "cpu usage unknown"
        idle_threshold = float(cfg.get("idle_cpu_threshold", 0.05))
        if busy_fraction > idle_threshold:
            return False, f"target busy ({busy_fraction:.3f} > {idle_threshold})"

        cache_fraction = self.memory.file_cache_fraction(cgroup)
        if cache_fraction is None:
            return False, "memory.stat unreadable"
        max_cache = float(cfg.get("max_file_cache_fraction", 0.5))
        if cache_fraction > max_cache:
            # Reclaiming here would mostly evict page cache this app is actively
            # using, trading memory for a disk-IO storm.
            return False, f"cache-heavy ({cache_fraction:.2f} > {max_cache})"

        if self.memory.reclaimable_anon(cgroup) <= 0:
            return False, "no anonymous memory to move"
        return True, "ok"

    def reclaim_memory(self, cgroup: str, nbytes: int,
                       swappiness: Optional[int] = None,
                       chunk_bytes: Optional[int] = None) -> ReclaimOutcome:
        """Reclaim *nbytes* from *cgroup* in chunks.  Blocks; prefer the async form.

        Stops early on any error.  EAGAIN means the cgroup ran out of reclaim
        headroom (swap full) -- the caller should back off and grow swap rather
        than retry, so it is surfaced on the aggregate outcome unchanged.
        """
        cfg = self.reclaim_config()
        if swappiness is None:
            swappiness = cfg.get("swappiness", DEFAULT_RECLAIM_SWAPPINESS)
        if chunk_bytes is None:
            chunk_bytes = max(1, int(cfg.get("chunk_mb", 200))) * 1024 * 1024

        remaining = max(0, int(nbytes))
        total_freed = 0
        total_ms = 0.0
        last = ReclaimOutcome(ok=True, requested_bytes=0, swappiness=swappiness,
                              reason="nothing requested")
        while remaining > 0:
            if not self.memory.exists(cgroup):
                last = ReclaimOutcome(ok=False, requested_bytes=nbytes,
                                      reclaimed_bytes=total_freed, elapsed_ms=total_ms,
                                      swappiness=swappiness, reason="cgroup gone mid-reclaim")
                break
            chunk = min(chunk_bytes, remaining)
            last = self.memory.reclaim(cgroup, chunk, swappiness=swappiness)
            total_freed += last.reclaimed_bytes
            total_ms += last.elapsed_ms
            if not last.ok:
                break
            remaining -= chunk

        return ReclaimOutcome(
            ok=last.ok, errno=last.errno, requested_bytes=int(nbytes),
            reclaimed_bytes=total_freed, elapsed_ms=total_ms,
            swappiness=swappiness, reason=last.reason,
        )

    def reclaim_memory_async(self, cgroup: str, nbytes: int,
                             swappiness: Optional[int] = None,
                             chunk_bytes: Optional[int] = None) -> Future:
        """Queue a chunked reclaim on the shared executor and return its Future.

        The balancer must not call reclaim_memory() inline: a single 200 MB chunk
        blocks the calling thread for ~200 ms, and the tick has other work.
        """
        return self._executor.submit(
            self.reclaim_memory, cgroup, nbytes, swappiness, chunk_bytes
        )

    def _swap_covers(self, nbytes: int) -> bool:
        """True when free swap already has room for *nbytes*, with margin.

        Reading /proc/meminfo costs tens of microseconds; the L0 check this
        guards can block on the executor for as long as a fallocate takes.
        """
        snapshot = self.swap.read_meminfo()
        if snapshot.swap_total <= 0:
            return False
        return snapshot.swap_free >= nbytes * PRE_RECLAIM_SWAP_MARGIN

    def _grow_swap_for_manual_reclaim(
            self, gate: Optional[Callable[[], bool]], why: str) -> Optional[bool]:
        """Ask L0 for headroom on behalf of a manual reclaim.

        ``True`` grown, ``False`` wanted but unavailable, ``None`` not needed or
        not attempted.  *gate* is the caller's growth rate limiter: the balancer
        passes the same slot claim its periodic checks use, so a manual limit
        cannot allocate a step a tick just allocated.  ``None`` disables rate
        limiting, which is what the unit tests want.
        """
        if gate is not None and not gate():
            logger.debug(
                f"[manual-reclaim] {why}: swap growth rate-limited, check skipped")
            return None
        try:
            # The one-worker executor used by automatic L0 checks, so a manual
            # request cannot race a periodic growth plan/file creation.
            plan, growth = self.ensure_swap_headroom_async(dry_run=False).result()
        except Exception as exc:
            logger.warning(f"[manual-reclaim] {why} swap check failed: {exc}")
            return False
        if not plan.should_grow:
            logger.debug(f"[manual-reclaim] {why}: L0 sees no need to grow ({plan.reason})")
            return None
        if growth is None or not growth.ok:
            logger.warning(
                f"[manual-reclaim] {why}: swap growth unavailable ({plan.reason})")
            return False
        return True

    def reclaim_memory_to_target(
            self, cgroup: str, target_bytes: int,
            growth_gate: Optional[Callable[[], bool]] = None) -> ReclaimOutcome:
        """Best-effort reclaim until *cgroup* reaches a user-requested target.

        This is the explicit manual-limit path, so it intentionally does not
        apply L2's automatic idle/cache/system-deficit gates. Check enabled L0
        watermarks before reclaim; if reclaim still runs out of headroom, check
        again and retry only after a managed swapfile was successfully added.

        *growth_gate* rate-limits those L0 checks -- see
        :meth:`_grow_swap_for_manual_reclaim`.  A gate claimed by the pre-check
        therefore refuses the post-EAGAIN one, which is the intended reading: a
        second growth step seconds after the first is exactly what the limiter
        exists to prevent, and if the reclaim EAGAIN'd *after* headroom was
        added, more headroom is not what it is short of.
        """
        current = self.memory.read_current(cgroup)
        if current is None:
            return ReclaimOutcome(
                ok=False, reason="memory.current unreadable; cannot size manual reclaim"
            )

        requested = max(0, current - max(0, int(target_bytes)))
        if requested == 0:
            return ReclaimOutcome(ok=True, reason="already at or below memory target")

        grown = None
        if self.swap.enabled and not self._swap_covers(requested):
            grown = self._grow_swap_for_manual_reclaim(growth_gate, "pre-reclaim")

        outcome = self.reclaim_memory(cgroup, requested)
        logger.debug(
            "[manual-reclaim] %s requested=%d MB reclaimed=%d MB "
            "elapsed=%.0f ms ok=%s reason=%s",
            cgroup, requested // (1024 * 1024),
            outcome.reclaimed_bytes // (1024 * 1024), outcome.elapsed_ms,
            outcome.ok, outcome.reason,
        )
        if not outcome.is_eagain or not self.swap.enabled:
            return outcome
        if grown is False:
            # The pre-check already wanted headroom and could not get it; the
            # retry would meet the same full swap.
            return outcome

        # No outer log line here: every branch of the helper already says which
        # of "rate-limited", "not needed" or "failed" stopped the retry.
        if not self._grow_swap_for_manual_reclaim(growth_gate, "post-EAGAIN"):
            return outcome

        current = self.memory.read_current(cgroup)
        if current is None:
            return outcome
        remaining = max(0, current - int(target_bytes))
        if remaining == 0:
            return ReclaimOutcome(
                ok=True, requested_bytes=requested,
                reclaimed_bytes=outcome.reclaimed_bytes,
                elapsed_ms=outcome.elapsed_ms, swappiness=outcome.swappiness,
                reason="target reached before swap headroom was exhausted",
            )

        retry = self.reclaim_memory(cgroup, remaining)
        logger.info(
            "[manual-reclaim] %s retry requested=%d MB reclaimed=%d MB "
            "elapsed=%.0f ms ok=%s reason=%s",
            cgroup, remaining // (1024 * 1024),
            retry.reclaimed_bytes // (1024 * 1024), retry.elapsed_ms,
            retry.ok, retry.reason,
        )
        return ReclaimOutcome(
            ok=retry.ok, errno=retry.errno, requested_bytes=requested,
            reclaimed_bytes=outcome.reclaimed_bytes + retry.reclaimed_bytes,
            elapsed_ms=outcome.elapsed_ms + retry.elapsed_ms,
            swappiness=retry.swappiness, reason=retry.reason,
        )

    # ------------------------------------------------------------------
    # L0: swap headroom
    # ------------------------------------------------------------------

    def ensure_swap_headroom(self, dry_run: bool = True) -> Tuple[GrowthPlan, Optional[ActionResult]]:
        """Evaluate the swap watermarks, and act on them when asked to.

        Returns the plan plus the result of carrying it out, or None when
        nothing needed doing.  Growth never touches the distro's own swap and
        never writes /etc/fstab.
        """
        plan = self.swap.plan_growth()
        self.swap.log_plan(plan)
        if not plan.should_grow:
            return plan, None
        result = self.swap.grow(plan, dry_run=dry_run)
        if not result.ok:
            logger.warning(f"[swap] growth refused: {result.reason}")
        return plan, result

    def ensure_swap_headroom_async(self, dry_run: bool = True) -> Future:
        """Queue swap growth on the shared executor.

        fallocate is milliseconds on ext4/xfs, but the fallback path really
        writes the whole file and mkswap/swapon are subprocesses, so none of
        this belongs on the tick.
        """
        return self._executor.submit(self.ensure_swap_headroom, dry_run)

    def apply_zswap(self, dry_run: bool = True) -> ActionResult:
        """Bring zswap up to the configured compressor and pool size."""
        result = self.swap.apply_zswap(dry_run=dry_run)
        if result.steps:
            logger.info(f"[swap] zswap {result.reason}: {'; '.join(result.steps)}")
        return result

    def release_swap(self, dry_run: bool = True, only_unused: bool = False,
                     limit: Optional[int] = None) -> List[ActionResult]:
        """Hand back managed swapfiles, skipping any that is unsafe to pull.

        Only files smartune created are candidates; the safety gate is
        MemAvailable against the pages that would have to be faulted back in.
        See SwapController.release for what *only_unused* and *limit* mean.
        """
        results = self.swap.release(dry_run=dry_run, only_unused=only_unused,
                                    limit=limit)
        for result in results:
            logger.info(f"[swap] release {result.path}: {result.reason}")
        return results

    def shrink_swap_if_idle(self, dry_run: bool = False) -> List[ActionResult]:
        """The idle path: give one file back once the pressure is long gone.

        This -- not shutdown -- is what actually walks swap back down to its
        original size, because it is the only context in which faulting a
        file's pages back into RAM is a safe thing to ask for.
        """
        allowed, reason = self.swap.should_shrink()
        if not allowed:
            logger.debug(f"[swap] no shrink: {reason}")
            return []
        logger.info(f"[swap] shrinking: {reason}")
        return self.release_swap(dry_run=dry_run, limit=1)

    def shrink_swap_if_idle_async(self, dry_run: bool = False) -> Future:
        """Queue the idle shrink on the shared executor.

        swapoff is the one call here that can block for a long time -- the
        kernel faults every page of the file back in before it returns -- so it
        must never run on the tick.  Same single-worker executor as growth,
        which also means growth and shrink cannot be in flight together.
        """
        return self._executor.submit(self.shrink_swap_if_idle, dry_run)

    def adjust_resources(self, app_id: str, policy: str, **resource_kwargs):
        """Adjust resources with optional parameters"""
        try:
            logger.info(
                f"Adjusting resources for app_id={app_id} with policy={policy} and resource_kwargs={resource_kwargs}")
            adjustments = {
                'low': lambda: self._low_pressure_adjustment(app_id),
                'medium': lambda: self._medium_pressure_adjustment(app_id, **resource_kwargs),
                'high': lambda: self._high_pressure_adjustment(app_id),
                'critical': lambda: self._critical_pressure_adjustment(app_id, **resource_kwargs),
            }
            adjustment_func = adjustments.get(policy, lambda: None)
            return adjustment_func()
        except Exception as e:
            logger.error("Adjust failed: %s", str(e))
            return False

    def _low_pressure_adjustment(self, app_id: str):
        """Low pressure adjustments."""
        logger.info("Performing low pressure adjustments for app_id=%s", app_id)
        results = [
            self.governor.set_powersave(),
            self.controller.set_all_resources(app_id, is_restore=True)
        ]

        return all(results)

    def _medium_pressure_adjustment(self, app_id: str, **kwargs):
        """Medium pressure adjustments."""
        logger.info("Performing medium pressure adjustments for app_id=%s", app_id)
        cpu_quota = kwargs.get('cpu_quota', None)
        mem_high = kwargs.get('mem_high', None)
        io_weight = kwargs.get('io_weight', None)

        results = [
            self.governor.set_performance(),
            self.controller.set_all_resources(
                app_id,
                cpu_quota=int(cpu_quota) if cpu_quota is not None else None,
                mem_high=int(mem_high) if mem_high is not None else None,
                io_weight=int(io_weight) if io_weight is not None else None,
                is_restore=False
            )
        ]

        return all(results)

    def _high_pressure_adjustment(self, app_id: str):
        """High pressure adjustments."""
        results = [
            self.governor.set_performance(),
            self.controller.high_cpu_throttle()
        ]

        return all(results)

    def _critical_pressure_adjustment(self, app_id: str, **kwargs):
        """Critical pressure adjustments."""
        logger.info("Performing critical pressure adjustments for app_id=%s", app_id)
        cpu_quota = kwargs.get('cpu_quota', None)
        mem_high = kwargs.get('mem_high', None)
        io_weight = kwargs.get('io_weight', None)

        return all([
            self.governor.set_performance(),
            self.controller.set_all_resources(
                app_id,
                cpu_quota=int(cpu_quota) if cpu_quota is not None else None,
                mem_high=int(mem_high) if mem_high is not None else None,
                io_weight=int(io_weight) if io_weight is not None else None,
                is_restore=False
            )
        ])

    def __del__(self):
        """Clean up the thread pool.

        Tolerates a half-built instance: if __init__ raised before _executor was
        assigned, __del__ still runs, and an AttributeError here is reported as
        an ignored exception that masks the original failure.
        """
        executor = getattr(self, "_executor", None)
        if executor is not None:
            executor.shutdown(wait=False)
