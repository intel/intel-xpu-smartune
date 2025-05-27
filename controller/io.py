import os

class IOController:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount

    def set_limit(self, name: str, cgroup: str, weight: int) -> bool:
        pass
