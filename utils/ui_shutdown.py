# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# "Stop service" from the dashboard. The packaged deployments start the service
# from the desktop icon and run it until the user stops it, so the dashboard
# offers a button for that; a run from the source tree is started and stopped
# from a terminal or systemctl and never offers it.
#
# Both service entry points (monitor.monitor_service, balancer.balance_service)
# call enable() only when SMARTUNE_UI_SHUTDOWN is set, which the deb's run
# scripts do. Until then available() is False and request() refuses.
#
# Busy checks name work that stopping would cut short (a benchmark job, apps
# under active control). They do not block the stop: the dashboard lists them
# in its confirmation so the user knows what the stop ends or undoes.

import threading

from utils.logger import get_logger
logger = get_logger(__name__)

# Long enough for the HTTP response to reach the browser before the server
# starts tearing down, so the dashboard learns the stop was accepted.
_RESPONSE_GRACE_S = 0.5

_lock = threading.Lock()
_handler = None
_requested = False
_busy_checks = {}   # name -> callable returning a truthy value (or a reason) while busy


def add_busy_check(name: str, check) -> None:
    """Register `check`, called with no arguments, as work a stop would end.

    It returns falsy when idle, and True or a reason (str) when busy. Reasons
    and names are noun phrases, listed under "This also ends:". A check
    that raises counts as busy: better a needless warning than a stop the user
    was not told about."""
    with _lock:
        _busy_checks[name] = check


def busy_reasons() -> list:
    """What a stop right now would cut short, one entry per busy check."""
    with _lock:
        checks = list(_busy_checks.items())
    reasons = []
    for name, check in checks:
        try:
            result = check()
        except Exception as exc:
            logger.warning(f"Busy check {name!r} failed: {exc}")
            reasons.append(name)
            continue
        if result:
            reasons.append(result if isinstance(result, str) else name)
    return reasons


def enable(handler) -> None:
    """Offer the dashboard's stop button; `handler` performs the stop."""
    global _handler
    with _lock:
        _handler = handler


def available() -> bool:
    with _lock:
        return _handler is not None


def request() -> bool:
    """Stop the service shortly (see _RESPONSE_GRACE_S). False if not enabled."""
    global _requested
    with _lock:
        handler = _handler
        if handler is None:
            return False
        if _requested:
            return True
        _requested = True
    reasons = busy_reasons()
    logger.info("Stop requested from the dashboard"
                + (f" while busy: {'; '.join(reasons)}." if reasons else "."))
    timer = threading.Timer(_RESPONSE_GRACE_S, handler)
    timer.daemon = True
    timer.start()
    return True
