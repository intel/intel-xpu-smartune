# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Best-effort persistence boundary for diagnostics detector scan cursors."""


def load_cursor(source):
    try:
        from db.DatabaseModel import DetectorCursor
        return DetectorCursor.get_cursor(source)
    except Exception:
        return 0.0


def save_cursor(source, cursor_epoch):
    try:
        from db.DatabaseModel import DetectorCursor
        return DetectorCursor.save_cursor(source, cursor_epoch)
    except Exception:
        return False