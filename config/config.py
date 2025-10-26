import yaml
from dataclasses import dataclass

@dataclass
class Config:
    psi_interval: float = 5.0
    cgroup_mount: str = "/sys/fs/cgroup"
    thresholds: dict = None
    weights: dict = None
    weights_top: dict = None
    workloads: dict = None
    balance_service: dict = None
    app_priority: dict = None

    @classmethod
    def from_file(cls, path: str):
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
