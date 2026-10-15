# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import math

from utils.logger import get_logger
logger = get_logger(__name__)

class PressureAnalyzer:
    # Fraction of limited-app CPU/IO pressure discounted from the system score.
    # Higher values discount more of the app's self-inflicted pressure.
    _CPU_IO_SELF_GATE = 0.7

    # --- Level-decision smoothing / hysteresis -------------------------------
    # Smooth non-critical levels and add hysteresis; critical uses the raw score
    # for immediate response.
    _SCORE_EWMA_ALPHA = 0.3   # same first-order-IIR family as PSIMonitor._FRAC_EWMA_ALPHA
    _LEVEL_HYSTERESIS = 0.05  # score must fall this far below a level's entry before downgrading
    # Ascending severity order, used only for hysteresis comparisons.
    _LEVEL_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    # --- Disk-IO pressure gate ------------------------------------------------
    # Weights for mapping io PSI into disk-stall severity. ``config.disk_psi_weights``
    # overrides these defaults.
    _PSI_IO_FULL_W = 3.0   # io.full weight: full ~= 0.33 alone saturates the stall severity
    _PSI_IO_SOME_W = 0.5   # io.some weight: partial waiting contributes, but secondary to full

    def _psi_io_weights(self) -> tuple:
        """Return configured ``(some, full)`` io PSI weights or their defaults."""
        cfg = getattr(self.config, 'disk_psi_weights', None) or {}
        some_w = cfg.get('some')
        full_w = cfg.get('full')
        return (
            float(some_w) if isinstance(some_w, (int, float)) else self._PSI_IO_SOME_W,
            float(full_w) if isinstance(full_w, (int, float)) else self._PSI_IO_FULL_W,
        )

    def __init__(self, config):
        self.config = config
        # State retained across pressure-level samples.
        self._score_smoothed = None
        self._last_level = None
        # Keep disk pressure state independent from the system score.
        self._disk_score_smoothed = None
        self._disk_last_level = None
        # Score breakdown included in the pressure-level log.
        self._score_parts = ""

    def _mem_scarcity_gate(self, mem_avail: float) -> float:
        """Return a smooth memory-scarcity weight based on the free-memory ratio.

        The sigmoid is centered on the configured memory busy threshold and rises
        as available memory falls.
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

        # Discount only the configured fraction of limited-app CPU/IO pressure;
        # retain the rest as a signal of overall system load.
        gate = self._CPU_IO_SELF_GATE
        psi_eff = {
            'cpu': psi_data.get('cpu', 0) * (1.0 - frac.get('cpu', 0.0) * gate),
            'io': psi_data.get('io', 0) * (1.0 - frac.get('io', 0.0) * gate),
        }

        # Use available memory directly for the memory contribution; unlike CPU
        # and IO, it is not discounted based on the limited-app fraction.
        mem_avail = usage_data['memory'].get('available_ratio', 0.0)
        mem_scarcity = self._mem_scarcity_gate(mem_avail)
        psi_eff['memory'] = mem_scarcity

        # Normalize by total weight so each resource contributes according to
        # its configured share.
        total_weight = sum(weights.values()) or 1
        load = (
            weights['cpu'] * psi_eff['cpu'] +
            weights['memory'] * psi_eff['memory'] +
            weights['io'] * psi_eff['io']
        ) / total_weight

        # Preserve the memory-scarcity signal so it can independently drive the
        # score to a high level.
        final_score = min(max(load, mem_scarcity), 1.0)

        # Keep the logged score breakdown compact and readable.
        self._score_parts = (
            f"load={load:.3f} = w-avg(cpu {psi_eff['cpu']:.3f}, mem {psi_eff['memory']:.3f}, "
            f"io {psi_eff['io']:.3f}) w={weights['cpu']}:{weights['memory']}:{weights['io']} "
            f"| mem_scarcity={mem_scarcity:.3f} (free {mem_avail * 100:.0f}%) "
            f"| cpu {usage_data['cpu'].get('usage', 0.0):.0f}% "
            f"mem {usage_data['memory'].get('usage', 0.0):.0f}%"
        )
        # Include the limited-app fraction only when it affects the score.
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
        """Return a stable level and smoothed score; critical uses the raw score."""
        if raw_score >= thresholds.get('critical', 1.0):
            # Enter critical immediately; subsequent descent is smoothed.
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
        # Log elevated states at info; routine low-pressure samples stay at debug.
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
        """Classify disk pressure using saturation gated by io PSI.

        Return ``(level, score, is_stressed)`` using the disk thresholds and
        independent smoothing state. ``disk_details`` supplies log context.
        """
        gate = self._CPU_IO_SELF_GATE
        frac = self_fraction or {}
        disc = 1.0 - frac.get('io', 0.0) * gate
        some = max(0.0, psi_io_some) * disc
        full = max(0.0, psi_io_full) * disc

        low = thresholds['low']
        high = thresholds['high']
        crit = thresholds['critical']

        # Combine io-stall severity with disk saturation.
        some_w, full_w = self._psi_io_weights()
        stall = min(1.0, some * some_w + full * full_w)
        sat = (disk_combined - low) / (high - low) if high > low else 1.0
        sat = max(0.0, min(1.0, sat))
        # PSI raises the saturation score within its remaining headroom.
        raw = disk_combined + (1.0 - disk_combined) * stall * sat
        raw = round(max(0.0, min(1.0, raw)), 2)

        if raw >= crit:
            # Enter critical immediately; subsequent descent is smoothed.
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
        # Explain the additional io.full pressure needed to reach critical.
        gap = ""
        if level == "high" and sat > 0 and full_w > 0 and disk_combined < 1.0:
            need_stall = min(1.0, (crit - disk_combined) / (1.0 - disk_combined) / sat)
            need_full = max(0.0, (need_stall - some * some_w) / full_w)
            gap = f" | for critical: stall>={need_stall:.2f} (io.full>={need_full:.3f})"
        # Keep idle samples at debug while reporting active disk pressure at info.
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
        """Delay level downgrades by the hysteresis margin; upgrades are immediate.

        Critical is released immediately because it is entered from the raw score.
        ``prev_level`` keeps the system and disk channels independent.
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
