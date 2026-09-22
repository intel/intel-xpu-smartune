# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os

class FanController:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
