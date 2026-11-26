# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Best-effort persistence boundary for diagnostics detector scan cursors."""


def load_position(source):
    """``(cursor_epoch, cursor_text)`` for a source; ``(0.0, None)`` when unset."""
    try:
        from db.DatabaseModel import DetectorCursor
        return DetectorCursor.get_position(source)
    except Exception:
        return 0.0, None


def save_cursor(source, cursor_epoch, cursor_text=None):
    try:
        from db.DatabaseModel import DetectorCursor
        return DetectorCursor.save_cursor(source, cursor_epoch, cursor_text)
    except Exception:
        return False
