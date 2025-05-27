import os

class FanController:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
