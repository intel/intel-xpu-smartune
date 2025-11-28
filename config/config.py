import yaml
from dataclasses import dataclass


@dataclass
class Config:
    psi_interval: float = 5.0
    cgroup_mount: str = "/sys/fs/cgroup"
    vendor: str = "generic"
    thresholds: dict = None
    weights: dict = None
    weights_top: dict = None
    workloads: dict = None
    balance_service: dict = None
    app_priority: dict = None
    blacklist: list = None
    cooldown_time: float = 15
    cpu_busy_threshold: float = 90
    memory_busy_threshold: float = 90
    disk_utilization_threshold: float = 90
    regular_update_sys_pressure_time: float = 5

    @classmethod
    def from_file(cls, path: str):
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


b_config = Config.from_file("config/config.yaml")

