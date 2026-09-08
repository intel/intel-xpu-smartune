# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# UI lease manager: lets the server shut itself down once no dashboard UI is
# open any more. The packaged monitor is launched on demand by the desktop icon
# and runs as root indefinitely; there is no reliable browser "close" event to
# key off (the launcher xdg-opens the URL and exits). Instead each open UI tab
# holds a *lease*: it POSTs a heartbeat every few seconds and (best-effort) a
# release on pagehide. A watchdog thread stops the service once every lease has
# lapsed, so closing the last tab/window quietly winds the service down.
#
# The manager tracks leases unconditionally (cheap), but only arms the watchdog
# when enable() is called — the standalone dev run and the balancer process
# leave it disarmed, so they never self-exit. See monitor.monitor_service, which
# arms it only when SMARTUNE_UI_LEASE is set (by packaging/deb/app/run_monitor.sh).

import os
import threading
import time

from utils.logger import logger


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to default on any error."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(f"Invalid {name}={raw!r}; using default {default}.")
        return default


class UiLeaseManager:
    """Track open-UI leases and, once armed, stop the service when the last lapses.

    Each session_id maps to a monotonic deadline. A session is *active* while its
    deadline is in the future. A heartbeat pushes the deadline out by
    HEARTBEAT_GRACE; a release pulls it in to a short RELEASE_GRACE (prompt
    shutdown on a genuine close, while still leaving a small window for a
    bfcache restore / reload to re-heartbeat).
    """

    def __init__(self):
        # Grace/poll windows (seconds). Generous heartbeat grace so a merely
        # backgrounded tab — whose timers the browser throttles to ~1/min — is
        # not mistaken for a closed one; the release beacon handles the common
        # close case promptly, this only backstops crashes / kills / lost network.
        self.heartbeat_grace = _env_float("SMARTUNE_UI_HEARTBEAT_GRACE", 90.0)
        self.release_grace = _env_float("SMARTUNE_UI_RELEASE_GRACE", 12.0)
        # If a UI never connects at all (auth cancelled, xdg-open failed, ...),
        # stop rather than linger as a root process forever.
        self.startup_grace = _env_float("SMARTUNE_UI_STARTUP_GRACE", 120.0)
        self.poll_interval = _env_float("SMARTUNE_UI_POLL_INTERVAL", 5.0)

        self._lock = threading.Lock()
        self._sessions = {}          # session_id -> monotonic deadline
        self._any_seen = False       # has any UI ever held a lease?
        self._started_at = time.monotonic()

        self._enabled = False
        self._shutdown_cb = None
        self._stop = threading.Event()
        self._thread = None

    # -- lease bookkeeping (always active, even when disarmed) ---------------

    def heartbeat(self, session_id: str) -> None:
        with self._lock:
            self._any_seen = True
            self._sessions[session_id] = time.monotonic() + self.heartbeat_grace

    def release(self, session_id: str) -> None:
        with self._lock:
            self._any_seen = True
            soon = time.monotonic() + self.release_grace
            existing = self._sessions.get(session_id)
            # Never push a deadline further out on release; only pull it in.
            self._sessions[session_id] = soon if existing is None else min(existing, soon)

    # -- watchdog ------------------------------------------------------------

    def enable(self, shutdown_cb) -> None:
        """Arm the watchdog. Idempotent — a second call is a no-op."""
        with self._lock:
            if self._enabled:
                return
            self._enabled = True
            self._shutdown_cb = shutdown_cb
            self._started_at = time.monotonic()
            self._thread = threading.Thread(
                target=self._watch, name="ui-lease-watchdog", daemon=True
            )
            self._thread.start()
        logger.info(
            "UI-lease watchdog armed "
            f"(heartbeat_grace={self.heartbeat_grace}s, release_grace={self.release_grace}s, "
            f"startup_grace={self.startup_grace}s, poll={self.poll_interval}s)."
        )

    def _should_stop(self, now: float) -> bool:
        """Decide, under lock, whether the service should shut down now.

        Also prunes lapsed sessions so the map does not grow without bound.
        """
        active = 0
        for sid in list(self._sessions):
            if self._sessions[sid] > now:
                active += 1
            else:
                del self._sessions[sid]
        if active:
            return False
        if self._any_seen:
            # A UI connected and now none remain.
            return True
        # No UI ever connected — give the browser a window to come up first.
        return (now - self._started_at) > self.startup_grace

    def _watch(self) -> None:
        while not self._stop.wait(self.poll_interval):
            with self._lock:
                stop = self._should_stop(time.monotonic())
            if stop:
                logger.info("No active UI leases remain; stopping the service.")
                self._stop.set()
                try:
                    self._shutdown_cb()
                except Exception:
                    logger.exception("UI-lease shutdown callback failed.")
                return


_manager = None
_manager_lock = threading.Lock()


def get_ui_lease_manager() -> UiLeaseManager:
    """Return the process-wide UI lease manager (created on first use)."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = UiLeaseManager()
    return _manager
