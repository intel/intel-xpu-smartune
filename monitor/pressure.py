# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import math

from utils.logger import logger

class PressureAnalyzer:
    # Steepness of the sigmoid (logistic) memory-discount gate (larger = sharper transition
    # around the memory "busy" point). See _mem_discount_gate.
    _MEM_GATE_STEEPNESS = 8.0

    def __init__(self, config):
        self.config = config

    def _mem_discount_gate(self, mem_avail: float) -> float:
        """Smooth sigmoid (logistic) gate in [0, 1] for how much of a limited app's
        self-inflicted MEMORY pressure to discount, as a function of the free-memory ratio.

        The sigmoid is centered on the memory "busy" point (``1 - memory_busy_threshold%``):
        the gate is ~1 while RAM is ample (fully trust attribution and discount) and tapers
        smoothly to ~0 as free memory approaches the busy point and below, so genuine memory
        pressure resurfaces before an OOM (MemoryHigh is only a soft cap). Continuous and
        differentiable -- no abrupt threshold.
        """
        busy = getattr(self.config, 'memory_busy_threshold', 90) / 100.0
        center = max(1.0 - busy, 1e-3)  # free-memory ratio at the busy point
        x = (mem_avail - center) / center * self._MEM_GATE_STEEPNESS
        x = max(-60.0, min(60.0, x))    # guard exp() against overflow
        return 1.0 / (1.0 + math.exp(-x))

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

        # Per-resource gate on how much of the self-inflicted share we actually remove.
        # cpu.max / io.max are HARD limits, so a limited app can never drive the system into
        # catastrophic CPU/IO exhaustion -- its self-inflicted stall there is always safe to
        # remove in full (gate = 1). MemoryHigh is a SOFT cap, so the app can still exhaust
        # RAM and OOM; there the gate tapers smoothly toward 0 as free memory runs out (see
        # _mem_discount_gate), so real memory pressure resurfaces before an OOM.
        mem_avail = usage_data['memory'].get('available_ratio', 0.0)
        mem_gate = self._mem_discount_gate(mem_avail)
        gate = {'cpu': 1.0, 'io': 1.0, 'memory': mem_gate}

        # Keep the pressure not attributable to the limited-dominant app (memory's share is
        # removed only to the extent RAM still has headroom).
        psi_eff = {
            res: psi_data.get(res, 0) * (1.0 - frac.get(res, 0.0) * gate[res])
            for res in ('cpu', 'memory', 'io')
        }

        base_score = (
            weights['cpu'] * psi_eff['cpu'] +
            weights['memory'] * psi_eff['memory'] +
            weights['io'] * psi_eff['io']
        )

        final_score = min(base_score, 1.0)

        # is_sys_busy is no longer a control input (the memory gate supersedes it); kept for
        # observability only.
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']
        logger.debug(f"score... = {final_score}, base_score={base_score}, psi_data={psi_data}, "
                     f"psi_eff={psi_eff}, usage_data={usage_data}, "
                     f"is_limited_app_dominant={is_limited_app_dominant}, self_fraction={frac}, "
                     f"mem_gate={round(mem_gate, 4)}, is_sys_busy={is_sys_busy}, weights={weights}")
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
