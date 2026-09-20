# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Bounded retention for the diagnostics event ledger and derived read models."""

import json
import os
import threading
import time

from utils.logger import get_logger

logger = get_logger(__name__)

RETENTION_DEFAULT_DAYS = 3
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 7
_CLEANUP_INTERVAL_SECONDS = 60 * 60
_started = False
_start_lock = threading.Lock()

_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "runtime_state.json")


def configured_days():
    """Use the monitor history setting so structured evidence shares its window."""
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        days = int(settings.get("snapshot_retention_days", RETENTION_DEFAULT_DAYS))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        days = RETENTION_DEFAULT_DAYS
    return max(RETENTION_MIN_DAYS, min(days, RETENTION_MAX_DAYS))


def run_cleanup(days=None):
    """Remove expired history while preserving unresolved operational state."""
    from db.DatabaseModel import AlertState, OperationalEvent

    days = configured_days() if days is None else days
    return {
        "retention_days": days,
        "events_deleted": OperationalEvent.delete_older_than(days),
        "resolved_alerts_deleted": AlertState.delete_resolved_older_than(days),
    }


def status():
    """Expose the retention contract used by diagnostics history queries."""
    days = configured_days()
    return {
        "retention_days": days,
        "retention_setting": "snapshot_retention_days",
        "event_ledger": "expired after retention period, except unresolved control lifecycles",
        "resolved_alerts": "expired after retention period",
        "active_alerts": "retained until resolved",
        "monitor_snapshots": "managed separately by monitor history retention",
        "raw_logs": "managed by their source retention policies",
    }


def start_cleanup_loop():
    """Start the diagnostics cleanup worker once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                result = run_cleanup()
                logger.info("Diagnostics retention cleanup: %s", result)
            except Exception as exc:
                logger.warning("Diagnostics retention cleanup failed: %s", exc)
            time.sleep(_CLEANUP_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="diag-retention").start()