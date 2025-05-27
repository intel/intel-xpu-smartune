import math

class PressureAnalyzer:
    def __init__(self, config):
        self.config = config
        self.weights = config.weights or {
            'cpu':    0.3,
            'memory': 0.7,
            'io':     0.1
        }

    def calculate_pressure_score(self, psi_data: dict) -> float:
        """Calculate weighted pressure score"""
        score = (self.weights['cpu'] * psi_data.get('cpu', 0)      +
                self.weights['memory'] * psi_data.get('memory', 0) +
                self.weights['io'] * psi_data.get('io', 0))

        factor = 10 ** 2
        return math.trunc(score * factor) / factor

    def get_pressure_level(self, score: float) -> str:
        """Determine pressure level"""
        if score > self.config.thresholds.get('critical', 80):
            return 'critical'
        elif score > self.config.thresholds.get('high', 60):
            return 'high'
        elif score > self.config.thresholds.get('medium', 40):
            return 'medium'
        return 'low'
