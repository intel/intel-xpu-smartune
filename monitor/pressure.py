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

    def __init__(self, config):
        self.config = config
        # EWMA-smoothed score and the last emitted level, carried across calls
        # to classify_level (None until the first sample).
        self._score_smoothed = None
        self._last_level = None

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
        # ample, which contradicts the intent (weights 1:8:1) that memory dominates and CPU
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

        # is_sys_busy is no longer a control input (the memory gate supersedes it); kept for
        # observability only.
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']
        logger.debug(f"score... = {final_score}, load={round(load, 4)}, psi_data={psi_data}, "
                     f"psi_eff={psi_eff}, usage_data={usage_data}, "
                     f"is_limited_app_dominant={is_limited_app_dominant}, self_fraction={frac}, "
                     f"mem_scarcity={round(mem_scarcity, 4)}, is_sys_busy={is_sys_busy}, weights={weights}")
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
            level = self._apply_level_hysteresis(candidate, smoothed, thresholds)

        self._score_smoothed = smoothed
        self._last_level = level
        logger.debug(f"level={level}, smoothed_score={round(smoothed, 4)}, raw_score={round(raw_score, 4)}")
        return level, round(smoothed, 2)

    def _apply_level_hysteresis(self, candidate: str, score: float, thresholds: dict) -> str:
        """Make level *downgrades* sticky; upgrades stay immediate.

        A downgrade below the previous level is only accepted once ``score`` has fallen a
        full ``_LEVEL_HYSTERESIS`` below that level's entry threshold. This kills chatter
        right at a boundary (e.g. hovering at 0.60) without adding any lag on the way up.
        """
        prev = self._last_level
        if prev is None or prev == "unknown":
            return candidate
        if self._LEVEL_RANK.get(candidate, 0) >= self._LEVEL_RANK.get(prev, 0):
            return candidate  # same or higher severity: accept immediately
        # Downgrade requested: only leave the current level once clearly below its entry.
        prev_entry = thresholds.get(prev, 0.0)
        if score < prev_entry - self._LEVEL_HYSTERESIS:
            return candidate
        return prev
