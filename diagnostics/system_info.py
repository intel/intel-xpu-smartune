# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Single owner of the host-identity reads diagnostics needs.

Kept apart from any one consumer so the boot-id source lives in one place: the
event enricher, the context assembler and the lifecycle reconciler all read it
from here instead of each re-opening ``/proc``.
"""


def read_boot_id():
    """Return the current boot id, or None when it cannot be read."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None
