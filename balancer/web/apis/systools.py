# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonMeta, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

def is_false(value):
    return value is False
