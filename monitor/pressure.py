#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


from utils.logger import logger

class PressureAnalyzer:
    def __init__(self, config):
        self.config = config
        self.weights = config.weights

    def calculate_pressure_score(self, psi_data: dict, usage_data, is_limited_app_dominant) -> float:
        """Calculate weighted pressure score"""

        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']
        # 1. 已经被限制的进程仍是top1，则降低cpu/mem/io权重
        weights = self.weights.copy()
        if is_limited_app_dominant and not is_sys_busy:
            weights['cpu'] = round(weights['cpu'] / 3)    # 降低3倍
            weights['memory'] = round(weights['memory'] / 3)
            weights['io'] = round(weights['io'] / 3)

        base_score = (
            weights['cpu'] * psi_data.get('cpu', 0) +
            weights['memory'] * psi_data.get('memory', 0) +
            weights['io'] * psi_data.get('io', 0)
        )

        # 2. 查看资源整体使用率，如果剩余较多则把分数降低
        resource_adjust_factor = 1.0
        if is_limited_app_dominant and not is_sys_busy:
            resource_adjust_factor = 0.6  # 当已经受限的应用占主导，但整体资源并不紧张时，降低分数

        # 3. 计算最终分数
        final_score = min(base_score * resource_adjust_factor, 1.0)

        logger.debug(f"score... = {final_score}, base_score={base_score}, psi_data={psi_data}, "
                     f"usage_data={usage_data}, is_limited_app_dominant={is_limited_app_dominant}, "
                     f"weights={weights}, resource_adjust_factor={resource_adjust_factor}")
        return round(final_score, 2)

    def get_pressure_level(self, score: float, thresholds: dict) -> str:
        """根据分数和阈值判断压力等级"""
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
