# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from utils.logger import logger

class PressureAnalyzer:
    def __init__(self, config):
        self.config = config
        self.weights = config.weights

    def calculate_pressure_score(self, psi_data: dict, usage_data, is_limited_app_dominant) -> float:
        """Calculate weighted pressure score"""

        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']
        # 1. If the currently limited app is still dominant, reduce cpu/mem/io weights
        weights = self.weights.copy()
        reduce_factor = self.config.dominant_app_reduce_factor
        if is_limited_app_dominant and not is_sys_busy:
            weights['cpu'] = round(weights['cpu'] / reduce_factor)
            weights['memory'] = round(weights['memory'] / reduce_factor)
            weights['io'] = round(weights['io'] / reduce_factor)

        base_score = (
            weights['cpu'] * psi_data.get('cpu', 0) +
            weights['memory'] * psi_data.get('memory', 0) +
            weights['io'] * psi_data.get('io', 0)
        )

        # 2. Reduce score when resource utilisation is low
        resource_adjust_factor = 1.0
        if is_limited_app_dominant and not is_sys_busy:
            resource_adjust_factor = round(1.0 / reduce_factor, 4)  # limited app is dominant but system is not busy

        # 3. Compute final score
        final_score = min(base_score * resource_adjust_factor, 1.0)

        logger.debug(f"score... = {final_score}, base_score={base_score}, psi_data={psi_data}, "
                     f"usage_data={usage_data}, is_limited_app_dominant={is_limited_app_dominant}, "
                     f"weights={weights}, resource_adjust_factor={resource_adjust_factor}")
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
