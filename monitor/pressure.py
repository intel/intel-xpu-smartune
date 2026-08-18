# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import math

from utils.logger import logger

class PressureAnalyzer:
    # Fraction of a limited app's self-inflicted CPU/IO stall to actually remove from the
    # score. 0 keeps all of it (post-limit pressure stays high, may re-trigger critical);
    # 1 removes all of it (can drop the score into the "low" full-restore band and flap).
    # ~0.7 keeps post-limit pressure reflecting real residual load, landing it around the
    # medium (partial-restore) band. Higher -> more discount -> lower score; lower -> less
    # discount -> higher score. The exact landing depends on the momentary CPU/IO PSI mix,
    # so this is a tunable knob, not a precise target. The discount stays continuous in the
    # self-fraction -- this factor only sets how strongly the self-share is trusted removable.
    _CPU_IO_SELF_GATE = 0.7

    # --- Level-decision smoothing / hysteresis -------------------------------
    # The RAW per-tick score is noisy: cpu PSI alone swings ~0.2<->0.85 between
    # adjacent samples, so the derived score crosses the medium/low boundary
    # every few seconds. Each crossing resets the balancer's restore stability
    # timer, so staged restore never converges. We therefore drive the
    # medium/low classification off an EWMA-smoothed score plus a sticky band,
    # while the CRITICAL trigger keeps reading the raw score so pre-OOM spikes
    # react without smoothing lag.
    _SCORE_EWMA_ALPHA = 0.3   # same first-order-IIR family as PSIMonitor._FRAC_EWMA_ALPHA
    _LEVEL_HYSTERESIS = 0.05  # score must fall this far below a level's entry before downgrading
    # Ascending severity order, used only for hysteresis comparisons.
    _LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    # --- Disk-IO pressure gate ------------------------------------------------
    # How the io PSI (some/full, fractions in [0, 1]) maps to the stall severity that gates
    # disk_combined -- see classify_disk_pressure for the gate itself. io.full dominates
    # because it means every non-idle task is blocked, not just some. Defaults only:
    # ``config.disk_psi_weights`` overrides them live, so a weight can be re-calibrated
    # from measured io PSI without a code change.
    _PSI_IO_FULL_W = 3.0   # io.full weight: full ~= 0.33 alone saturates the stall severity
    _PSI_IO_SOME_W = 0.5   # io.some weight: partial waiting contributes, but secondary to full

    def _psi_io_weights(self) -> tuple:
        """``(some_w, full_w)`` read live from config, falling back to the class defaults.

        Note these are *weights*, not thresholds: unlike ``disk_thresholds`` an in-code
        default is fine here, because a missing block cannot silently borrow the system
        channel's tuning -- there is no system-side equivalent to borrow.
        """
        cfg = getattr(self.config, 'disk_psi_weights', None) or {}
        some_w = cfg.get('some')
        full_w = cfg.get('full')
        return (
            float(some_w) if isinstance(some_w, (int, float)) else self._PSI_IO_SOME_W,
            float(full_w) if isinstance(full_w, (int, float)) else self._PSI_IO_FULL_W,
        )

    def __init__(self, config):
        self.config = config
        # EWMA-smoothed score and the last emitted level, carried across calls
        # to classify_level (None until the first sample).
        self._score_smoothed = None
        self._last_level = None
        # Independent smoothing/level state for the disk-IO channel (classify_disk_pressure),
        # kept separate so it never perturbs the system-score state above.
        self._disk_score_smoothed = None
        self._disk_last_level = None
        # Score breakdown from the current tick's calculate_pressure_score, held for
        # classify_level to print: the two run back to back on every tick and one
        # [sys-level] line carrying both the inputs and the level beats two half-lines.
        self._score_parts = ""

    def _mem_scarcity_gate(self, mem_avail: float) -> float:
        """Smooth sigmoid (logistic) gate in [0, 1] for how much of a (possibly saturated)
        memory-PSI reading represents *genuine* memory pressure, as a function of the
        free-memory ratio.

        Memory PSI pegs at ~1.0 whenever a soft cap (MemoryHigh) forces constant reclaim,
        even while RAM is ample -- that saturation is churn, not scarcity. The gate is
        centered on the memory "busy" point (``1 - memory_busy_threshold%``): it is ~0 while
        RAM is ample (treat the PSI as churn and discount it) and rises smoothly to ~1 as
        free memory approaches the busy point and below, so genuine memory pressure
        resurfaces before an OOM (MemoryHigh is only a soft cap). Continuous and
        differentiable -- no abrupt threshold. Availability-driven, so it behaves identically
        before and after any limit is applied.
        """
        busy = getattr(self.config, 'memory_busy_threshold', 90) / 100.0
        center = max(1.0 - busy, 1e-3)  # free-memory ratio at the busy point
        steepness = getattr(self.config, 'mem_gate_steepness', 8.0)
        x = (mem_avail - center) / center * steepness
        x = max(-60.0, min(60.0, x))    # guard exp() against overflow
        # 1/(1+e^x) is the complement of the ample-RAM logistic: ~0 when RAM is ample,
        # rising to ~1 as free RAM runs out.
        return 1.0 / (1.0 + math.exp(x))

    def calculate_pressure_score(self, psi_data: dict, usage_data, is_limited_app_dominant,
                                 self_fraction: dict = None) -> float:
        """Calculate weighted pressure score.

        When a rate-limited app is still the dominant consumer, throttling it inflates
        PSI (tasks stall on their cgroup limit) even though the rest of the system may
        have headroom. ``self_fraction`` (per-resource, 0..1; see
        PSIMonitor.get_self_inflicted_fraction) says how much of each resource's pressure
        is that app's own doing; we discount only that portion, so any pressure other
        tasks are genuinely experiencing survives. The post-limit level is therefore not
        forced anywhere -- it can land at low/medium/high/critical depending on reality.
        """
        # Read weights live each call so a config edit takes effect without a restart.
        weights = dict(self.config.weights or {})

        frac = self_fraction if (is_limited_app_dominant and self_fraction) else {}

        # CPU / IO are HARD limits (cpu.max / io.max): a limited app can never drive the
        # system into catastrophic CPU/IO exhaustion, so discounting its self-inflicted stall
        # is safe. We remove only a fraction of it (``_CPU_IO_SELF_GATE``), not all: at, say,
        # 85% CPU the machine is genuinely busy even though the limited app "caused" the
        # stall, and fully removing it drops the score into the "low" band -> premature full
        # restore -> limit/restore flapping. Keeping part of it lands post-limit pressure in
        # the medium (partial-restore) band, which is stable.
        gate = self._CPU_IO_SELF_GATE
        psi_eff = {
            'cpu': psi_data.get('cpu', 0) * (1.0 - frac.get('cpu', 0.0) * gate),
            'io': psi_data.get('io', 0) * (1.0 - frac.get('io', 0.0) * gate),
        }

        # Memory pressure is availability-driven and PSI-INDEPENDENT. Two failure modes make
        # memory PSI the wrong signal here: (1) it saturates (~1.0) from soft-cap (MemoryHigh)
        # reclaim churn even while RAM is ample -> over-report; (2) it can stay at ~0 even at
        # near-OOM usage when no reclaim thrashing has kicked in yet -> under-report (observed:
        # 2% free RAM but memory PSI 0.0 -> memory term 0 -> "low"). The scarcity gate encodes
        # proximity to the busy/OOM point directly from the free-RAM ratio (~0 while RAM is
        # ample, rising to ~1 as free RAM runs out), so it IS the memory contribution -- no PSI
        # multiplier. (Equivalent to max(psi*scarcity, scarcity) since psi <= 1.) Being
        # availability-driven, it is identical before and after a limit is applied.
        mem_avail = usage_data['memory'].get('available_ratio', 0.0)
        mem_scarcity = self._mem_scarcity_gate(mem_avail)
        psi_eff['memory'] = mem_scarcity

        # NORMALIZED weighted average, not a raw sum. A raw sum let CPU/IO each add their
        # full PSI (weight 1) so cpu 100% alone reached ~0.7-0.9 -- "high" -- even with RAM
        # ample, which contradicts the intent (weights 1:7:2) that memory dominates and CPU
        # saturation is benign. Dividing by Σweights keeps CPU/IO minor (their small share)
        # and lets memory carry most of the average.
        total_weight = sum(weights.values()) or 1
        load = (
            weights['cpu'] * psi_eff['cpu'] +
            weights['memory'] * psi_eff['memory'] +
            weights['io'] * psi_eff['io']
        ) / total_weight

        # Independent memory channel: the normalized average caps memory's own contribution
        # at weight_mem/Σweights (< 1), but a genuine near-OOM scarcity must still be able to
        # reach critical on its own -- so floor the score at mem_scarcity. Net effect:
        # CPU/IO-only load stays low; the score only climbs to high/critical as free RAM runs
        # out. (mem_scarcity itself is ~0 until RAM approaches the busy point, so memory does
        # not inflate the score while RAM is ample.)
        final_score = min(max(load, mem_scarcity), 1.0)

        # Handed to classify_level, which prints it alongside the level it derived. Formatted
        # to a fixed precision because mem_scarcity is a logistic that underflows to values
        # like 1.29e-22 when RAM is ample, and the raw repr of that buries the rest of the line.
        self._score_parts = (
            f"load={load:.3f} = w-avg(cpu {psi_eff['cpu']:.3f}, mem {psi_eff['memory']:.3f}, "
            f"io {psi_eff['io']:.3f}) w={weights['cpu']}:{weights['memory']}:{weights['io']} "
            f"| mem_scarcity={mem_scarcity:.3f} (free {mem_avail * 100:.0f}%) "
            f"| cpu {usage_data['cpu'].get('usage', 0.0):.0f}% "
            f"mem {usage_data['memory'].get('usage', 0.0):.0f}%"
        )
        # Named only when a limited app is actually dominant; otherwise the discount is a
        # no-op and printing it every tick implies something happened.
        if frac:
            self._score_parts += (f" | self_disc cpu={frac.get('cpu', 0.0):.2f} "
                                  f"io={frac.get('io', 0.0):.2f}")
        return round(final_score, 2)

    def get_pressure_level(self, score: float, thresholds: dict) -> str:
        """Determine the pressure level from a score and threshold configuration."""
        if score >= thresholds.get('critical', 1.0):
            return "critical"
        elif score >= thresholds.get('high', 0.8):
            return "high"
        elif score >= thresholds.get('medium', 0.6):
            return "medium"
        elif score >= thresholds.get('low', 0.4):
            return "low"
        else:
            return "low"

    def classify_level(self, raw_score: float, thresholds: dict) -> tuple:
        """Map a raw pressure score to a stable ``(level, smoothed_score)`` for the control loop.

        ``critical`` is latched off the RAW score so a pre-OOM spike triggers with no
        smoothing lag. Every band below critical is decided from an EWMA-smoothed score
        (see ``_SCORE_EWMA_ALPHA``) plus per-level hysteresis (see ``_apply_level_hysteresis``),
        which stops the medium/low classification from chattering across a threshold and
        repeatedly resetting the balancer's restore stability timer. The smoothed score is
        returned as well so callers report the same value they act on.
        """
        if raw_score >= thresholds.get('critical', 1.0):
            # Fast-attack: snap the smoothed value straight up to the raw score so the
            # reported score stays CONSISTENT with the emitted level (a gauge that derives
            # its label from the score must not read "high" while we act on "critical"),
            # and so OOM reaction never waits on the EWMA. Descent from here is still
            # slow-release via the EWMA below, giving an honest, non-jumpy fall-off.
            smoothed = raw_score
            level = "critical"
        else:
            alpha = self._SCORE_EWMA_ALPHA
            prev = self._score_smoothed
            smoothed = raw_score if prev is None else alpha * raw_score + (1.0 - alpha) * prev
            candidate = self.get_pressure_level(smoothed, thresholds)
            level = self._apply_level_hysteresis(candidate, smoothed, thresholds, self._last_level)

        self._score_smoothed = smoothed
        self._last_level = level
        # `score` is the smoothed value (returned, shown in the UI, decides every band below
        # critical); `raw` only decides the critical latch. INFO once the system is out of
        # the low band, DEBUG otherwise -- this runs every tick.
        _log = logger.info if level != "low" else logger.debug
        _log("[sys-level] level=%s score=%.2f raw=%.2f | %s",
             level, smoothed, raw_score, self._score_parts)
        return level, round(smoothed, 2)

    @staticmethod
    def _format_disks(details: dict) -> str:
        """One-line sub-signal digest of the disks a level can be traced to.

        Busy disks only (``details`` arrives busiest-first); an all-zero row explains
        nothing. Falls back to the busiest disk so an idle machine still shows what it
        looked at.
        """
        if not details:
            return "no disks"
        busy = {d: v for d, v in details.items() if v.get('is_busy')}
        return " ".join(
            f"{d}({v['disk_type']}) p={v['pressure']:.3f} util={v['utilization']:.0f}% "
            f"await={v['await_ms']:.1f}ms aqu={v['aqu']:.0f} "
            f"rd={v['read_kb_per_sec'] / 1024:.0f} wr={v['write_kb_per_sec'] / 1024:.0f} MB/s"
            for d, v in (busy or dict(list(details.items())[:1])).items()
        )

    def classify_disk_pressure(self, disk_combined: float, psi_io_some: float,
                               psi_io_full: float, self_fraction: dict, thresholds: dict,
                               disk_details: dict = None) -> tuple:
        """Map a USE-based ``disk_combined`` to a stable disk-IO ``(level, score, is_stressed)``
        by gating it with (self-inflicted-discounted) io PSI.

        ``disk_combined`` says *how saturated* the disk subsystem is; the io PSI says whether
        that saturation is *actually stalling work*. The two are folded into one continuous
        gated score, so the same EWMA + hysteresis machinery as ``classify_level`` applies
        (with its own state, see ``_disk_*``):

          * ``stall`` in [0, 1] is a continuous severity from the io PSI (``full`` dominant).
          * ``sat`` in [0, 1] is how far ``disk_combined`` sits in the ``[low, high]`` band --
            0 below ``low`` (disk not the cause), 1 at/above ``high``.
          * the PSI lifts ``disk_combined`` within its remaining headroom, scaled by ``sat``:
            ``raw = disk_combined + (1 - disk_combined) * stall * sat``.

        The result is monotone and continuous in BOTH signals: the level climbs
        medium -> high -> critical as saturation and stall rise together (no medium->critical
        cliff), a saturated-but-not-stalling disk tops out at high (armed, not throttled), and
        a stalling-but-unsaturated disk (e.g. a network fs) stays low (``sat`` -> 0). Only a
        disk that is both fully saturated and stalling the whole system reaches critical.

        The PSI is discounted by the same ``_CPU_IO_SELF_GATE`` used for the system score, so a
        rate-limited app stalling on its own cgroup limit does not fake system-wide IO
        pressure. ``self_fraction`` may be None (no discount).

        ``thresholds`` must be the DISK bands (``config.disk_thresholds``), never the system
        ``config.thresholds`` -- the two channels are tuned independently.

        ``disk_details`` is the per-disk sub-signal map behind ``disk_combined``
        (``DiskPressureMonitor.evaluate()['details']``), logged here rather than at its
        source so the inputs and the level they produced stay on one line.
        """
        gate = self._CPU_IO_SELF_GATE
        frac = self_fraction or {}
        disc = 1.0 - frac.get('io', 0.0) * gate
        some = max(0.0, psi_io_some) * disc
        full = max(0.0, psi_io_full) * disc

        low = thresholds['low']
        high = thresholds['high']
        crit = thresholds['critical']

        # Continuous stall severity from the io PSI, and the saturation ramp over [low, high].
        some_w, full_w = self._psi_io_weights()
        stall = min(1.0, some * some_w + full * full_w)
        sat = (disk_combined - low) / (high - low) if high > low else 1.0
        sat = max(0.0, min(1.0, sat))
        # PSI fills disk_combined's headroom, scaled by saturation. Rounded to absorb float
        # noise so a fully-filled headroom lands exactly on the critical threshold.
        raw = disk_combined + (1.0 - disk_combined) * stall * sat
        raw = round(max(0.0, min(1.0, raw)), 2)

        if raw >= crit:
            # Genuine extreme (fully saturated AND system-wide stall): pin to critical without
            # EWMA lag (the EWMA cannot asymptotically reach the top threshold). Descent is
            # still slow-release via the EWMA below on subsequent ticks.
            smoothed = raw
            level = "critical"
        else:
            alpha = self._SCORE_EWMA_ALPHA
            prev = self._disk_score_smoothed
            smoothed = raw if prev is None else alpha * raw + (1.0 - alpha) * prev
            candidate = self.get_pressure_level(smoothed, thresholds)
            level = self._apply_level_hysteresis(candidate, smoothed, thresholds, self._disk_last_level)

        self._disk_score_smoothed = smoothed
        self._disk_last_level = level
        is_stressed = self._LEVEL_RANK.get(level, 0) >= self._LEVEL_RANK["high"]
        # At "high" the disk is armed but not throttling, which is when "why not critical?"
        # gets asked -- so say what is still missing: `need_full` is the io.full that would
        # tip it over at this saturation and `some`, and an unreachable value there indicts
        # the workload, not the model.
        gap = ""
        if level == "high" and sat > 0 and full_w > 0 and disk_combined < 1.0:
            need_stall = min(1.0, (crit - disk_combined) / (1.0 - disk_combined) / sat)
            need_full = max(0.0, (need_stall - some * some_w) / full_w)
            gap = f" | for critical: stall>={need_stall:.2f} (io.full>={need_full:.3f})"
        # INFO once the disk is in play, DEBUG while it is idle -- this runs every tick.
        # `score` is the smoothed value (what is returned, what the UI shows, what decides
        # every band below critical); `raw` only decides the critical latch.
        _log = logger.info if (raw >= low or level != "low") else logger.debug
        _log(
            "[disk-level] level=%s score=%.2f raw=%.2f | combined=%.3f sat=%.2f stall=%.2f "
            "(io some=%.3f full=%.3f self_disc=%.2f) | %s%s",
            level, smoothed, raw, disk_combined, sat, stall, some, full, disc,
            self._format_disks(disk_details), gap,
        )
        return level, round(smoothed, 2), is_stressed

    def _apply_level_hysteresis(self, candidate: str, score: float, thresholds: dict,
                                prev_level: str) -> str:
        """Make level *downgrades* sticky; upgrades stay immediate.

        A downgrade below ``prev_level`` is only accepted once ``score`` has fallen a
        full ``_LEVEL_HYSTERESIS`` below that level's entry threshold. This kills chatter
        right at a boundary (e.g. hovering at 0.60) without adding any lag on the way up.
        ``prev_level`` is passed in so the system-score and disk channels can each keep
        their own last level.

        ``critical`` is exempt, in both channels. Every other level is *entered* on the
        smoothed score, so holding it a little past its entry on the way down is symmetric.
        Critical is not: it is entered by a latch on the RAW score topping out, so leaving
        it on ``smoothed >= entry - 0.05`` would report critical at a score of 0.96 -- a
        level the score itself says was never reached, and one the UI renders as "critical"
        next to "96%". It also keeps the throttle armed: the balancer acts on the peak level
        since its last tick, so a hysteresis-held critical throttles another app on evidence
        that has already expired.
        """
        prev = prev_level
        if prev is None or prev == "unknown":
            return candidate
        if self._LEVEL_RANK.get(candidate, 0) >= self._LEVEL_RANK.get(prev, 0):
            return candidate  # same or higher severity: accept immediately
        if prev == "critical":
            return candidate  # latched on the way in, released on the way out
        # Downgrade requested: only leave the current level once clearly below its entry.
        prev_entry = thresholds.get(prev, 0.0)
        if score < prev_entry - self._LEVEL_HYSTERESIS:
            return candidate
        return prev
