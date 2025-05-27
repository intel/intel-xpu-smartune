import os

class PSIMonitor:
    RESOURCES = ['cpu', 'memory', 'io']

    def __init__(self):
        self.base_path = "/proc/pressure"

    def read_pressure(self, resource: str, metric: str = 'some'):
        """Read PSI values for a resource"""
        path = os.path.join(self.base_path, resource)
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith(metric):
                        parts = line.split()
                        #return float(parts[4].split('=')[1])
                        # avg10
                        return float(parts[1].split('=')[1])
        except FileNotFoundError:
            return 0.0
        return 0.0

    def get_current_pressure(self):
        """Get current pressure for all resources"""
        return {
            res: self.read_pressure(res)
            for res in self.RESOURCES
        }
