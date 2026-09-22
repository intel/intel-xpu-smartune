# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

class PSIController:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
