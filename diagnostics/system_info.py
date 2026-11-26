# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Single owner of the host-identity reads diagnostics needs.

Kept apart from any one consumer so the boot-id source lives in one place: the
event enricher, the context assembler and the lifecycle reconciler all read it
from here instead of each re-opening ``/proc``.
"""


def normalize_boot_id(value):
    """A boot id in journald's form (32 hex digits, no dashes); compare boot ids through this."""
    if not isinstance(value, str):
        return value
    return value.strip().replace("-", "").lower() or None


def read_boot_id():
    """Return the current boot id in journald's form (32 hex digits), or None."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as handle:
            return normalize_boot_id(handle.read())
    except OSError:
        return None
