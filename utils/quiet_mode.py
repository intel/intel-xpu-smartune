# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Quiet mode: a process-wide gate that suspends SmarTune's own background
# activity for the duration of a benchmark run.
#
# The problem it solves is not average overhead -- the collectors are cheap --
# but that the background load is not *constant*: it depends on which dashboard
# page happens to be open, on how `monitored_sections` is configured, on whether
# a retention sweep lands inside a case window, and on whether the balancer
# limits or restores an app mid-run. Two runs of the same model are therefore not
# comparable. While the gate is up, the load a run sees is independent of the UI.
#
# Everything is in memory and nothing is written to config.yaml: a crash or a
# restart necessarily clears the gate. That is deliberate -- a SmarTune wedged in
# quiet mode has stopped being a monitor. Three things unwedge it: this
# in-memory-only rule, the job listener in benchmark.service.quiet (which fires
# on every terminal status), and the TTL lease below, renewed by the sampler
# thread and reclaimed by a watchdog if that thread dies.
#
# All consumers depend on this module one-way. It lives in utils/ because both
# benchmark/ and balancer/ are optional components (the monitor-only package
# ships neither), so neither of them can own it.

import os
import threading
import time

from utils.logger import logger

# How long a lease survives without a heartbeat. The sampler renews every 0.5s,
# so this is not a tuning knob for the normal path -- it only bounds how long the
# gate can outlive a sampler thread that died without its job reaching a terminal
# status. Generous, because a wedged collector can stall a tick for seconds and
# dropping the gate mid-run would silently corrupt the run it was protecting.
DEFAULT_TTL_S = 120.0

_WATCHDOG_POLL_S = 5.0


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


class QuietModeManager:
    """Holds the gate state, the lease that backs it, and the preflight checks.

    Two flags, not one, because they answer different questions:

    * ``_owner`` -- which run is holding quiet mode. Set for as long as the run
      lives, regardless of what the user does in the UI.
    * ``_gate_up`` -- whether collectors should actually stand down right now.
      The UI switch toggles this one, so a user who wants to watch live system
      data during a run can have it without ending the run's hold.

    Keeping them separate is what makes the switch re-entrant: turning the gate
    back on mid-run needs no new lease, and the run still knows (via
    ``user_exited``) that its numbers were taken in a dirtied environment.
    """

    def __init__(self):
        self.ttl_s = _env_float("SMARTUNE_QUIET_MODE_TTL", DEFAULT_TTL_S)
        self.poll_interval = _env_float("SMARTUNE_QUIET_MODE_POLL", _WATCHDOG_POLL_S)

        self._lock = threading.Lock()
        self._owner = None           # opaque holder id (a run_id), or None
        self._gate_up = False        # is the gate in effect right now?
        self._since = None           # time.time() when the current hold started
        self._deadline = 0.0         # monotonic lease expiry
        self._user_exited = False    # has the user dropped the gate during this hold?

        self._blockers = {}          # name -> callable
        self._thread = None
        self._stop = threading.Event()

    # -- the hot path --------------------------------------------------------

    def is_active(self) -> bool:
        """Whether background activity should stand down right now.

        Called from every gated collector loop, so it stays a lock plus two
        comparisons. It re-checks the lease itself rather than trusting the
        watchdog: if that thread ever dies, an expired lease must still read as
        inactive, or the gate would outlive everything meant to release it.
        """
        with self._lock:
            if self._owner is None or not self._gate_up:
                return False
            if time.monotonic() > self._deadline:
                return False
            return True

    # -- hold lifecycle ------------------------------------------------------

    def enter(self, owner: str, ttl: float = None) -> None:
        """Take the gate on behalf of ``owner``. Idempotent per owner.

        A different owner taking over is logged rather than refused: only one
        benchmark run exists at a time (jobs.JobManager is single-slot), so this
        can only mean a previous hold leaked, and the newer run is the truthful
        one.
        """
        ttl_s = self.ttl_s if ttl is None else float(ttl)
        with self._lock:
            # An expired hold counts as absent even if the watchdog has not got
            # round to clearing it yet, so taking over from one is not reported
            # as a leak.
            expired = (self._owner is not None
                       and time.monotonic() > self._deadline)
            previous = None if expired else self._owner
            if previous == owner:
                self._deadline = time.monotonic() + ttl_s
                return
            self._owner = owner
            self._gate_up = True
            self._user_exited = False
            self._since = time.time()
            self._deadline = time.monotonic() + ttl_s
            self._arm_watchdog_locked()
        if previous is not None:
            logger.warning(
                f"Quiet mode taken over by {owner!r} while {previous!r} still held it; "
                "the earlier hold leaked."
            )
        # An infinite TTL is the no-sampler fallback, and it is the one case
        # where only the job listener can end the hold -- say so, rather than
        # logging "ttl=infs" at whoever is working out why the gate is stuck.
        lease = "no lease (released on job end only)" if ttl_s == float("inf") \
            else f"ttl={ttl_s:g}s"
        logger.info(f"Quiet mode entered by {owner!r} ({lease}).")

    def exit(self, owner: str = None) -> bool:
        """Release the gate. Returns whether this call was the one that released it.

        ``owner=None`` forces the release regardless of holder -- for the
        watchdog and for administrative recovery. A non-matching owner is a
        no-op, so a late callback from a finished run cannot cancel the hold of
        the run that started after it.
        """
        with self._lock:
            if self._owner is None:
                return False
            if owner is not None and owner != self._owner:
                return False
            released, self._owner = self._owner, None
            self._gate_up = False
            self._since = None
            self._deadline = 0.0
            self._user_exited = False
        logger.info(f"Quiet mode released (owner={released!r}).")
        return True

    def heartbeat(self, owner: str) -> None:
        """Renew ``owner``'s lease. No-op if it is not the holder.

        Renewed even while the user has the gate down: the lease exists to stop
        the hold from leaking, not to record whether the gate is up.

        An already-expired lease is *not* revived. A sampler that went quiet for
        longer than the TTL has already had monitoring restored underneath it,
        and re-raising the gate on its next tick would only make the background
        load flap -- the one thing quiet mode exists to prevent. From there the
        job lifecycle is what ends the hold.
        """
        with self._lock:
            if self._owner == owner and time.monotonic() <= self._deadline:
                self._deadline = time.monotonic() + self.ttl_s

    # -- the UI switch -------------------------------------------------------

    def set_gate(self, up: bool) -> bool:
        """Raise or drop the gate without touching the hold. Returns the new state.

        Dropping it is what the "exit quiet mode" control does: collectors,
        history writes and the balancer's automatic control all come back, and
        the run's own ``quiet_held`` is recorded as False so the results are not
        later mistaken for clean ones.
        """
        with self._lock:
            if self._owner is None:
                return False
            self._gate_up = bool(up)
            if not up:
                self._user_exited = True
            return self._gate_up

    def held_clean(self) -> bool:
        """Whether the current hold has never been interrupted by the user."""
        with self._lock:
            return self._owner is not None and not self._user_exited

    def state(self) -> dict:
        """A snapshot for the UI."""
        with self._lock:
            expired = self._owner is not None and time.monotonic() > self._deadline
            return {
                "active": bool(self._owner is not None and self._gate_up and not expired),
                "held": self._owner is not None and not expired,
                "owner": self._owner,
                "since": self._since,
                "user_exited": self._user_exited,
            }

    # -- preflight blockers --------------------------------------------------

    def register_blocker(self, name: str, check) -> None:
        """Register a preflight check that can refuse a run.

        ``check()`` returns a falsy value when clear, or a dict (merged into the
        reported hit) / a string reason when it blocks. Registration is by name
        so re-registering replaces rather than accumulates.

        The registry is the reason a monitor-only deployment needs no special
        case: nothing registers, so nothing blocks.
        """
        with self._lock:
            self._blockers[name] = check

    def check_blockers(self) -> list:
        """Run every check; return the hits that block entry, in registration order."""
        with self._lock:
            registered = list(self._blockers.items())

        hits = []
        for name, check in registered:
            try:
                result = check()
            except Exception:
                # A broken check must not ground every benchmark run: this gate
                # guards data quality, not safety. Log loudly and let the run go.
                logger.exception(
                    f"Quiet-mode blocker {name!r} raised; treating it as clear."
                )
                continue
            if not result:
                continue
            if isinstance(result, dict):
                hits.append({"name": name, **result})
            else:
                hits.append({"name": name, "reason": str(result)})
        return hits

    # -- lease watchdog ------------------------------------------------------

    def _arm_watchdog_locked(self) -> None:
        """Start the reclaim thread on first use. Caller holds the lock."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch, name="quiet-mode-watchdog", daemon=True
        )
        self._thread.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.poll_interval):
            with self._lock:
                owner = self._owner
                expired = owner is not None and time.monotonic() > self._deadline
            if expired:
                logger.warning(
                    f"Quiet-mode lease held by {owner!r} expired after "
                    f"{self.ttl_s:.0f}s without a heartbeat; restoring normal "
                    "monitoring. The run's sampler thread probably died."
                )
                self.exit()


_manager = None
_manager_lock = threading.Lock()


def get_quiet_mode_manager() -> QuietModeManager:
    """Return the process-wide quiet-mode manager (created on first use)."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = QuietModeManager()
    return _manager


# Module-level shorthands. The gated call sites read better as
# `quiet_mode.is_active()` than as a manager lookup, and `is_active` in
# particular sits in several hot loops.

def is_active() -> bool:
    return get_quiet_mode_manager().is_active()


def enter(owner: str, ttl: float = None) -> None:
    get_quiet_mode_manager().enter(owner, ttl)


def exit(owner: str = None) -> bool:  # noqa: A001 - mirrors enter()
    return get_quiet_mode_manager().exit(owner)


def heartbeat(owner: str) -> None:
    get_quiet_mode_manager().heartbeat(owner)


def set_gate(up: bool) -> bool:
    return get_quiet_mode_manager().set_gate(up)


def held_clean() -> bool:
    return get_quiet_mode_manager().held_clean()


def state() -> dict:
    return get_quiet_mode_manager().state()


def register_blocker(name: str, check) -> None:
    get_quiet_mode_manager().register_blocker(name, check)


def check_blockers() -> list:
    return get_quiet_mode_manager().check_blockers()
