# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import os, signal, subprocess, time
import psutil
from functools import wraps
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union

from collections import OrderedDict
from controller.app_intercept import AppIntercept

from utils.logger import get_logger
logger = get_logger(__name__)
try:
    # record_control_action is the diagnostics control seam; it is internally
    # best-effort, so the only thing to guard is the import itself (a minimal
    # deployment may strip the diagnostics package). Fall back to a no-op so
    # control flow is unchanged.
    from diagnostics import record_control_action
except Exception:  # pragma: no cover - diagnostics absent
    def record_control_action(*_a, **_k):
        return None
from utils import app_utils, quiet_mode
from config.config import b_config
import threading
from multiprocessing import JoinableQueue
import queue
import heapq
from controller.network import NetworkController
from controller.io import IOController
from controller.swap import MEM_RISE_TRIGGER
from monitor.disk_pressure import media_for_disk


IO_LIMIT_MBPS_THRESHOLD = 100
IO_LIMIT_IOPS_THRESHOLD = 1000


def _protection_id(source: str, public_app_id: str, effective_app_id: str, limited_at: float) -> str:
    """Stable ID shared by each resource event in one limit lifecycle."""
    material = f"{source}:{public_app_id}:{effective_app_id}:{limited_at:.6f}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _control_resources(parts: dict, resource_parts: dict) -> list:
    """Map the balancer's channel/part flags onto the neutral resource vocabulary.

    CPU and memory travel together as one control channel here; the ledger names
    them individually. Keeping this translation on the balancer side stops the
    internal ``parts``/``resource_parts`` shape from leaking across the
    diagnostics boundary -- diagnostics only ever sees CPU / MEMORY / DISK_IO.
    """
    resources = []
    if parts.get("cpu_mem_limited"):
        cpu = bool(resource_parts.get("cpu"))
        memory = bool(resource_parts.get("memory"))
        # cpu_mem_limited means the channel is capped. When the split names
        # neither -- a merge can leave {cpu:False, memory:False} -- attribute to
        # both rather than drop the resource, so RECOVERED stays symmetric with
        # APPLIED.
        if not (cpu or memory):
            cpu = memory = True
        if cpu:
            resources.append("CPU")
        if memory:
            resources.append("MEMORY")
    if parts.get("io_limited"):
        resources.append("DISK_IO")
    return resources


def _emit_control_events(action: str, parts: dict, *, app_id: str, app_name: str,
                         protection_id: str, attributes: dict,
                         control_mode: str = "auto") -> None:
    """Hand a completed limit action to diagnostics as neutral control facts."""
    attributes = dict(attributes or {})
    attributes.setdefault("control_mode", control_mode)
    attributes.setdefault("priority", "undefined")
    resource_parts = attributes.get("resource_parts") or {}
    record_control_action(
        action, app_id=app_id, app_name=app_name, protection_id=protection_id,
        resources=_control_resources(parts, resource_parts), facts=attributes,
    )


_MB = 1024 * 1024

# Ceiling on the EAGAIN backoff.  EAGAIN means swap is full, which is a
# condition L0 fixes or nobody does; doubling without a cap would leave an app
# un-reclaimable for hours after the headroom came back.
_RECLAIM_MAX_BACKOFF = 600.0

# Minimum spacing between swap-growth attempts.  Growth is cheap but not free,
# and a volume that keeps refusing must not get an mkswap attempt every tick.
_SWAP_GROWTH_INTERVAL = 60.0
# ...except under critical pressure, where the 60 s spacing cancels the very
# bypass that exists to serve critical: the monitor loop skips
# idle_check_interval on the critical rising edge, and that bypassed tick would
# then return without evaluating because 60 s had not elapsed.  The interval
# above guards against retrying a *refusal* (full volume, unsupported
# filesystem) every tick; it must not decide how long a machine in trouble
# waits for its next look at the watermarks.
_SWAP_GROWTH_URGENT_INTERVAL = 10.0

# How long after our own reclaim the disk-IO arm stays quiet.
#
# Reclaim pushes anon pages to swap, and swap bios are real device traffic --
# enough to drive the disk channel critical within seconds.  Acting on that is
# wrong twice over: the pressure is ours, not the candidate's, and it is gone as
# soon as the reclaim ends.  The arm cannot tell on its own, because
# `/proc/<pid>/io` only sees process-context IO so kernel writeback never shows
# up against the app that caused it.  (`self_disc` does not cover this either:
# it discounts a *limited app's* own stall on its own io.max, while reclaim
# stalls land on the daemon's cgroup.)
#
# The window has to outlast the signal, not the write: the disk score is an
# EWMA (alpha 0.3) over the pressure monitor's refresh, so our contribution
# lingers for several samples after the last byte.  20 s sheds most of it while
# still letting a genuinely heavy writer be throttled on the next tick but one.
_RECLAIM_DISK_QUIET_SEC = 20.0


def _emit_reclaim_event(event_type: str, severity: str, summary: str,
                        entry: "LimitedApp", attributes: dict) -> None:
    """Report one L2 outcome to diagnostics.  Best-effort, never raises."""
    try:
        from diagnostics import emit_event
    except Exception:  # pragma: no cover - diagnostics absent
        return
    try:
        emit_event(event_type, severity=severity, category="resource.memory",
                   summary=summary, source="balancer",
                   app_id=entry.public_app_id, resource_type="memory",
                   protection_id=entry.protection_id,
                   attributes=dict(attributes, priority=entry.priority))
    except Exception as exc:  # pragma: no cover - diagnostics is not load-bearing
        logger.warning("Failed to emit %s: %s", event_type, exc)


def _emit_auto_handoff_recovery(entry: "LimitedApp") -> None:
    """Close the auto lifecycle when its live caps become manual-owned."""
    _emit_control_events(
        "RECOVERED", entry.limit_parts, app_id=entry.public_app_id,
        app_name=entry.app_name, protection_id=entry.protection_id,
        attributes={
            "app_name": entry.app_name,
            "priority": entry.priority,
            "limit_parts": dict(entry.limit_parts),
            "resource_parts": entry.resource_parts,
            "cgroups": entry.cgroups,
            "handoff": "manual",
        },
    )


def _serialized_limit_operation(operation):
    """Keep cgroup ownership checks and writes in one atomic operation."""
    @wraps(operation)
    def guarded(self, *args, **kwargs):
        with self.all_limits.operation_lock:
            return operation(self, *args, **kwargs)
    return guarded


@dataclass
class LimitedApp:
    """All runtime state for one app currently under a resource limit.

    A single ``LimitRegistry.apps`` entry keyed by the primary effective
    app id (the lexicographically-first cgroup basename).  ``source``
    distinguishes pressure-driven ("auto") from REST/UI ("manual") limits
    so the pressure loop never restores or replaces a manual limit.

    Fields:
      * public_app_id — the public app id (DB primary key) used for
            status updates and SSE callbacks.  For auto limits it may
            equal the cgroup key.
      * limit_rates   — the rate config used when the limit was applied.
      * limit_parts   — {'cpu_mem_limited': bool, 'io_limited': bool}.
      * state         — ``None`` for fully limited, ``"partially_restored"``
            once *any* channel has been partially restored (auto only).
      * partial_parts — {'sys': bool, 'disk_io': bool}: which channel has
            already had its staged (2x) relaxation.  The two channels recover
            on independent timers, so one flag per channel is required --
            ``state`` stays as the coarse "has been relaxed at all" signal
            that the PSI-dominant check reads.
      * cgroups       — [primary, *extras]; multi-cgroup apps fan restores
            out across every entry.
      * limit_disks   — disk names the io.max cap was written to, empty for
            "every disk".  Restores must target exactly this set: a partial
            restore writes a real (2x) cap, so replaying it against every disk
            puts a limit on disks that were never throttled.
      * pids          — snapshot of the app's PIDs at limit time, used by
            the reaper to detect that the app has closed (see
            DynamicBalancer._is_app_closed).
      * priority      — the app's configured priority at limit time
            ("undefined" for uncontrolled apps), reported to the UI.
      * is_controlled — True when the app has a DB row.  Decides which
            identity an auto-limit exclusion is keyed by (see
            LimitRegistry.add_exclusion).
      * limit_reason  — which pressure channel triggered the limit:
            "system_pressure" or "disk_pressure".
      * pressure_level — the level of that channel when the limit landed.
    """
    public_app_id: str
    app_name: str
    source: str                              # "auto" | "manual"
    limit_rates: dict
    limit_parts: dict
    resource_parts: dict = field(default_factory=dict)
    state: Optional[str] = None
    priority: str = "undefined"
    is_controlled: bool = False
    limit_reason: str = ""
    pressure_level: str = ""
    partial_parts: dict = field(
        default_factory=lambda: {'sys': False, 'disk_io': False})
    cgroups: list = field(default_factory=list)
    limit_disks: list = field(default_factory=list)
    pids: set = field(default_factory=set)
    representative_pid: Optional[int] = None
    limited_at: float = 0.0                   # epoch seconds when the limit was applied
    protection_id: str = ""                   # stable control-event lifecycle identity
    adopted_from_auto: bool = False           # retain the auto-limit cgroup scope in Manual Control
    frozen: bool = False                      # manual Freeze is active (cgroup.freeze=1 on every cgroup)
    frozen_at: float = 0.0                    # epoch seconds when it was frozen
    freeze_notified: bool = False             # the "still frozen" reminder was sent for this freeze
    # --- L2 (memory.reclaim) state.  No restore path reads these: reclaim is a
    # one-shot move of pages to swap, and the app faults back whatever it still
    # needs, so there is nothing to undo when the limit is lifted.
    last_reclaim_at: float = 0.0              # epoch seconds, for the cooldown
    reclaim_backoff: float = 0.0              # extra seconds on top of the cooldown,
                                              # doubled on each EAGAIN (swap exhausted)
    last_cpu_usec: Optional[int] = None       # previous cpu.stat sample, for the idle gate
    last_cpu_sample_at: float = 0.0           # when that sample was taken
    reclaim_inflight: bool = False            # a chunked reclaim is on the executor


class LimitRegistry:
    """Runtime registry of every app currently under a resource limit.

    Fields:
      * apps — OrderedDict[primary_effective_app_id, LimitedApp].  The
            single source of truth for both auto- and manual-limited apps
            (see ``LimitedApp.source``).  Insertion order is preserved so
            the auto restore path can pop the oldest limit first.
      * manual_limit_baseline — {effective_app_id: peak usage snapshot}
            that persists across restore→limit cycles to keep the
            manually-applied cap from tightening when a second sample
            (taken under an active limit) reports a lower value than the
            original peak.  Kept separate from ``apps`` on purpose: its
            lifetime outlives an individual LimitedApp entry (a manual
            restore removes the entry but intentionally keeps the peak).
      * is_limited_app_dominant — True when the current top process is
            one we already limited; the pressure loop reads this so it
            doesn't count its own throttled traffic as fresh pressure.
      * auto_limit_exclusions — {key: exclusion dict} for apps the auto
            engine must not touch.  Each record carries a ``reason``:
              - "user_restore": the user hand-restored an auto limit and does
                not want it auto-limited again this run.
              - "manual_limit": the user set (or took over as) a manual limit;
                auto must not re-limit an app the user has claimed.  Cleared
                when the manual limit is restored or the app closes.
            Process-lifetime only (never persisted): the semantics are
            "leave it alone for this run of the service".
      * lock — guards every mutation of ``apps`` and
            ``auto_limit_exclusions`` so the reaper thread and the REST
            manual limit/restore calls never race.
    """

    def __init__(self):
        self.apps: "OrderedDict[str, LimitedApp]" = OrderedDict()
        self.manual_limit_baseline: Dict[str, dict] = {}
        self.is_limited_app_dominant: bool = False
        self.auto_limit_exclusions: "OrderedDict[str, dict]" = OrderedDict()
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()

    # --- Query helpers (preserve the ordering semantics the callers rely on) ---
    def first_auto(self) -> "Optional[tuple[str, LimitedApp]]":
        """Return the oldest auto-limited (key, LimitedApp), or None.

        Preserves FIFO-head ordering while filtering unified entries by source.
        """
        for key, app in self.apps.items():
            if app.source == "auto":
                return key, app
        return None

    def first_auto_with_part(self, part: str,
                             skip_partially_restored: bool = False) -> "Optional[tuple[str, LimitedApp]]":
        """Oldest auto-limited entry that still holds *part*, or None.

        *part* is a ``limit_parts`` key ('cpu_mem_limited' / 'io_limited').
        The separated policy recovers the sys and disk-IO channels on their own
        timers, so a restore must pick a target that actually carries the
        channel whose timer fired — ``first_auto()`` returns the oldest entry
        regardless of channel, which under a mixed set of limits relaxes a cap
        whose own stability window has not elapsed.

        With *skip_partially_restored* the entries already relaxed on that
        channel are skipped, so a staged partial restore advances through the
        queue instead of re-examining the same head every tick.
        """
        channel = 'sys' if part == 'cpu_mem_limited' else 'disk_io'
        for key, app in self.apps.items():
            if app.source != "auto" or not app.limit_parts.get(part):
                continue
            if skip_partially_restored and app.partial_parts.get(channel):
                continue
            return key, app
        return None

    def by_public_id(self, public_app_id: str, source: Optional[str] = None) -> "Optional[tuple[str, LimitedApp]]":
        """Find the (key, LimitedApp) whose public_app_id matches, or None.

        When *source* is given, only entries of that source ("auto"/"manual")
        are considered.  The manual restore path passes ``source="manual"`` so
        a user-initiated restore can never pull an auto-limited app out of the
        pressure-driven staged-recovery flow.
        """
        for key, app in self.apps.items():
            if app.public_app_id == public_app_id and (source is None or app.source == source):
                return key, app
        return None

    def by_any_id(self, ident: str, source: Optional[str] = None) -> "Optional[tuple[str, LimitedApp]]":
        """Find the (key, LimitedApp) *ident* refers to under any of its names, or None.

        One limited app answers to several ids: the public app id, the primary cgroup it
        is keyed by, and every extra cgroup it spans.  A caller that only has one of them
        -- the disk-IO candidate loop holds the id the top-consumer sample reported, which
        for a resolved multi-cgroup app is none of the above by the time the limit lands --
        would otherwise conclude the app is unlimited and cap it again every tick.
        """
        for key, app in self.apps.items():
            if source is not None and app.source != source:
                continue
            if ident == key or ident == app.public_app_id or ident in (app.cgroups or []):
                return key, app
        return None

    # --- Auto-limit exclusions ("do not auto-limit this again") ---
    #
    # Created when the user restores an auto-limited app by hand. Two key spaces:
    #
    #   * "app:<app_id>" for controlled apps. Covers every instance, which is the unit
    #     the user sees. app_name matches too, since a controlled app can be sampled
    #     under its configured id or (cgroup fallback) under its process name.
    #   * "instance:<cgroup>" for uncontrolled ones. Three shells running `stress` sit in
    #     three vte-spawn-<uuid>.scope cgroups, so excluding one must leave the others
    #     throttleable; app_name is stored for display but not matched. Processes sharing
    #     one scope can't be separated, but neither can the limit, so the exclusion is as
    #     coarse as the limit was.
    def exclusion_key(self, is_controlled: bool, public_app_id: str, primary_cgroup: str) -> str:
        """Key an exclusion is stored under. See the note above for the two identities."""
        if is_controlled and public_app_id:
            return f"app:{public_app_id}"
        return f"instance:{primary_cgroup or public_app_id}"

    def add_exclusion(self, entry: "LimitedApp", reason: str = "user_restore") -> dict:
        """Record *entry*'s app as excluded from future auto-limiting.

        *reason* is why the app is exempt: "user_restore" (the user hand-restored an
        auto limit) or "manual_limit" (the user owns it via a manual limit). It is stored
        on the record so the UI can tell the two apart and the manual-limit exemption can
        be cleared on restore/close without disturbing user-restore exemptions.
        """
        primary_cgroup = (entry.cgroups or [entry.public_app_id])[0]
        key = self.exclusion_key(entry.is_controlled, entry.public_app_id, primary_cgroup)
        record = {
            "key": key,
            "kind": "app" if key.startswith("app:") else "instance",
            "reason": reason,
            "app_id": entry.public_app_id,
            "app_name": entry.app_name,
            "priority": entry.priority,
            "cgroups": list(entry.cgroups or []),
            "excluded_at": time.time(),
        }
        self.auto_limit_exclusions[key] = record
        return record

    def remove_exclusion(self, ident: str) -> Optional[dict]:
        """Drop the exclusion *ident* refers to (its key, or the app id/cgroup it holds)."""
        record = self.auto_limit_exclusions.pop(ident, None)
        if record is not None:
            return record
        for key, rec in list(self.auto_limit_exclusions.items()):
            if ident == rec.get("app_id") or ident in (rec.get("cgroups") or []):
                return self.auto_limit_exclusions.pop(key)
        return None

    def list_exclusions(self) -> list:
        """Every current exclusion, oldest first."""
        return [dict(rec) for rec in self.auto_limit_exclusions.values()]

    def is_excluded(self, app_id: str, app_name: str, cgroups=None) -> Optional[dict]:
        """The exclusion matching this candidate, or None.

        The caller passes whatever identity the top-consumer sample carries, which need
        not be the one the limit was keyed by, so both key spaces are checked.
        """
        if not self.auto_limit_exclusions:
            return None
        candidate_ids = {str(i) for i in ([app_id] + list(cgroups or [])) if i}
        lowered_name = (app_name or "").strip().lower()
        for rec in self.auto_limit_exclusions.values():
            if candidate_ids & ({str(rec.get("app_id") or "")} | set(rec.get("cgroups") or [])):
                return rec
            # Name matching is an "app"-kind privilege only; see the note above.
            if (rec.get("kind") == "app" and lowered_name
                    and lowered_name == (rec.get("app_name") or "").strip().lower()):
                return rec
        return None


@dataclass
class _MonitorLoopState:
    """Per-loop runtime state for ``DynamicBalancer._run_monitor_resource_loop``.

    Shared across the policy-specific tick methods so they can read and
    mutate the same loop variables.
    """
    default_idle_check_interval: float
    idle_check_interval: float
    last_check_time: float = 0.0
    last_reap_time: float = 0.0
    last_network_sample_time: float = 0.0
    network_sample_interval: float = 5.0          # network sampling interval (seconds)
    top_consume_apps: list = None
    reach_threshold: bool = False                 # some apps may have negligible resource usage; skip limiting them
    top_source: Optional[str] = None              # channel the held batch was sampled for: "sys" | "disk_io"
    top_fetched_at: float = 0.0                   # when the held batch was sampled (batch-age guard)
    restore_pending: bool = False                 # True when there are apps waiting to be restored
    pressure_start_time: Optional[float] = None   # timestamp when pressure entered medium/low
    current_pressure: Optional[str] = None        # current pressure level; used to detect stability
    disk_io_not_stressed_start_time: Optional[float] = None  # timestamp when disk IO pressure was relieved
    prev_critical: bool = False                   # critical seen on the previous loop iteration (rising-edge detection)
    critical_since: Optional[float] = None        # timestamp critical was first entered (sustained-recheck timer)
    last_sustained_recheck_time: float = 0.0      # last time a sustained-critical background recheck fired
    prev_pressure: Optional[str] = None
    prev_pre_oom: bool = False                    # memory pre-OOM band on the previous iteration (rising-edge detection)
    prev_passive_enabled: Optional[bool] = None   # passive switch on the previous iteration (falling-edge -> lock-to-manual)
    # Disk-IO top-consumer prefetch state (separated policy only), mirroring the
    # pressure fields above but tracked independently so the two channels never
    # perturb each other's rising-edge / sustained-recheck timers.
    prev_disk_level: Optional[str] = None         # disk level on the previous iteration (rising-edge detection)
    disk_high_since: Optional[float] = None        # timestamp disk entered high/critical (sustained-recheck timer)
    disk_last_recheck_time: float = 0.0            # last time a sustained disk background recheck fired
    current_time: float = 0.0

    # Stability thresholds, kept on the state object so the tick methods
    # can reach them directly.
    STABLE_PERIOD: int = 1800                     # 30-minute stability period (seconds)
    STABLE_DISK_IO_PERIOD: int = 300              # 5-minute disk IO stability period (seconds)
    # A candidate batch describes the machine at the moment it was sampled. The disk
    # channel holds an unconsumed batch while it sits at "high" (see
    # _tick_separated_policy), so without an age bound a batch can outlive the episode
    # it was warmed for by hours and be spent on apps that have since exited.
    TOP_BATCH_TTL: float = 30.0                   # max age of a held candidate batch (seconds)

    def __post_init__(self):
        if self.top_consume_apps is None:
            self.top_consume_apps = []

    def reset(self) -> None:
        """Clear transient state when the loop bails out of a tick."""
        self.drop_top_batch()
        self.idle_check_interval = self.default_idle_check_interval
        self.pressure_start_time = None

    def drop_top_batch(self) -> None:
        """Forget the held candidate batch and everything derived from it."""
        self.top_consume_apps = []
        self.reach_threshold = False
        self.top_source = None
        self.top_fetched_at = 0.0

    def keep_top_batch(self, apps: list, reach_threshold: bool, source: str, now: float) -> None:
        """Record a freshly sampled batch together with its channel and age."""
        self.top_consume_apps = apps
        self.reach_threshold = reach_threshold
        self.top_source = source
        self.top_fetched_at = now

    def stale_top_batch_reason(self, source: str, now: float) -> Optional[str]:
        """Why the held batch must not be reused for *source*, or None.

        Two disqualifiers, both observed in the field:
          * it was sampled for the other channel — the sys channel ranks apps by
            CPU/memory and the disk channel by IO, so spending a disk batch on a
            CPU/memory critical caps an app nobody measured as a CPU/memory hog
            (and vice versa);
          * it is older than ``TOP_BATCH_TTL`` — the pressure that justified it
            is no longer the pressure being handled.
        """
        if not self.top_consume_apps:
            return None
        if self.top_source != source:
            return f"sampled for {self.top_source!r}, needed for {source!r}"
        age = now - self.top_fetched_at
        if age > self.TOP_BATCH_TTL:
            return f"batch age {age:.1f}s > {self.TOP_BATCH_TTL:.0f}s"
        return None


class TopConsumerPrefetcher:
    """Background-warmed cache of the top resource-consuming apps.

    ``resource_monitor.get_top_resource_consumers()`` is a multi-second
    CPU+IO+GPU sampling pipeline; running it inline at the moment pressure
    hits ``critical`` would delay throttling by that same duration. This
    class warms the answer asynchronously so the eventual critical-path
    lookup returns immediately.

    Pure cache, no autonomous behavior: every fetch comes from an explicit
    ``start(reason)`` call, and it never inspects ``passive_resource_control``
    itself -- the caller must skip ``start()`` when auto-limit is off so the
    multi-second sampling never runs without a consumer.

    Two independent time constants, easy to confuse:
      * ``CACHE_TTL`` is a *debounce*: it suppresses repeat ``start()`` calls
        fired within seconds of one another. It says nothing about validity.
      * ``MAX_CACHE_AGE`` is the *validity* bound used by
        ``resolve_for_critical()``. Past it the cached answer is still returned
        (slightly stale beats stalling the critical path on a resample) but a
        background refresh is kicked off. Without it the event-driven refresh
        alone could hand back a minutes-old top list.

    Cold-start fallback: ``resolve_for_critical()`` returns cached data; if
    empty, waits up to ``CRITICAL_WAIT`` for an in-flight prefetch; only then
    pays for a synchronous fetch. So the first critical event after boot never
    proceeds without top-consumer info.

    Thread-safety: ``_lock`` guards the cache dict; ``_inflight`` is a one-shot
    gate that prevents fetch storms when multiple triggers race.
    """

    CACHE_TTL = 5.0                       # rising-edge / listener debounce window (s)
    MAX_CACHE_AGE = 30.0                  # past this the cached top list is refreshed in background (s)
    CRITICAL_WAIT = 0.35                  # max wait for an in-flight prefetch on cold-start (s)
    SUSTAINED_CRITICAL_REFRESH_SEC = 45.0  # seconds of sustained critical before background recheck
                                           # (wall-clock, independent of the loop's tick cadence)

    def __init__(self, fetch_top_consumers):
        """
        :param fetch_top_consumers: callable returning
            ``(apps, reach_threshold)``; usually
            ``resource_monitor.get_top_resource_consumers``.
        """
        self._fetch = fetch_top_consumers
        self._cache = {"apps": [], "reach_threshold": False, "fetched_at": 0.0}
        self._lock = threading.Lock()
        self._inflight = threading.Event()
        # Cost accounting -- see _record_cost.  Guarded by _lock.
        self._cost = {"runs": 0, "wall": 0.0, "cpu": 0.0, "sync_runs": 0,
                      "sync_wall": 0.0, "first_at": 0.0}

    def _record_cost(self, reason: str, wall: float, cpu: float,
                     synchronous: bool = False) -> None:
        """Log what one fetch cost, and the duty cycle so far.

        Wall time and CPU time are reported separately on purpose.  A fetch is
        ``_get_top_processes(samples=3, interval=1.0)``, so at least three of
        its seconds are pure sleep, and sleep is not a reason to avoid running
        it more often.  The question "can this refresh continuously?" is
        answered by the CPU column and the duty cycle, not by ``took=``.

        Deliberately at INFO, not DEBUG: this line is what explains a long
        critical-to-limit delay, and it is useless if it is filtered out.
        """
        now = time.time()
        with self._lock:
            if not self._cost["first_at"]:
                self._cost["first_at"] = now - wall
            self._cost["runs"] += 1
            self._cost["wall"] += wall
            self._cost["cpu"] += cpu
            if synchronous:
                self._cost["sync_runs"] += 1
                self._cost["sync_wall"] += wall
            cost = dict(self._cost)

        elapsed = max(1e-6, now - cost["first_at"])
        logger.info(
            f"Top-consumer fetch cost ({reason}): wall={wall:.2f}s cpu={cpu:.3f}s "
            f"({100.0 * cpu / max(wall, 1e-6):.1f}% busy, rest is the sampling sleep) | "
            f"{cost['runs']} run(s) in {elapsed:.0f}s: wall={cost['wall']:.1f}s "
            f"cpu={cost['cpu']:.2f}s, cpu duty={100.0 * cost['cpu'] / elapsed:.2f}%, "
            f"wall duty={100.0 * cost['wall'] / elapsed:.1f}% | "
            f"blocking={cost['sync_runs']} run(s), {cost['sync_wall']:.1f}s"
        )

    def start(self, reason: str) -> None:
        """Kick off a background prefetch. No-op when one is already in
        flight or when the cache was refreshed within ``CACHE_TTL``
        seconds (back-to-back trigger debounce).

        Caller is responsible for ensuring auto-limit is enabled before
        invoking this — the cache has no other consumer.
        """
        if self._inflight.is_set():
            logger.debug(f"Top-consumer prefetch skipped ({reason}): fetch already in flight")
            return
        with self._lock:
            age = time.time() - self._cache["fetched_at"]
            if self._cache["apps"] and age < self.CACHE_TTL:
                logger.debug(f"Top-consumer prefetch skipped ({reason}): cache fresh, age={age:.2f}s")
                return
        self._inflight.set()
        t0 = time.time()
        logger.debug(f"Top-consumer prefetch started ({reason})")

        def _worker():
            # thread_time is this thread's own CPU, so the sampling sleeps and
            # the other threads' work are both excluded -- which is the point.
            cpu0 = time.thread_time()
            try:
                apps, threshold = self._fetch()
                with self._lock:
                    self._cache["apps"] = list(apps or [])
                    self._cache["reach_threshold"] = bool(threshold)
                    self._cache["fetched_at"] = time.time()
                logger.debug(
                    f"Top-consumer prefetch completed ({reason}): apps={len(apps)}, "
                    f"reach_threshold={threshold}, took={time.time() - t0:.2f}s"
                )
                self._record_cost(reason, time.time() - t0,
                                  time.thread_time() - cpu0)
            except Exception as e:
                logger.warning(f"Top-consumer prefetch failed ({reason}): {e}")
            finally:
                self._inflight.clear()

        threading.Thread(target=_worker, daemon=True).start()

    def is_stale(self) -> bool:
        """True when the cache is empty or older than ``MAX_CACHE_AGE``.

        Lets a caller that holds on to a previously-resolved list (e.g. the disk
        channel while it sits at "high" without throttling) know when to pull a
        fresh one instead of reusing an answer indefinitely.
        """
        with self._lock:
            if not self._cache["apps"]:
                return True
            return (time.time() - self._cache["fetched_at"]) > self.MAX_CACHE_AGE

    def resolve_for_critical(self):
        """Return ``(apps, reach_threshold)`` for the critical-path lookup.

        Order of operations:
          1. Return cached data immediately when present. Past ``MAX_CACHE_AGE``
             it is still returned — blocking the critical path on a multi-second
             resample is worse than acting on a slightly old top list — but a
             background refresh is started so the next call is fresh.
          2. Otherwise wait up to ``CRITICAL_WAIT`` for an in-flight
             prefetch and return its result.
          3. As a last resort, run a synchronous fetch — pays the full
             multi-second sampling cost; only reached on cold-start
             before any trigger has fired.
        """
        with self._lock:
            apps = list(self._cache["apps"])
            threshold = bool(self._cache["reach_threshold"])
            fetched_at = self._cache["fetched_at"]
            age = time.time() - fetched_at
        # Validity is "has a fetch ever completed", not "did it find anything".
        # An idle machine legitimately has no app above the threshold, and
        # testing the list meant that answer was indistinguishable from a cold
        # start -- so every critical event on such a machine paid the full
        # synchronous sampling cost, over and over, to be told [] again.
        if fetched_at:
            if age > self.MAX_CACHE_AGE:
                logger.info(
                    f"Critical resolve: cached top is stale (age={age:.1f}s > "
                    f"{self.MAX_CACHE_AGE}s); using it now, refreshing in background"
                )
                self.start("stale_refresh")
            else:
                logger.debug(f"Critical resolve: using cached top (age={age:.2f}s, apps={len(apps)})")
            return apps, threshold

        if self._inflight.is_set():
            logger.debug(f"Critical resolve: waiting up to {self.CRITICAL_WAIT}s for in-flight prefetch")
            waited = time.time()
            self._inflight.wait(self.CRITICAL_WAIT)
            waited = time.time() - waited
            with self._lock:
                apps = list(self._cache["apps"])
                threshold = bool(self._cache["reach_threshold"])
                fetched_at = self._cache["fetched_at"]
            # Same reasoning as above: the prefetch having finished is what
            # matters, not whether it found anyone. Re-running it synchronously
            # to get the same empty list back costs seconds and buys nothing.
            if fetched_at:
                logger.info(f"Critical resolve: got cache after waiting {waited:.2f}s "
                            f"(apps={len(apps)})")
                return apps, threshold
            logger.info(f"Critical resolve: waited {waited:.2f}s and the prefetch "
                        f"is still running; falling through to a blocking fetch")

        # The blocking branch: every second spent here is a second the limit is
        # not applied, and the fetch takes seconds -- so it is logged loudly.
        logger.info("Critical resolve: cache empty, falling back to synchronous fetch")
        t0, cpu0 = time.time(), time.thread_time()
        result = self._fetch()
        self._record_cost("critical_sync", time.time() - t0,
                          time.thread_time() - cpu0, synchronous=True)
        return result


class MaxPriorityQueue:
    def __init__(self):
        self._queue = queue.PriorityQueue()
        self._index = 0  # tie-breaker for equal-priority items

    def put(self, item):
        # Negate priority to implement max-heap ordering.
        priority = -item[1]
        heapq.heappush(self._queue.queue, (priority, self._index, item))
        self._index += 1

    def get(self):
        return heapq.heappop(self._queue.queue)[-1]

    def remove_if(self, condition_func):
        """
        Remove items that satisfy a condition (generic; no business logic).
        :param condition_func: callable receiving (data, priority) tuple, returns bool
        :return: list of removed items
        """
        removed_items = []
        new_queue = []

        for priority, idx, item in self._queue.queue:
            if condition_func(item):
                removed_items.append(item)
            else:
                new_queue.append((priority, idx, item))

        self._queue.queue = new_queue
        heapq.heapify(self._queue.queue)  # restore heap invariant
        return removed_items

    def empty(self):
        """Return True if the queue is empty."""
        return len(self._queue.queue) == 0

    def __str__(self):
        # Display highest-priority items first.
        items = sorted(((-priority, data) for priority, _, data in self._queue.queue), reverse=True)
        return str([(k, v) for (_, (k, v)) in items])

    def __len__(self):
        """Return the current number of items in the queue."""
        return len(self._queue.queue)


def _split_proportionally(total_budget, all_ids: list, per_cg_usage: dict) -> dict:
    """Distribute *total_budget* across *all_ids* proportionally to each entry in
    *per_cg_usage* ({basename: raw_value}).

    :param total_budget: Total budget to distribute (int MB or CPU%), or ``None``
        meaning "no limit for this resource".  When ``None``, every entry in the
        returned dict is also ``None`` so callers can safely forward the value to
        ``adjust_resources`` which treats ``None`` as "no limit".
    :param all_ids: Ordered list of cgroup basenames to distribute across.
    :param per_cg_usage: {basename: raw usage value} used for proportional weights.

    When *per_cg_usage* is missing or all values are zero, the budget is split
    equally so that single-cgroup apps (empty *all_ids* or no per-cgroup data)
    are never affected and multi-cgroup apps at worst receive equal shares rather
    than N times the intended cap.

    :returns: {basename: allocated_budget} where each value mirrors the type of
              *total_budget* (int >= 1 when a positive budget is given, or None).
              Values sum to approximately *total_budget*.
    """
    if total_budget is None or total_budget == 0:
        return {cg: total_budget for cg in all_ids}
    total_usage = sum(per_cg_usage.get(cg, 0) for cg in all_ids)
    if total_usage <= 0:
        n = len(all_ids) or 1
        each = max(1, total_budget // n)
        return {cg: each for cg in all_ids}
    return {
        cg: max(1, int(total_budget * per_cg_usage.get(cg, 0) / total_usage))
        for cg in all_ids
    }


class DynamicBalancer:
    # How many top disk-IO consumers to fetch so the throttle decision can skip
    # non-throttleable candidates (Critical / already-limited) and fall through to the
    # next heavy user.  Kept small: the candidate-sampling floor in _get_top_processes
    # (max(n*3, 9)) means values up to 3 cost the same as fetching just #1.
    _DISK_IO_TOP_N = 3
    # Fallbacks for the per-media tables in `limit_policy.disk_io` (see config.yaml for
    # what each number means and how it was chosen). Kept here so the throttle path keeps
    # working against a config that predates these keys.
    #
    # candidate_floor: the minimum I/O an app must be doing ON ONE DISK before capping it
    # is worthwhile -- throttling a light user cannot relieve the device. EITHER floor
    # qualifies, because bandwidth alone would miss small-block random workloads that move
    # few MB but saturate the device on IOPS.
    _CANDIDATE_FLOOR_DEFAULT = {
        'nvme': {'mb_s': 30.0, 'iops': 2000.0},
        'sata_ssd': {'mb_s': 20.0, 'iops': 1200.0},
        'mmc': {'mb_s': 10.0, 'iops': 400.0},
        'hdd': {'mb_s': 5.0, 'iops': 60.0},
        'usb': {'mb_s': 4.0, 'iops': 50.0},
        'unknown': {'mb_s': 5.0, 'iops': 60.0},
    }
    # media_scale: the `rate` table in limit_policy is calibrated for NVMe; these
    # re-express it for slower media so a priority means the same fraction of what the
    # device can do rather than the same absolute MB/s.
    _MEDIA_SCALE_DEFAULT = {
        'nvme': 1.0, 'sata_ssd': 0.6, 'mmc': 0.30,
        'hdd': 0.25, 'usb': 0.15, 'unknown': 0.25,
    }

    def __init__(self):
        self.bpf_monitor = AppIntercept("controller/bpf_event.c")
        self.config = b_config
        self.control_manager = self.bpf_monitor.control_manager
        self.resource_monitor = self.control_manager.res
        self.io_ctl = IOController()

        self.known_pids = set()

        self.is_running = False
        self.app_detect_queue = JoinableQueue(1000000)
        self.app_priority_queue = MaxPriorityQueue()
        # Runtime state for every app currently under a resource limit.
        # See LimitRegistry / LimitedApp for the full field list.
        self.all_limits = LimitRegistry()

        # Background-warmed top-consumer cache. Pure cache; callers must
        # gate ``start()`` on passive_resource_control being enabled —
        # the fetch is a multi-second CPU+IO+GPU sampling pipeline.
        self.top_prefetcher = TopConsumerPrefetcher(
            self.resource_monitor.get_top_resource_consumers
        )

        # Separate cache for the disk-IO top consumers.  Same rationale as
        # top_prefetcher, but keyed off the disk-IO level: warmed on the rising
        # edge into disk "high" so the eventual critical-path throttle finds the
        # answer ready instead of re-sampling the multi-second IO pipeline every
        # tick while disk pressure persists.
        self.disk_top_prefetcher = TopConsumerPrefetcher(
            self._fetch_top_disk_consumers
        )

        self.network_controller = NetworkController()

        # L0 rate limiter.  Zero means "never attempted", so the first eligible
        # tick after start-up can grow immediately.  The lock is needed because
        # the critical-state listener claims the same slot from the pressure
        # monitor's refresh thread -- see _claim_swap_growth_slot.
        self._last_swap_growth_at = 0.0
        self._swap_growth_lock = threading.Lock()
        self._last_swap_release_at = 0.0

        # Wall-clock until which the disk-IO arm ignores disk pressure because
        # our own reclaim produced it -- see _RECLAIM_DISK_QUIET_SEC.  A plain
        # float: written from the reclaim executor's callback and the REST
        # thread, read from the monitor loop, and a torn read is impossible for
        # a float store in CPython while a lock here would sit on the hot path.
        self._reclaim_quiet_until = 0.0

        # Anything a previous run could not hand back -- files still held at
        # shutdown, and files left on disk by a reboot (we never write fstab,
        # so after a reboot they are no longer swapped on and nobody owns the
        # space).  only_unused keeps this honest: at start-up we know nothing
        # about the machine's memory history, so a file holding pages is left
        # for the idle path to judge.
        self._reclaim_orphaned_swap()

    def _reclaim_orphaned_swap(self) -> None:
        """Release leftover managed swapfiles at start-up.  Never raises.

        Deliberately not gated on ``swap_growth.enabled``: switching growth off
        should give back what an earlier run allocated, not strand it.  If the
        feature was never on there are no managed files and this is a no-op.
        """
        try:
            self.control_manager.release_swap(dry_run=False, only_unused=True)
        except Exception as exc:
            logger.warning(f"[swap] start-up release failed: {exc}")

    def _fetch_top_disk_consumers(self):
        """Adapter so ``TopConsumerPrefetcher`` can warm the disk-IO top list.

        Fetches the top ``_DISK_IO_TOP_N`` consumers (not just #1) so the throttle
        decision can skip non-throttleable candidates (Critical / already-limited)
        and move on to the next heavy disk user.  ``get_top_disk_io_consumers``
        returns just the app list; the prefetcher expects ``(apps, reach_threshold)``,
        and any disk-IO stress counts as threshold-crossing, so it is always True.
        """
        return self.resource_monitor.get_top_disk_io_consumers(n=self._DISK_IO_TOP_N), True

    def _disk_io_table(self, key: str) -> dict:
        """Raw per-media table from ``limit_policy.disk_io``, or ``{}`` if unusable."""
        policy = getattr(self.config, 'limit_policy', None) or {}
        table = (policy.get('disk_io') or {}).get(key)
        return table if isinstance(table, dict) else {}

    def _media_scales(self) -> Dict[str, float]:
        """Per-media coefficient applied to the priority rate row, merged over the defaults.

        Merged per class rather than all-or-nothing: an operator who only wants to soften
        ``usb`` writes that one key and keeps the calibrated values for the rest, and a typo
        costs one media class instead of the whole table.
        """
        raw = self._disk_io_table('media_scale')
        scales = dict(self._MEDIA_SCALE_DEFAULT)
        for media in scales:
            value = raw.get(media)
            if isinstance(value, (int, float)) and 0 < float(value) <= 1.0:
                scales[media] = float(value)
        return scales

    def _candidate_floors(self) -> Dict[str, Dict[str, float]]:
        """Per-media ``{mb_s, iops}`` qualification floors, merged over the defaults."""
        raw = self._disk_io_table('candidate_floor')
        floors = {media: dict(entry) for media, entry in self._CANDIDATE_FLOOR_DEFAULT.items()}
        for media, entry in floors.items():
            configured = raw.get(media)
            if not isinstance(configured, dict):
                continue
            for field in ('mb_s', 'iops'):
                value = configured.get(field)
                if isinstance(value, (int, float)) and float(value) >= 0:
                    entry[field] = float(value)
        return floors

    def _qualifies_for_throttle(self, proc: dict) -> Tuple[bool, str]:
        """Is this app heavy enough on some ONE disk that capping it would help?

        Judged per disk against that disk's media floor, because the floors differ by an
        order of magnitude across media: 5 MB/s is nothing on an NVMe and most of what a
        thumb drive can deliver. ``io_per_disk`` comes from cgroup ``io.stat`` (see
        ``monitor/cgroup.py``), so the numerator and the disk attribution share a source.

        With no per-disk breakdown the app's totals are judged against the STRICTEST floor
        of any media class: a missing breakdown is not evidence of a slow device, so the
        conservative reading is the one that avoids throttling a light user.

        :return: ``(qualifies, reason)`` -- *reason* is for the decision log either way.
        """
        floors = self._candidate_floors()
        per_disk = proc.get('io_per_disk') or {}

        if not per_disk:
            strictest = max(floors.values(), key=lambda f: f['mb_s'])
            mb = (proc.get('io_read_rate') or 0.0) + (proc.get('io_write_rate') or 0.0)
            iops = (proc.get('io_read_iops') or 0.0) + (proc.get('io_write_iops') or 0.0)
            ok = mb >= strictest['mb_s'] or iops >= strictest['iops']
            return ok, (f"totals {mb:.1f}MB/s {iops:.0f}iops vs strictest floor "
                        f"{strictest['mb_s']}MB/s / {strictest['iops']}iops (no per-disk data)")

        for disk, rates in per_disk.items():
            media = media_for_disk(disk)
            floor = floors.get(media, floors['unknown'])
            mb = (rates.get('read_mb_s') or 0.0) + (rates.get('write_mb_s') or 0.0)
            iops = (rates.get('read_iops') or 0.0) + (rates.get('write_iops') or 0.0)
            if mb >= floor['mb_s'] or iops >= floor['iops']:
                return True, (f"{disk}({media}) {mb:.1f}MB/s {iops:.0f}iops "
                              f">= floor {floor['mb_s']}MB/s / {floor['iops']}iops")
        busiest = max(per_disk, key=lambda d: sum(per_disk[d].get(k) or 0.0
                                                  for k in ('read_mb_s', 'write_mb_s')))
        return False, (f"no disk over its media floor (busiest {busiest}"
                       f"/{media_for_disk(busiest)})")

    def _scaled_io_limits(self, io_limits: dict, disk_filter=None) -> Dict[str, Dict[str, int]]:
        """Expand one priority rate row into a per-disk ``io.max`` map, scaled by media.

        The same 20 MB/s cap is a mild nudge on an NVMe and more than a USB stick can
        deliver at all, so each disk gets the rate row multiplied by its media coefficient.
        Bandwidth and IOPS are scaled by the SAME factor on purpose: the rate table is built
        on ``iops = MB/s * 300``, and scaling them independently would move the block size
        at which the IOPS cap starts to bind.

        ``default`` remains unscaled so disks appearing between enumeration and
        write are capped at the loosest value.
        """
        base = {
            'rbps': io_limits['read'] * 1024 ** 2,
            'wbps': io_limits['write'] * 1024 ** 2,
            'riops': io_limits['read_iops'],
            'wiops': io_limits['write_iops'],
        }
        scales = self._media_scales()
        limits: Dict[str, Dict[str, int]] = {'default': {k: max(1, int(v)) for k, v in base.items()}}
        for disk in (self.io_ctl.get_disk_id(disk_filter) or {}):
            scale = scales.get(media_for_disk(disk), scales['unknown'])
            limits[disk] = {k: max(1, int(v * scale)) for k, v in base.items()}
        return limits

    def start(self):
        """
        Start the service, including the worker thread that processes the task queue.
        """
        self.network_controller.setup_tc_classes_and_filters()
        self.is_running = True

        # A previous run may have died while an app was frozen; nothing in the
        # (process-lifetime) registry knows about it, so thaw what we recorded.
        try:
            self.control_manager.thaw_leftover_frozen()
        except Exception:
            logger.error("Failed to thaw leftover frozen cgroups", exc_info=True)

        self.monitor_thread = threading.Thread(target=self._run_monitor_resource_loop, daemon=True)
        self.monitor_thread.start()

        self.handle_thread = threading.Thread(target=self._run_handle_loop, daemon=True)
        self.handle_thread.start()

        self.app_intercept_thread = threading.Thread(target=self._run_app_intercept_loop, daemon=True)
        self.app_intercept_thread.start()

        logger.info("Service started; worker threads are running")

    def _run_monitor_resource_loop(self):
        """Main pressure-driven decision loop.

        Each iteration:
          1. Samples peak pressure and disk-IO stress.
          2. Warms the top-consumer cache on rising edges / sustained
             critical, gated by ``passive_resource_control``.
          3. Runs the policy tick.
          4. Runs the network tick.

        Decision logic lives in ``_tick_separated_policy``; this method only
        orchestrates.
        """
        logger.info("Monitor resource service started")
        state = self._make_monitor_loop_state()
        # ``limit_policy.policy`` is kept as an extension point, but "separated"
        # is the only mode there is: the CPU/memory and disk-IO channels have
        # their own thresholds, their own candidate lists and their own recovery
        # timers. Anything else in the config is a leftover, not a choice.
        policy = self.config.limit_policy.get('policy', 'separated')
        if policy != 'separated':
            logger.warning(
                "limit_policy.policy=%r is no longer supported; running as 'separated'",
                policy)

        self.control_manager.register_critical_state_listener(self._on_critical_state_changed)
        self.control_manager.register_level_change_listener(self._on_pressure_level_changed)

        while self.is_running:
            try:
                state.current_time = time.time()

                _prc = self.config.passive_resource_control or {}
                passive_enabled = bool(_prc.get('enabled', True))

                # Falling edge (enabled -> disabled): hand every auto-limited app to the
                # operator as a manual limit WITHOUT releasing any cgroup. Releasing all
                # caps at once under high pressure would let the suppressed load stampede
                # back and crush the box -- the same crash window "lock to manual" exists
                # to close. So disabling passive control means zero release, full handoff.
                # None on first tick means "no transition", so a boot with passive already
                # off never triggers a spurious sweep.
                if (state.prev_passive_enabled is True and not passive_enabled):
                    try:
                        self.lock_all_auto_to_manual()
                    except Exception as exc:
                        logger.error("Failed to lock auto-limited apps to manual on passive disable: %s", exc)
                state.prev_passive_enabled = passive_enabled

                # With auto-limiting off, nothing we did is inflating PSI, so drop any
                # stale dominant state to fall back to raw (undiscounted) pressure.
                if not passive_enabled and self.all_limits.is_limited_app_dominant:
                    self.all_limits.is_limited_app_dominant = False
                    self.control_manager.set_limited_app_dominant(False)

                # Quiet mode (a benchmark run is being measured) suspends
                # automatic intervention. Never by touching the config switch:
                # routing it through passive_resource_control would fire the
                # falling edge above and convert every existing auto limit into a
                # manual one that never restores itself. Hence the local override,
                # applied *after* prev_passive_enabled has been recorded, as a
                # backstop for anything added outside the branch below.
                quiet = quiet_mode.is_active()
                if quiet:
                    passive_enabled = False

                # Feed L0/L2's worst-case window here, on every ~1s pass of the
                # loop -- not inside the idle_check gate below.  The checks that
                # consult the window run once a minute; a window built from
                # their own samples would describe nothing.  Reading
                # /proc/meminfo costs tens of microseconds.
                #
                # Unconditional, including when passive control is off: the same
                # samples drive the shrink timer, and a run that grew swap and
                # then had passive turned off still has to give the files back.
                self.control_manager.sample_memory()

                if quiet:
                    # The whole decision block is skipped, not just neutered.
                    # SystemPressureMonitor is stopped for the length of the run,
                    # so every level this block could read is frozen at the last
                    # tick before the run started -- and with passive control off
                    # the only arms it can still reach are release-only ones.
                    # Deciding a staged restore off stale pressure is worse than
                    # not deciding, and consuming the peak latch each tick would
                    # log a pressure reading nobody sampled. So: keep the one duty
                    # that must not stop, and nothing else.
                    #
                    # Draining is pure release (SIGCONT plus bookkeeping, see
                    # _drain_pending_app_queue). Without it an app the user
                    # launched and had held pending would stay suspended for the
                    # length of the benchmark.
                    state.prev_critical = False
                    if not self.app_priority_queue.empty():
                        self._drain_pending_app_queue(state)
                else:
                    # A RISING EDGE into critical bypasses the idle_check_interval gate. This loop
                    # wakes every ~1s (see time.sleep below), so a fresh critical level -- e.g.
                    # memory racing toward OOM -- gets a limit applied within ~1s instead of
                    # waiting up to idle_check_interval. Edge- (not level-) triggered on purpose:
                    # while critical persists, the tick reverts to the normal idle_check cadence,
                    # so we don't re-run the apply pipeline every second. Uses the non-consuming
                    # current level (does NOT reset the peak latch consumed below).
                    critical_now = self.control_manager.current_level == "critical"
                    critical_edge = critical_now and not state.prev_critical
                    state.prev_critical = critical_now
                    if (not self.app_priority_queue.empty()
                            or critical_edge
                            or (state.current_time - state.last_check_time) >= state.idle_check_interval):
                        # Use consume_peak_pressure_level() instead of get_current_pressure_level()
                        # so that transient "critical" spikes that occurred while the
                        # idle_check_interval gate was closed are never silently dropped.
                        pressure, _, disk_level = self.control_manager.consume_peak_pressure_level()

                        state.last_check_time = state.current_time
                        # Top-consumer prefetch / recheck only exist to warm the cache for the
                        # auto-limit path.  When passive control is off we are not going to
                        # apply any auto-limit, so skip the multi-second sampling pipeline.
                        self._maybe_trigger_prefetch(state, pressure, disk_level, passive_enabled)

                        # L0 before the policy tick: swap headroom is what makes
                        # L2 able to do anything at all, and its own watermarks
                        # -- not the pressure level -- decide whether it fires.
                        if passive_enabled:
                            self._tick_swap_headroom(state, pressure)
                        # The other direction, and deliberately not gated on
                        # passive control: a run that grew swap and then had
                        # passive turned off would otherwise leave the extra
                        # files behind for good.  It is inside the quiet-mode
                        # branch though -- swapoff faults a file's pages back
                        # in, which is exactly the kind of intervention a
                        # measured benchmark run must not see.
                        self._tick_swap_release(state)

                        self._tick_separated_policy(state, pressure, disk_level, passive_enabled)
                        state.prev_pressure = pressure
                        state.prev_disk_level = disk_level

                    # Both halves of the tick have to go under quiet mode: the
                    # sampling side reads every interface on its own cadence, and
                    # the handling side can install tc classes on the run's traffic.
                    self._run_network_tick(state)

                # Reaper: restore limits for apps that have since closed. Runs
                # on its own short cadence (limit_reap_interval), independent of
                # the idle_check_interval gate above, so a closed app's stale
                # limit is lifted within a couple of seconds.
                reap_interval = float(getattr(self.config, "limit_reap_interval", 2))
                if state.current_time - state.last_reap_time >= reap_interval:
                    state.last_reap_time = state.current_time
                    self._reap_closed_apps()

                time.sleep(1)
            except Exception as e:
                logger.error(f"Error in monitor loop: {str(e)}", exc_info=True)
                state.reset()
                time.sleep(1)

        logger.info("Monitor resource service stopped")

    def _make_monitor_loop_state(self) -> "_MonitorLoopState":
        """Build the per-loop state object, clamping idle_check_interval
        to the [min, max] window and emitting a warning when the config
        value gets clamped."""
        _MIN_IDLE_CHECK = 2.0   # seconds – below this polling is too aggressive
        _MAX_IDLE_CHECK = 30.0  # seconds – above this response latency becomes unacceptable
        _raw_idle = float(getattr(self.config, "monitor_idle_check_interval", 10))
        _pressure_update = float(getattr(self.config, "regular_update_sys_pressure_time", 5))
        # monitor_idle_check_interval must not be shorter than the pressure-data refresh period
        # to avoid making decisions on stale data, and must stay within [2, 30] seconds.
        default_idle_check_interval = max(
            _MIN_IDLE_CHECK,
            min(_MAX_IDLE_CHECK, max(_raw_idle, _pressure_update))
        )
        if default_idle_check_interval != _raw_idle:
            logger.warning(
                "monitor_idle_check_interval=%.1fs clamped to %.1fs "
                "(allowed range [%.0fs, %.0fs], min=regular_update_sys_pressure_time=%.1fs)",
                _raw_idle, default_idle_check_interval, _MIN_IDLE_CHECK, _MAX_IDLE_CHECK, _pressure_update,
            )
        return _MonitorLoopState(
            default_idle_check_interval=default_idle_check_interval,
            idle_check_interval=default_idle_check_interval,
        )

    def _maybe_prefetch_early(self, state: "_MonitorLoopState") -> None:
        """Warm the top-consumer cache before the pressure *level* moves.

        Two independent signals, either is enough, and both give the fetch (which
        takes seconds under full CPU load) a head start on the level change:

        * **Entering the pre-OOM band.** ``is_memory_pre_oom`` is the flag the
          pressure monitor already computes to decide its own refresh cadence
          (70% of RAM used with the defaults).
        * **Memory climbing fast.** ``mem_rise_rate()`` reads the slope off the
          same window the swap growth arms use.

        Not the pressure *score*: its memory term is a deliberately sharp gate,
        so it steps straight from near-zero to 1.00. Nothing useful can be
        predicted from a step function.

        The band arm is edge-triggered so sitting in the band costs nothing. The
        slope arm is not — but it can only fire while memory is genuinely
        climbing by a tenth of RAM per window, which is self-limiting, and
        ``start()`` debounces repeats within CACHE_TTL.
        """
        pre_oom = self.control_manager.is_memory_pre_oom
        entering_band = pre_oom and not state.prev_pre_oom
        state.prev_pre_oom = pre_oom

        if entering_band:
            logger.info("Memory entered the pre-OOM band: warming top-consumer cache")
            self.top_prefetcher.start("entering_pre_oom")
            return

        rise = self.control_manager.swap.mem_rise_rate()
        if rise is not None and rise >= MEM_RISE_TRIGGER:
            logger.info(
                f"Memory climbing fast (+{100.0 * rise:.0f} points across the window): "
                f"warming top-consumer cache"
            )
            self.top_prefetcher.start("memory_climbing")

    def _on_critical_state_changed(self, is_critical: bool) -> None:
        """Critical-state listener — backstops the rising-edge prefetch
        when pressure jumps directly into critical from below the ``high``
        band, and asks L0 for swap headroom on the same edge.

        Runs on the pressure monitor's refresh thread, which is the earliest
        anything in the process learns that pressure went critical: on the
        stress run it fired at 22:34:55.250, while the monitor loop's own
        critical-edge tick did not reach the swap check until 22:34:56.122.
        Both gate on ``passive_resource_control`` — neither the multi-second
        sampling nor a new swapfile is worth paying for when auto-limit is off.
        """
        if not is_critical:
            return
        prc = self.config.passive_resource_control or {}
        if not bool(prc.get('enabled', True)):
            logger.debug("Critical-state listener fired but passive control disabled: skipping prefetch")
            return
        if quiet_mode.is_active():
            # Matches the tick, which reaches _tick_swap_headroom only inside
            # the non-quiet branch.  A measured run must not have a swapfile
            # appear underneath it.
            logger.debug("Critical-state listener fired but quiet mode is active: skipping")
            return
        logger.debug("Critical-state listener fired: triggering top-consumer prefetch")
        self.top_prefetcher.start("critical_listener")
        # Out-of-band growth. Deliberately goes through the same rate-limit slot
        # as the tick, so whichever thread gets there first wins and the other
        # one is a no-op rather than a second fallocate.
        if (self.control_manager.swap.enabled
                and self._claim_swap_growth_slot(time.time(), urgent=True)):
            logger.info("[swap] critical edge: requesting headroom out of band")
            self._request_swap_headroom()

    def _on_pressure_level_changed(self, sys_level: str, disk_level: str) -> None:
        """Push the new pressure levels to the UI over SSE. Not stored in the DB.

        Runs on the monitor's refresh thread, one notification per transition, so the
        dashboard can show a live level without polling. Not driven off the monitor-loop
        tick, which reads the peak-latched level on the idle_check_interval cadence:
        right for limit decisions, but it lags and overstates the current state.
        """
        try:
            app_utils.callback_manager.send_callback_notification({
                'app_id': "",
                'app_name': "",
                'status': "pressure_level_changed",
                'sys_level': sys_level,
                'disk_level': disk_level,
                'purpose': "notify"
            }, False)
        except Exception as exc:
            logger.error("Failed to push pressure level change: %s", exc)

        # NOTE: durable pressure events are NOT emitted here. They are owned by the
        # snapshot-driven pressure-level monitor (diagnostics/episodes.py), which
        # records debounced level-change events and works in monitor-only too.
        # Emitting here as well would double-count.

    def _maybe_trigger_prefetch(self, state: "_MonitorLoopState", pressure: str,
                                disk_level: str, passive_enabled: bool) -> None:
        """Edge-trigger and sustained-critical recheck for the
        top-consumer prefetch. No-op when passive_resource_control is
        disabled (the multi-second sampling has no consumer in that case).

        Handles the pressure channel (CPU/mem) and, in separated policy, the
        independent disk-IO channel: both warm their own cache on the rising
        edge into "high" so the critical-path throttle never pays the sampling
        cost inline.
        """
        if not passive_enabled:
            state.critical_since = None
            state.disk_high_since = None
            return

        # Edge trigger: prefetch whenever pressure enters the high band from
        # any other state (low/medium below, critical above). This is the
        # core mechanism — by the time we reach critical the cache is warm.
        # Sustained high stays cached. The critical-state listener is a
        # backstop for non-high→critical direct jumps.
        if pressure == "high" and state.prev_pressure != "high":
            logger.debug(
                f"Pressure edge {state.prev_pressure}→high: triggering top-consumer prefetch"
            )
            self.top_prefetcher.start("entering_high")

        # The high edge above assumes pressure climbs through the bands, but it
        # can jump low → critical in a single refresh, in which case
        # "entering_high" never fires and the first critical pays for a
        # synchronous multi-second fetch. These two catch the climb before the
        # level does. Both are edge-triggered here and debounced by CACHE_TTL
        # inside start(), so a machine sitting above the line does not resample.
        self._maybe_prefetch_early(state)

        # Sustained-critical recheck: if critical persists for SUSTAINED_CRITICAL_REFRESH_SEC,
        # the original top1 has had ample time to settle under its limit. Refresh top in
        # background to catch a new dominant app that may have taken over. Timed on wall-clock
        # (not iteration count) so the cadence is independent of how often this tick runs.
        # Timer resets whenever pressure drops out of critical.
        if pressure == "critical":
            now = state.current_time
            if state.critical_since is None:
                state.critical_since = now
                state.last_sustained_recheck_time = now
            elif now - state.last_sustained_recheck_time >= TopConsumerPrefetcher.SUSTAINED_CRITICAL_REFRESH_SEC:
                logger.debug(
                    f"Sustained critical for {round(now - state.critical_since, 1)}s: "
                    f"triggering background top-consumer recheck"
                )
                self.top_prefetcher.start("sustained_critical_recheck")
                state.last_sustained_recheck_time = now
        else:
            state.critical_since = None

        # Warm the disk cache at "high" so the throttle at "critical" finds it ready
        # (see disk_top_prefetcher). A sustained window triggers a background recheck,
        # so a new dominant IO app is still picked up while the stress persists.
        disk_stressed = disk_level in ("high", "critical")
        # Memory wins when both channels are loaded. The disk sampler is cheap on
        # its own (mode="io" skips the 3x1s candidate pass), but its real cost is
        # wall-clock contention: run concurrently it stretches the memory fetch
        # several-fold under load, and that is the fetch with an OOM kill at the
        # end of it. The disk channel loses nothing durable -- it already reuses
        # its cache via is_stale(), and has no equivalent cliff.
        #
        # Note this gates only the fetches, not disk_stressed itself: the
        # episode timers below must keep tracking that the disk *is* stressed,
        # or clearing them here would make the channel think the episode ended
        # and drop a batch it is still holding.
        disk_prefetch_ok = disk_stressed and pressure != "critical"
        if disk_stressed and not disk_prefetch_ok:
            logger.debug(
                "Disk prefetch deferred: system pressure is critical and the "
                "memory top-consumer fetch has priority"
            )
        if disk_prefetch_ok and state.prev_disk_level not in ("high", "critical"):
            logger.debug(
                f"Disk level edge {state.prev_disk_level}→{disk_level}: "
                f"triggering disk top-consumer prefetch"
            )
            self.disk_top_prefetcher.start("entering_disk_high")

        if disk_stressed:
            now = state.current_time
            if state.disk_high_since is None:
                state.disk_high_since = now
                state.disk_last_recheck_time = now
            elif now - state.disk_last_recheck_time >= TopConsumerPrefetcher.SUSTAINED_CRITICAL_REFRESH_SEC:
                if not disk_prefetch_ok:
                    # Same priority rule as the edge above. The timer is left
                    # alone so the recheck happens on the next tick that is not
                    # competing with a memory crisis, rather than being skipped
                    # for another full SUSTAINED_CRITICAL_REFRESH_SEC.
                    logger.debug("Sustained disk recheck deferred: memory is critical")
                else:
                    logger.debug(
                        f"Sustained disk stress for {round(now - state.disk_high_since, 1)}s: "
                        f"triggering background disk top-consumer recheck"
                    )
                    self.disk_top_prefetcher.start("sustained_disk_recheck")
                    state.disk_last_recheck_time = now
        else:
            state.disk_high_since = None
            # The disk channel holds its batch unconsumed while it sits at "high"
            # (nothing is throttled below critical). Once the disk calms down that
            # batch describes an episode that is over, so drop it here rather than
            # leaving it for whichever channel next reads the slot.
            if state.top_source == "disk_io" and state.top_consume_apps:
                logger.debug(
                    "[disk-io] stress ended: dropping %d held candidate(s)",
                    len(state.top_consume_apps),
                )
                state.drop_top_batch()

    def _drain_pending_app_queue(self, state: "_MonitorLoopState") -> None:
        """Pop one pending app off ``app_priority_queue`` and resume it:
        emit SIGCONT, flip DB status to "running", broadcast the SSE
        callback, then reset loop state.
        """
        app_data, priority = self.app_priority_queue.get()
        logger.info(
            f"Starting app: {app_data['app_name']} (PID: {app_data['pid']}, Priority: {priority})")
        os.kill(app_data['pid'], signal.SIGCONT)
        app_utils.update_app_status(app_data['app_id'], "running")
        app_utils.callback_manager.send_callback_notification({
            'app_id': app_data['app_id'],
            'app_name': app_data['app_name'],
            'status': "running",
            'purpose': "app"
        }, True)
        state.reset()

    def _drop_excluded_candidates(self, apps: list) -> list:
        """Remove candidates the user opted out of auto-limiting.

        Filters the whole batch rather than just the head, so the next candidate becomes
        #1 and gets limited on the same tick. Dropping one head per tick would cost a
        tick of reaction latency per excluded app.
        """
        if not apps:
            return apps
        with self.all_limits.lock:  # is_excluded walks a dict the REST thread mutates
            kept = []
            for candidate in apps:
                app_meta = candidate.get('app') or {}
                # Match against the candidate's own primary cgroup too, not just its
                # extra cgroups. An exclusion (user_restore *or* manual_limit) is keyed
                # by the resolved cgroup the limit sits on, which is this sample's
                # cgroup_id; omitting it lets an excluded app -- e.g. one just adopted
                # into manual control -- slip back into the auto pool and get re-limited
                # (which then clobbers the manual entry). Mirrors the disk-io arm.
                candidate_cgroups = list(candidate.get('extra_cgroups') or [])
                sampled_cgroup = (
                    os.path.basename((candidate.get('cgroup') or '').rstrip('/'))
                    or candidate.get('cgroup_id')
                    or ''
                ).strip()
                if sampled_cgroup and sampled_cgroup not in candidate_cgroups:
                    candidate_cgroups.append(sampled_cgroup)
                excluded = self.all_limits.is_excluded(
                    app_meta.get('id') or '',
                    candidate.get('process', {}).get('name') or '',
                    candidate_cgroups,
                )
                if excluded is None:
                    kept.append(candidate)
                else:
                    logger.info(
                        "Skipping top consumer %r (%s): excluded from auto-limit by user "
                        "restore (%s)", candidate.get('process', {}).get('name'),
                        app_meta.get('id'), excluded.get('key'),
                    )
        return kept

    def _refilter_held_batch(self, state: "_MonitorLoopState") -> None:
        """Re-apply the exclusion filter to a batch sampled on an earlier tick.

        Both channels keep their batch across ticks, so an exclusion added while pressure
        is still up would go unnoticed until the batch aged out and the app the user just
        freed could be re-limited from the stale list.
        """
        if not state.top_consume_apps or not self.all_limits.auto_limit_exclusions:
            return
        kept = self._drop_excluded_candidates(state.top_consume_apps)
        if len(kept) != len(state.top_consume_apps):
            state.top_consume_apps = kept

    def _update_dominant_flag_from_top(self, state: "_MonitorLoopState") -> None:
        """Walk the prefetched top-consumer list and decide whether any
        already-limited (and not yet partially-restored) app is the
        current dominant resource consumer. Sets
        ``self.all_limits.is_limited_app_dominant`` and pushes the flag down
        into the control manager so PSI baselines compensate correctly.
        """
        # Collect the cgroups of ALL already-auto-limited apps among the top consumers
        # (not just the first). Discounting every already-limited app's self-inflicted PSI
        # -- rather than a single one -- means that once all top hogs are limited, their
        # combined pressure is attributed correctly and the score stops over-reporting.
        dominant_cgroups = []
        is_dominant = False
        for app_info in state.top_consume_apps:
            # Match by cgroup membership, not just the top-consumer id: a
            # controlled multi-cgroup app is keyed in the registry by its
            # resolved primary cgroup basename, which need not equal this
            # sample's ``app.id`` or its surfaced cgroup.
            current_app_id = (app_info.get('app') or {}).get('id')
            top_cgroups = set()
            if app_info.get('cgroup'):
                top_cgroups.add(os.path.basename(app_info['cgroup']))
            if app_info.get('cgroup_id'):
                top_cgroups.add(app_info['cgroup_id'])
            for extra in app_info.get('extra_cgroups', []) or []:
                top_cgroups.add(os.path.basename(extra))

            entry = self.all_limits.apps.get(current_app_id)
            if entry is None:
                for cand_key, cand in self.all_limits.apps.items():
                    if cand.source == "auto" and (
                        cand_key in top_cgroups or top_cgroups & set(cand.cgroups)
                    ):
                        entry = cand
                        break

            if entry is not None and entry.source == "auto" and entry.state != "partially_restored":
                is_dominant = True
                # Full cgroup path of this limited consumer (resolved from its PID),
                # used to discount its self-inflicted PSI when scoring system pressure.
                pid = (app_info.get('process') or {}).get('pid') or \
                    next(iter(app_info.get('pids') or []), None)
                cg = app_utils.get_cgroup_path_by_pid(pid) if pid else None
                if cg and cg not in dominant_cgroups:
                    dominant_cgroups.append(cg)

        self.all_limits.is_limited_app_dominant = is_dominant
        logger.info(f"Balance- was the process limited before? {self.all_limits.is_limited_app_dominant}")
        self.control_manager.set_limited_app_dominant(
            self.all_limits.is_limited_app_dominant, dominant_cgroups)

    def _target_has_auto_cpu_mem_limit(self, target_app: dict, app_id: str) -> bool:
        """Whether this system-pressure target already carries a CPU/memory cap.

        The dominant flag intentionally covers either kind of auto limit because both
        can contribute self-induced PSI. It must not, however, prevent a system
        pressure response merely because the target already has an I/O-only cap.
        """
        candidate_ids = {
            str(ident) for ident in (
                app_id,
                (target_app.get('app') or {}).get('id'),
                target_app.get('cgroup_id'),
            ) if ident
        }
        for cgroup in [target_app.get('cgroup'), *(target_app.get('extra_cgroups') or [])]:
            if cgroup:
                candidate_ids.add(os.path.basename(str(cgroup).rstrip('/')))

        with self.all_limits.lock:
            for candidate_id in candidate_ids:
                found = self.all_limits.by_any_id(candidate_id, source="auto")
                if found is not None and found[1].limit_parts.get('cpu_mem_limited', False):
                    return True
        return False

    def _tick_separated_policy(
        self,
        state: "_MonitorLoopState",
        pressure: str,
        disk_level: str,
        passive_enabled: bool,
    ) -> None:
        """One iteration of the separated-policy state machine.

                Three mutually-exclusive cases:
                    * critical system pressure — apply or refresh CPU/memory limits
                    * disk-IO stress without critical system pressure — apply or refresh I/O limits
          * pending app launches with no critical pressure — drain queue
          * medium/low pressure with limited apps — staged restore

        Disk IO is handled in two stages off ``disk_level``: ``>= high`` engages the arm
        and identifies the top consumer, but the actual throttle is applied only at
        ``critical`` — a saturated disk that has not yet blocked the system is observed,
        not throttled.
        """
        is_disk_io_stressed = disk_level in ("high", "critical")
        if passive_enabled and (pressure == "critical" or is_disk_io_stressed):
            state.restore_pending = False

            # The two channels keep their candidates in the same slot, so a batch left
            # behind by one of them must never be spent by the other (nor by a later
            # episode of its own channel): both the app ranking and reach_threshold
            # would then describe pressure that is no longer the pressure being handled.
            # A merely-high disk is an observation/pre-fetch state, not a throttle. It
            # must not prevent an active system-critical condition from applying its
            # CPU/memory protection in this tick.
            channel = "sys" if pressure == "critical" else "disk_io"
            handling_disk_io = channel == "disk_io"
            stale_reason = state.stale_top_batch_reason(channel, state.current_time)
            if stale_reason:
                logger.info(
                    "Discarding %d held top-consumer candidate(s): %s",
                    len(state.top_consume_apps), stale_reason,
                )
                state.drop_top_batch()

            if not handling_disk_io:
                state.pressure_start_time = None
                if not state.top_consume_apps:
                    apps, reach_threshold = self.top_prefetcher.resolve_for_critical()
                    state.keep_top_batch(self._drop_excluded_candidates(apps),
                                         reach_threshold, channel, state.current_time)
            else:
                state.disk_io_not_stressed_start_time = None
                # Serve from the prefetch cache instead of re-sampling every tick. The
                # staleness check matters at "high", where the batch is never consumed:
                # without it we keep acting on the list captured when the disk got busy.
                if not state.top_consume_apps or self.disk_top_prefetcher.is_stale():
                    apps, _ = self.disk_top_prefetcher.resolve_for_critical()
                    # IO pressure always counts as threshold-crossing.
                    state.keep_top_batch(self._drop_excluded_candidates(apps),
                                         True, channel, state.current_time)
            self._refilter_held_batch(state)
            if state.top_consume_apps:
                self._update_dominant_flag_from_top(state)

                if not handling_disk_io:
                    should_adjust, is_controlled, app_id, limit_rates = self._handle_critical_pressure(
                        state.top_consume_apps, state.reach_threshold)
                    # CPU/mem critical acts on the #1 top consumer. An existing I/O-only
                    # cap must not suppress CPU/memory protection for this same cgroup.
                    target_app = state.top_consume_apps[0]
                    can_apply = (not self._target_has_auto_cpu_mem_limit(target_app, app_id)
                                 and state.reach_threshold and should_adjust and app_id)
                    consumed_idx = 0
                else:
                    should_adjust, is_controlled, app_id, limit_rates, target_app, consumed_idx = \
                        self._handle_disk_io_stressed(state.top_consume_apps)
                    # Disk: throttle only at "critical" (at "high" we just prefetch/identify).
                    # _handle_disk_io_stressed already excludes Critical and already-limited
                    # targets, so the is_limited_app_dominant gate is intentionally NOT applied
                    # here -- successive critical ticks move on to the next heavy consumer.
                    can_apply = (disk_level == "critical"
                                 and state.reach_threshold and should_adjust and app_id)

                if can_apply and target_app and self._target_still_present(target_app, app_id):
                    self._apply_resource_limits(
                        target_app,
                        app_id,
                        limit_rates,
                        is_controlled,
                        is_disk_io_stressed=handling_disk_io,
                        pressure_level=(disk_level if handling_disk_io else pressure),
                    )

                if not handling_disk_io:
                    # L2 runs after L1 in the same tick, deliberately.  L1 has
                    # just pinned the target at its memory.high and pushed it
                    # toward idle, which is exactly the state where reclaim is
                    # cheap and the pages stay out.
                    self._tick_memory_reclaim(state)

                if handling_disk_io and disk_level != "critical":
                    # "high" is the armed state: identify the target but keep the prefetched
                    # list intact, or the critical tick it was warmed for finds it empty.
                    # Held only for as long as the episode and the batch TTL last (see
                    # stale_top_batch_reason) -- never across episodes or channels.
                    logger.debug(
                        "[disk-io] level=high: holding %d prefetched candidate(s), no throttle",
                        len(state.top_consume_apps),
                    )
                elif consumed_idx is not None:
                    # Drop the candidate we actually acted on, not blindly the head: the head
                    # is often a Critical app we deliberately skipped, and popping it would
                    # discard a valid future target while keeping the one just limited.
                    state.top_consume_apps.pop(consumed_idx)
                else:
                    # Critical, but the whole batch was unthrottleable. Popping one entry per
                    # tick would re-walk the same dead list for several ticks; drop it so the
                    # next tick resolves a fresh one and can react to a new heavy consumer.
                    logger.info(
                        "[disk-io] critical but no throttleable candidate in this batch; "
                        "discarding it to force a fresh top-consumer list next tick"
                    )
                    state.drop_top_batch()
            else:
                state.reset()

        elif not self.app_priority_queue.empty() and pressure != "critical" and not is_disk_io_stressed:
            self._drain_pending_app_queue(state)
        else:
            self._tick_separated_restore(state, pressure, is_disk_io_stressed)

    def _tick_separated_restore(
        self,
        state: "_MonitorLoopState",
        pressure: str,
        is_disk_io_stressed: bool,
    ) -> None:
        """Staged restore arm of the separated-policy tick.

        Tracks two independent stability timers (pressure and disk-IO) and
        runs partial / full restore once the relevant timer crosses
        ``STABLE_PERIOD`` / ``STABLE_DISK_IO_PERIOD``.

        Separated policy means separated recovery: a timer only ever lifts the
        caps of *its own* channel, and only on an app that actually carries
        them.  Restoring both channels off whichever timer happened to fire
        would hand back disk bandwidth because CPU/memory has been calm for
        30 minutes — while the disk may still be under stress — and would let
        one app's sys limit be relaxed on the strength of another app's
        disk-IO stability.
        """
        if not (self.all_limits.first_auto() is not None and not state.restore_pending):
            return

        should_check_pressure = (pressure in ("medium", "low") and
                                 any(app.limit_parts.get('cpu_mem_limited', False) for app in
                                     self.all_limits.apps.values() if app.source == "auto"))
        should_check_io = (not is_disk_io_stressed and
                           any(app.limit_parts.get('io_limited', False) for app in
                               self.all_limits.apps.values() if app.source == "auto"))
        if not (should_check_pressure or should_check_io):
            state.reset()
            return

        logger.debug(f"pressure_start_time: {state.pressure_start_time}, "
                     f"current_pressure: {state.current_pressure}, pressure: {pressure}")
        if should_check_pressure:
            if (state.pressure_start_time is None) or (state.current_pressure != pressure):
                state.pressure_start_time = state.current_time
                state.current_pressure = pressure
                logger.info(
                    f"Pressure level changed to {pressure}. "
                    f"Will restore resources after {state.STABLE_PERIOD} sec if it remains stable.")

        if should_check_io:
            if state.disk_io_not_stressed_start_time is None:
                state.disk_io_not_stressed_start_time = state.current_time
                logger.info(
                    f"Disk IO stress resolved. Will consider for restoration after {state.STABLE_DISK_IO_PERIOD} sec if it remains stable.")

        pressure_stable = (should_check_pressure and
                           (state.current_time - state.pressure_start_time >= state.STABLE_PERIOD))
        io_stable = (should_check_io and
                     (state.current_time - state.disk_io_not_stressed_start_time >= state.STABLE_DISK_IO_PERIOD))
        io_double_stable = (should_check_io and
                     (state.current_time - state.disk_io_not_stressed_start_time >= state.STABLE_DISK_IO_PERIOD * 2))

        logger.debug(f"pressure_stable: {pressure_stable}, io_stable: {io_stable}, io_double_stable: {io_double_stable}")

        if pressure_stable and pressure == "medium":
            state.restore_pending = True
            self._restore_channel("sys", "partial",
                                  f"Pressure remained at 'medium' for {state.STABLE_PERIOD} sec")
        elif io_stable and not io_double_stable:
            state.restore_pending = True
            self._restore_channel("disk_io", "partial", "Disk IO stress resolved")
        elif (pressure_stable and pressure == "low") or io_double_stable:
            state.restore_pending = True
            # Both timers can be ripe on the same tick; each still only lifts its own
            # caps, on its own target.
            if pressure_stable and pressure == "low":
                self._restore_channel("sys", "full",
                                      f"Pressure remained at 'low' for {state.STABLE_PERIOD} sec")
            if io_double_stable:
                if self._restore_channel(
                        "disk_io", "full",
                        f"Disk IO stayed calm for {state.STABLE_DISK_IO_PERIOD * 2} sec"):
                    state.disk_io_not_stressed_start_time = None
                    logger.debug("Reset IO stress timer after full restoration")
        state.restore_pending = False

    # Which limit_parts flag each recovery channel owns.
    _CHANNEL_PART = {'sys': 'cpu_mem_limited', 'disk_io': 'io_limited'}

    def _notify_auto_limit_changed(self, public_id: str, app_name: str, detail: str) -> None:
        """Tell the UI the auto-limited list changed without an app-status transition.

        A staged restore relaxes one of an app's caps while it stays "limited", so no
        purpose="app" callback fires and the card would show a stale row until something
        else happened. The dashboard listens to this instead of polling.
        """
        try:
            app_utils.callback_manager.send_callback_notification({
                'app_id': public_id,
                'app_name': app_name,
                'status': "auto_limit_changed",
                'detail': detail,
                'purpose': "notify"
            }, False)
        except Exception as exc:
            logger.error("Failed to push auto-limit change notification: %s", exc)

    def _restore_channel(self, channel: str, restore_type: str, why: str) -> bool:
        """Run one staged restore step for *channel* ("sys" | "disk_io").

        Picks the oldest auto-limited app that still carries this channel's cap
        (skipping, for a partial step, the ones already relaxed on it) and
        restores that channel alone — the other channel's caps stay untouched
        until its own timer says otherwise.

        :returns: True when a full restore actually completed, so the caller can
            reset the channel's stability timer.
        """
        part = self._CHANNEL_PART[channel]
        found = self.all_limits.first_auto_with_part(
            part, skip_partially_restored=(restore_type == "partial"))
        if not found:
            return False

        app_id, entry = found
        return self._restore_channel_for(app_id, entry, channel, restore_type, why)

    def _restore_channel_for(self, app_id: str, entry: "LimitedApp", channel: str,
                             restore_type: str, why: str) -> bool:
        """Restore one channel of one *named* entry.

        The body of ``_restore_channel`` after it has picked a target.  Split out
        so a caller that already knows which app it wants — the sys-critical path
        clearing an io cap off the app it is about to reclaim from — goes through
        the same accounting as the timers do: the ``limit_parts`` flag, the
        ``limit_disks`` list, the RECOVERED/FAILED events and the UI notification
        all happen here and nowhere else.  A cap lifted outside this method would
        leave the registry claiming a limit the kernel no longer has.

        :returns: True when a full restore actually completed.
        """
        part = self._CHANNEL_PART[channel]
        app_name = entry.app_name
        # Only this channel's flag is set, so restore_resources leaves the other
        # channel's cap in place even when the app is limited on both.
        parts = {'cpu_mem_limited': part == 'cpu_mem_limited',
                 'io_limited': part == 'io_limited'}
        logger.info(f"{why}. {restore_type.capitalize()} restore of {channel} "
                    f"limits for app {app_id}.")

        # Snapshot before restore_resources clears the entry's part flags and a
        # full restore may pop it.
        recover_attrs = {"app_name": app_name, "priority": entry.priority,
                 "limit_parts": dict(entry.limit_parts),
                         "resource_parts": entry.resource_parts,
                         "cgroups": entry.cgroups, "pids": entry.pids}
        recover_app_id = entry.public_app_id or app_id
        protection_id = entry.protection_id

        if not self.restore_resources(app_id, app_name, entry.limit_rates, parts, restore_type):
            logger.warning(f"{restore_type.capitalize()} {channel} restore failed for {app_name}")
            # A failed full restore leaves the cap in place, so the lifecycle is
            # not recovered.
            if restore_type == "full":
                _emit_control_events(
                    "FAILED", parts, app_id=recover_app_id, app_name=app_name,
                    protection_id=protection_id, attributes=recover_attrs)
            self.all_limits.apps.move_to_end(app_id)
            return False

        if restore_type == "partial":
            entry.partial_parts[channel] = True
            # Coarse flag the PSI-dominant check reads: this app is no longer
            # under its full original cap.
            entry.state = "partially_restored"
            self.all_limits.apps.move_to_end(app_id)
            self._notify_auto_limit_changed(
                entry.public_app_id or app_id, app_name, f"{channel}_partially_restored")
            return False

        # Full restore: restore_resources has cleared this channel's flag on the
        # entry; the app only leaves the registry once neither channel is capped.
        entry.partial_parts[channel] = False
        # RECOVERED for the channel just lifted, even when the other channel of the
        # same lifecycle is still capped.
        _emit_control_events(
            "RECOVERED", parts, app_id=recover_app_id, app_name=app_name,
            protection_id=protection_id, attributes=recover_attrs)
        still_limited = (entry.limit_parts.get('cpu_mem_limited')
                         or entry.limit_parts.get('io_limited'))
        if still_limited:
            self.all_limits.apps.move_to_end(app_id)
            logger.info(f"Restored {channel} limits for app {app_id}; "
                        f"remaining limits {entry.limit_parts}, moved to end of queue")
            self._notify_auto_limit_changed(
                entry.public_app_id or app_id, app_name, f"{channel}_restored")
            return True

        # The DB row and the UI know the app by its public id, which for a
        # resolved multi-cgroup app is not the cgroup key used here.
        public_id = entry.public_app_id or app_id
        app_utils.update_app_status(public_id, "running")
        app_utils.callback_manager.send_callback_notification({
            'app_id': public_id,
            'app_name': app_name,
            'status': "running",
            'purpose': "app"
        }, False)
        self.all_limits.apps.pop(app_id, None)
        logger.info(f"Fully restored app {app_id}, removed from limited apps")
        return True

    def _clear_io_cap_before_sys_limit(self, app_id: str, app_name: str) -> None:
        """Lift an auto disk-IO cap off an app that is about to be capped on CPU/memory.

        The two caps are individually sound and jointly self-defeating: an app
        under ``memory.high`` is a reclaim target, and reclaim writes its anon
        pages to swap over the same block device ``io.max`` is throttling.  The
        app then either reclaims at the throttled rate or stalls outright, and
        the system deficit the CPU/memory cap was meant to relieve stays.

        Only the disk channel yields, and only for auto limits — a manual io cap
        is the user's decision and is never touched here.  Yielding is not
        forgetting: the disk channel is judged on its own thresholds every tick,
        so if the disk is still saturated once memory is off the critical list it
        gets capped again.
        """
        found = self.all_limits.by_any_id(app_id, source="auto")
        if not found:
            return
        key, entry = found
        if not entry.limit_parts.get('io_limited'):
            return

        logger.info(
            "[io-yield] %s (%s) is about to be capped on CPU/memory while it carries "
            "an auto io.max; restoring its disk-IO channel first so reclaim can write "
            "to swap", app_name, key)
        self._restore_channel_for(
            key, entry, "disk_io", "full",
            "System pressure is critical; the disk cap yields to memory reclaim")

    # ------------------------------------------------------------------
    # L2: proactive reclaim, run after the L1 (memory.high) arm
    # ------------------------------------------------------------------

    def _reclaim_candidates(self) -> "list[tuple[str, LimitedApp, int]]":
        """Limited apps L2 may pull from, with their time-order weight.

        ``LimitRegistry.apps`` is an OrderedDict in the order limits were
        applied, so the index is the app's seniority.  Weighting by ``index + 1``
        makes the most recently limited app give up the most: when several apps
        start at once, the newcomer yields before the one that was already
        running and settled.

        ``source`` is deliberately not filtered.  A manually-limited app has a
        ``memory.high`` the user chose and is, by the fact that the user chose to
        limit it, usually one of the big holders; excluding it left L2 unable to
        reach the very apps most worth reclaiming from.  Reclaim does not change
        anyone's cap -- it asks the kernel to honour the cap already in place --
        so this takes nothing away from the user's decision.
        """
        candidates = []
        with self.all_limits.lock:
            entries = list(self.all_limits.apps.items())
        for index, (key, entry) in enumerate(entries):
            if not entry.limit_parts.get('cpu_mem_limited'):
                continue
            if entry.priority == "critical":
                continue
            if entry.reclaim_inflight:
                continue
            candidates.append((key, entry, index + 1))
        return candidates

    def _reclaim_cooldown_ok(self, entry: "LimitedApp", now: float) -> bool:
        """True once the cooldown, plus any EAGAIN backoff, has elapsed."""
        cfg = self.control_manager.reclaim_config()
        wait = float(cfg.get("cooldown_sec", 30)) + entry.reclaim_backoff
        return (now - entry.last_reclaim_at) >= wait

    def _tick_memory_reclaim(self, state: "_MonitorLoopState") -> None:
        """One L2 round: measure the system deficit, split it, reclaim.

        Three things make this safe to run behind the L1 arm:

        * the goal is the *system* target, never a per-app one.  Deficit filled,
          round over -- nothing is pushed below its own memory.high, which is
          sometimes far under the app's working set;
        * free memory is only the gate.  PSI sets the gain, because a box that is
          85% full and not stalling does not need to be pulled at 5% a round;
        * every candidate is checked for idleness first.  An active target
          faults the pages straight back in within seconds, so the per-chunk
          block the caller pays for buys nothing at all.
        """
        if not self.control_manager.reclaim_enabled:
            return

        deficit, facts = self.control_manager.system_memory_deficit()
        if deficit <= 0:
            logger.info("[reclaim] system already under target (%.1f%% used)",
                         facts.get("used_fraction", 0) * 100)
            return

        candidates = self._reclaim_candidates()
        if not candidates:
            logger.info("[reclaim] deficit %d MB but no eligible auto-limited app",
                         deficit // _MB)
            return

        # Swap is the only place anonymous pages can go.  When the shortfall is
        # in slab or page tables instead, reclaim cannot reach it and the round
        # would just cost blocking time for nothing.
        if facts.get("movable_anon_bytes", 0) <= 0:
            logger.info("[reclaim] shortfall is not in anonymous memory; standing down")
            return

        now = state.current_time
        psi_memory = self.control_manager.memory_pressure()
        gain = self.control_manager.reclaim_gain(psi_memory)

        ready, weights = [], {}
        for key, entry, order in candidates:
            if not self._reclaim_cooldown_ok(entry, now):
                continue
            # Reclaim writes anon pages to swap over the block device this app's
            # io.max is throttling, so reclaiming here buys stalls, not memory.
            # L2 does not lift the cap itself -- the disk channel's accounting is
            # not L2's to touch.  ``_clear_io_cap_before_sys_limit`` clears it on
            # the next sys-critical cap, and the disk timer clears it when the
            # disk calms down; either way this app comes back as a candidate.
            if entry.limit_parts.get('io_limited'):
                logger.info("[reclaim] skip %s: still under an io.max cap", key)
                continue
            cgroup = entry.cgroups[0] if entry.cgroups else key
            elapsed = now - entry.last_cpu_sample_at if entry.last_cpu_sample_at else 0.0
            busy, cpu_usec = self.control_manager.cpu_busy_fraction(
                cgroup, entry.last_cpu_usec, elapsed)
            entry.last_cpu_usec = cpu_usec
            entry.last_cpu_sample_at = now
            allowed, reason = self.control_manager.should_reclaim(cgroup, busy)
            if not allowed:
                logger.info("[reclaim] skip %s: %s", key, reason)
                continue
            current = self.control_manager.memory.read_current(cgroup) or 0
            if current <= 0:
                continue
            ready.append((key, entry, cgroup))
            weights[key] = current * order

        if not ready:
            return

        # Cap the round at what the gain allows, then at the deficit: the gain
        # keeps a single round from overshooting, the deficit keeps a series of
        # rounds from reclaiming past the point of doing any good.
        round_budget = min(deficit, int(sum(weights.values()) * gain))
        if round_budget <= 0:
            return
        shares = _split_proportionally(round_budget, [k for k, _, _ in ready], weights)

        logger.info(
            "[reclaim] deficit %d MB, psi_mem %.3f -> gain %.1f%%, budget %d MB "
            "across %d app(s)",
            deficit // _MB, psi_memory, gain * 100, round_budget // _MB, len(ready))

        for key, entry, cgroup in ready:
            share = shares.get(key) or 0
            if share <= 0:
                continue
            entry.reclaim_inflight = True
            entry.last_reclaim_at = now
            self._mark_reclaim_noise()
            future = self.control_manager.reclaim_memory_async(cgroup, share)
            future.add_done_callback(
                lambda fut, e=entry, k=key, c=cgroup, n=share:
                    self._on_reclaim_done(fut, e, k, c, n))

    def _on_reclaim_done(self, future, entry: "LimitedApp", key: str,
                         cgroup: str, requested: int) -> None:
        """Executor callback: record the outcome and react to EAGAIN.

        Runs on the worker thread, so it touches only this entry's own fields
        and hands everything else to the (thread-safe) diagnostics seam.
        """
        entry.reclaim_inflight = False
        # Re-arm from the *end* of the reclaim: a long one would otherwise have
        # its window expire while it was still writing to swap.
        self._mark_reclaim_noise()
        try:
            outcome = future.result()
        except Exception as exc:                     # pragma: no cover - defensive
            logger.error("[reclaim] %s raised: %s", key, exc)
            return

        cfg = self.control_manager.reclaim_config()
        cooldown = float(cfg.get("cooldown_sec", 30))
        attributes = {
            "cgroup": cgroup,
            "app_name": entry.app_name,
            "requested_mb": requested // _MB,
            "reclaimed_mb": outcome.reclaimed_bytes // _MB,
            "elapsed_ms": round(outcome.elapsed_ms, 1),
            "swappiness": outcome.swappiness,
        }

        if outcome.is_eagain:
            # Swap is full: retrying immediately just burns another 4 ms per
            # call.  Back off, and ask L0 for more fuel -- that, not the retry,
            # is what can actually make the next round work.
            entry.reclaim_backoff = min(
                max(cooldown, entry.reclaim_backoff * 2), _RECLAIM_MAX_BACKOFF)
            logger.warning(
                "[reclaim] %s: no swap headroom (EAGAIN), backing off %.0fs "
                "and requesting swap growth", key, entry.reclaim_backoff)
            _emit_reclaim_event(
                "RESOURCE_MEMORY_RECLAIM_EXHAUSTED", "warning",
                f"Reclaim for {entry.app_name} found no swap headroom",
                entry, dict(attributes, backoff_sec=entry.reclaim_backoff))
            self._request_swap_headroom()
            return

        entry.reclaim_backoff = 0.0
        if not outcome.ok:
            logger.warning("[reclaim] %s failed: %s", key, outcome.reason)
            _emit_reclaim_event(
                "RESOURCE_MEMORY_RECLAIM_FAILED", "warning",
                f"Reclaim for {entry.app_name} failed: {outcome.reason}",
                entry, dict(attributes, error=outcome.reason))
            return

        logger.info("[reclaim] %s: freed %d MB in %.0f ms",
                    key, outcome.reclaimed_bytes // _MB, outcome.elapsed_ms)
        _emit_reclaim_event(
            "RESOURCE_MEMORY_RECLAIM_APPLIED", "info",
            f"Reclaimed {outcome.reclaimed_bytes // _MB} MB from {entry.app_name}",
            entry, attributes)

    def _mark_reclaim_noise(self) -> None:
        """Declare the disk traffic from now on ours, for a short window.

        Called on both edges of every reclaim (ours and the manual path's) so a
        reclaim that runs longer than the window keeps extending it.
        """
        self._reclaim_quiet_until = time.time() + _RECLAIM_DISK_QUIET_SEC

    def _in_reclaim_quiet_window(self) -> bool:
        """True while disk pressure is attributable to our own reclaim."""
        return time.time() < self._reclaim_quiet_until

    def _claim_swap_growth_slot(self, now: float, urgent: bool) -> bool:
        """Rate limiter for growth attempts, shared by the tick and the listener.

        Returns True when the caller may go ahead, and stamps the slot as taken.
        Locked because the two callers run on different threads -- the monitor
        loop and the pressure monitor's refresh thread -- and the whole point of
        the listener path is that it fires at the same moment the tick does.
        Without this they would both submit on the critical edge and grow twice.
        """
        interval = _SWAP_GROWTH_URGENT_INTERVAL if urgent else _SWAP_GROWTH_INTERVAL
        with self._swap_growth_lock:
            if now - self._last_swap_growth_at < interval:
                return False
            self._last_swap_growth_at = now
            return True

    def _tick_swap_headroom(self, state: "_MonitorLoopState", pressure: str) -> None:
        """L0: pre-emptive swap growth, evaluated on the normal tick cadence.

        Not tied to critical pressure -- growth has its own watermarks and fires
        well before the memory is needed, because ``fallocate`` on ext4 costs
        milliseconds now and a crisis is exactly the wrong time to start
        allocating.  But *how often we are allowed to look* is tied to it: under
        critical the spacing drops to _SWAP_GROWTH_URGENT_INTERVAL, because the
        cost of one more look is a /proc/meminfo parse and the cost of not
        looking is an OOM kill.
        """
        if not self.control_manager.swap.enabled:
            return
        if not self._claim_swap_growth_slot(state.current_time,
                                            urgent=(pressure == "critical")):
            return
        self.control_manager.ensure_swap_headroom_async(dry_run=False)

    def _tick_swap_release(self, state: "_MonitorLoopState") -> None:
        """L0 in reverse: give a grown swapfile back once the pressure is gone.

        Runs on the same cadence as growth and is the only path that will fault
        a file's pages back into RAM, because it is the only one that can wait
        for a moment when there is room.  Not gated on ``swap.enabled``: turning
        growth off should hand back what earlier growth allocated, not strand
        it.  One file per round -- see should_shrink for the hysteresis.
        """
        if state.current_time - self._last_swap_release_at < _SWAP_GROWTH_INTERVAL:
            return
        self._last_swap_release_at = state.current_time
        self.control_manager.shrink_swap_if_idle_async()

    def _request_swap_headroom(self) -> None:
        """Ask L0 to add swap, if the operator enabled it.

        Called from the EAGAIN path.  Growth is planned and executed on the
        control manager's executor because the fallback allocator really writes
        the whole file.
        """
        if not self.control_manager.swap.enabled:
            logger.info("[reclaim] swap growth is disabled; L2 cannot make progress")
            return
        self.control_manager.ensure_swap_headroom_async(dry_run=False)

    def _upsert_auto_limited(self, app_id, public_id, app_name, limit_rates,
                             resource_limited, io_limited, priority, is_controlled,
                             limit_reason, pressure_level, cgroups,
                             limit_disks=None, pids=None, representative_pid=None,
                             resource_parts=None) -> None:
        """Record -- or refresh -- the registry entry of an auto-limited app.

        Under sustained critical pressure the same app can be limited again, or capped on
        the other channel (system pressure first, disk-IO later). Replacing the entry with
        a fresh LimitedApp would reset limited_at and drop the earlier limit_parts flag,
        leaking that cap: the restore path would never lift it again.

        So merge instead: keep the first timestamp and reason, union the applied parts,
        cgroups, disks and PIDs (a cgroup we forget is a cap we never lift), and refresh
        the pressure level. A channel that was just re-capped loses its partial flag.

        An existing *manual* entry is still replaced, not merged: those carry a user-set
        rate and their own baseline.
        """
        now = time.time()
        parts = {'cpu_mem_limited': bool(resource_limited), 'io_limited': bool(io_limited)}
        resource_parts = dict(resource_parts or {})
        partial_parts = {'sys': False, 'disk_io': False}
        state = None
        limited_at = now
        protection_id = _protection_id("auto", public_id, app_id, limited_at)
        cgroups = list(dict.fromkeys(cgroups or []))
        limit_disks = list(limit_disks or [])
        pids = set(pids or [])

        prev = self.all_limits.apps.get(app_id)
        prev_is_auto = prev is not None and prev.source == "auto"
        prev_lp = prev.limit_parts if prev_is_auto else {}
        # Channels this apply newly caps, versus any prior auto state: a reused
        # protection_id can gain a channel later (CPU/memory first, disk-IO after).
        # Gate on the *_limited flags -- the actual control result -- not on
        # limit_rates/resource_parts, which carry the whole priority profile and stay
        # populated even when disk pressure caps IO alone (resource_limited=False).
        # The cpu/memory split is left to _emit_control_events; here we only decide
        # whether the channel itself was newly hit.
        cpu_mem_newly = bool(resource_limited) and not bool(prev_lp.get('cpu_mem_limited'))
        io_newly = bool(io_limited) and not bool(prev_lp.get('io_limited'))
        if prev_is_auto:
            resource_parts = {
                'cpu': bool(prev.resource_parts.get('cpu')) or bool(resource_parts.get('cpu')),
                'memory': bool(prev.resource_parts.get('memory')) or bool(resource_parts.get('memory')),
            }
            parts = {
                'cpu_mem_limited': bool(prev.limit_parts.get('cpu_mem_limited')) or parts['cpu_mem_limited'],
                'io_limited': bool(prev.limit_parts.get('io_limited')) or parts['io_limited'],
            }
            partial_parts = {
                'sys': bool(prev.partial_parts.get('sys')) and not resource_limited,
                'disk_io': bool(prev.partial_parts.get('disk_io')) and not io_limited,
            }
            state = "partially_restored" if any(partial_parts.values()) else None
            limited_at = prev.limited_at or now
            protection_id = prev.protection_id or _protection_id("auto", public_id, app_id, limited_at)
            limit_reason = prev.limit_reason or limit_reason
            # The disk arm's limit_rates carries no cpu_rate/mem_rate, so overwriting
            # would leave the staged restore of an earlier CPU/memory cap without the
            # rate it has to double. Fresh values win for the keys this apply did set.
            limit_rates = {**(prev.limit_rates or {}), **(limit_rates or {})}
            cgroups = list(dict.fromkeys(list(prev.cgroups) + cgroups))
            limit_disks = list(dict.fromkeys(list(prev.limit_disks) + limit_disks))
            pids |= set(prev.pids)
            representative_pid = representative_pid or prev.representative_pid

        self.all_limits.apps[app_id] = LimitedApp(
            public_app_id=public_id,
            app_name=app_name,
            source="auto",
            limit_rates=limit_rates,
            limit_parts=parts,
            resource_parts=resource_parts,
            state=state,
            priority=priority or "undefined",
            is_controlled=bool(is_controlled),
            limit_reason=limit_reason,
            pressure_level=pressure_level,
            partial_parts=partial_parts,
            cgroups=cgroups,
            limit_disks=limit_disks,
            pids=pids,
            representative_pid=representative_pid,
            limited_at=limited_at,
            protection_id=protection_id,
        )

        # A re-cap of an already-capped channel yields no newly-hit channel, so
        # sustained-critical ticks neither flood the ledger nor pay for the process
        # snapshot inside _emit_control_events. resource_parts (merged) carries the
        # cpu/memory split for the channel just hit.
        applied_parts = {'cpu_mem_limited': cpu_mem_newly, 'io_limited': io_newly}
        if any(applied_parts.values()):
            _emit_control_events(
                "APPLIED", applied_parts, app_id=public_id, app_name=app_name,
                protection_id=protection_id,
                attributes={"app_name": app_name, "reason": limit_reason,
                            "priority": priority, "pressure_level": pressure_level,
                            "limit_rates": limit_rates,
                            "parts": applied_parts, "resource_parts": resource_parts,
                            "cgroups": cgroups, "pids": pids},
            )

    def _run_network_tick(self, state: "_MonitorLoopState") -> None:
        """Network sampling + handling. Runs every iteration (regardless
        of ``idle_check_interval``) so traffic pressure stays current.
        """
        do_pressure_eval = (state.current_time - state.last_network_sample_time) >= state.network_sample_interval
        if do_pressure_eval:
            state.last_network_sample_time = state.current_time
        self.network_controller.process_network_cycle(self.control_manager, do_pressure_eval)

    def _run_handle_loop(self):
        logger.info("Resource handle service is wait for processing")
        while self.is_running:
            try:
                coming_app = self.bpf_monitor.app_pending_queue.get(block=True, timeout=5)
                logger.debug(f"_run_handle_loop: Processing app {coming_app}")

                priority = app_utils.get_app_priority(app_name=coming_app["app_name"])
                logger.debug(f"_run_handle_loop: App {coming_app['app_name']} priority is {priority}")

                priority_num = app_utils.get_priority_value(priority)
                logger.debug(f"_run_handle_loop: priority value is {priority_num}")
                self.app_priority_queue.put((coming_app, priority_num))
                logger.debug(f"_run_handle_loop: Resource insufficient, {coming_app} app added to pending queue")

            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Resource handle loop failed: %s", exc, exc_info=True)
                time.sleep(2)
        logger.debug("Exiting _run_handle_loop")

    def _run_app_intercept_loop(self):
        logger.info("Resource app intercept service is wait for processing")

        self.bpf_monitor.bpf["events"].open_perf_buffer(self.bpf_monitor.print_event)

        monitor_apps = app_utils.get_controlled_apps()

        if monitor_apps:
            monitored_names = [app["app_name"] for app in monitor_apps if app.get("app_name") and app["app_name"].strip()]
            self.bpf_monitor.add_to_monitorlist(monitored_names)
            logger.info(f"Monitoring execve() for: {', '.join(monitored_names)}")

            logger.info(f"monitor_apps: {monitor_apps}")
            for app in monitor_apps:
                app_utils.adjust_oom_priority(app["app_id"], app["app_name"], app["priority"], app.get("cmdline", ""))
        else:
            logger.warning("No controlled apps to monitor")

        while self.is_running:
            try:
                self.bpf_monitor.bpf.perf_buffer_poll(timeout=100)
            except KeyboardInterrupt:
                logger.debug("Exiting...")
                break
            except Exception as e:
                logger.error(f"App intercept error: {str(e)}")
                time.sleep(3)
                break

    def _stressed_disks(self) -> list:
        """Names of the disks currently in the busy band, from the last pressure tick.

        Used to scope an ``io.max`` write to the disks that are actually under pressure.
        Returns an empty list when the information is unavailable, which callers must read
        as "unknown" (cap every disk) rather than "no disk is busy" (cap nothing).
        """
        try:
            stress = self.control_manager.get_disk_io_stress() or {}
            return list(stress.get('stressed_disks') or [])
        except Exception as e:
            logger.warning(f"[disk-io] could not read stressed disks: {e}")
            return []

    def _app_working_set_mb(self, cgroup_ids: "list[str]",
                            per_cg_mem_rss: dict) -> int:
        """This app's working set in MB, across every cgroup it spans.

        Prefers the RSS the top-consumer sample already carries (bytes, keyed by
        cgroup) so the common path costs nothing.  When the sample has no row for
        a cgroup -- an app resolved to more cgroups than were sampled, or a sample
        that predates the app's growth -- that cgroup is read from cgroupfs
        instead.  ``memory.current`` plus ``memory.swap.current``, matching the
        manual path's definition (balancer.py ``set_resource_limit``): pages this
        app pushed to swap are still its working set, and counting only RSS would
        shrink the cap every time reclaim succeeded.

        :returns: MB, or 0 when nothing could be read.
        """
        total_bytes = 0
        for cgroup in cgroup_ids:
            sampled = per_cg_mem_rss.get(cgroup)
            if sampled:
                total_bytes += int(sampled)
                continue
            try:
                current = self.control_manager.memory.read_current(cgroup) or 0
                swapped = self.control_manager.memory.read_swap_current(cgroup) or 0
            except Exception as exc:
                logger.error("[auto-limit] could not read memory of %s: %s", cgroup, exc)
                continue
            total_bytes += current + swapped
        return total_bytes // _MB

    def _auto_mem_high_mb(self, app_id: str, extra_cgroup_ids: "list[str]",
                          per_cg_mem_rss: dict, total_mem_mb: int,
                          rate: "Optional[float]", app_name: str) -> "Optional[int]":
        """The ``memory.high`` an auto limit should write, in MB.

        The system share (``total_mem * rate``) is a ceiling on how much of the
        machine one app may hold, not a limit on the app: a process using 2% of
        RAM "capped" at 30% of RAM is not capped at all, and the whole
        many-small-apps case is exactly that shape.  Sizing against the app's own
        working set instead means ``rate < 1`` always buys a real reduction,
        which is what makes the entry worth registering and what gives L2
        something to reclaim against.

        The system share is kept as a ceiling because the working set counts
        swapped pages and can therefore exceed MemTotal, while ``memory.high`` is
        measured against RAM alone.

        A working set of 0 means the read failed, not that the app is empty --
        fall back to the system share rather than writing a cap of 0 MB.
        """
        if not rate:
            return None
        system_share = int(total_mem_mb * rate)
        working_set = self._app_working_set_mb([app_id] + list(extra_cgroup_ids),
                                               per_cg_mem_rss)
        if working_set <= 0:
            logger.warning(
                "[auto-limit] %s: no memory reading; falling back to the system "
                "share %d MB", app_name, system_share)
            return system_share
        # Floor of 1 MB for the same reason as the manual path: a cap of 0 would
        # put the app in permanent reclaim rather than limit it.
        usage_share = max(1, int(working_set * rate))
        mem_high = min(system_share, usage_share)
        logger.debug(
            "[auto-limit] %s: working set %d MB * %.2f = %d MB vs system share "
            "%d MB -> memory.high %d MB",
            app_name, working_set, rate, usage_share, system_share, mem_high)
        return mem_high

    @_serialized_limit_operation
    def _apply_resource_limits(self, target_app, app_id, limit_rates, is_controlled,
                               is_disk_io_stressed=False, pressure_level=""):
        """Apply resource limits (common logic).

        *pressure_level* is the level of the channel that triggered this limit; it is
        recorded on the registry entry so the UI can say *why* the app was limited.
        """
        app_name = (
            (target_app.get('app') or {}).get('name')
            or target_app.get('process', {}).get('name')
            or app_id
        )
        total_mem = self.resource_monitor.get_total_memory()
        logger.debug(f"Adjusting resources for app: {app_id}")

        extra_cgroup_ids = target_app.get('extra_cgroups', [])
        manual_cgroups = [
            cgroup_id for cgroup_id in [app_id] + list(extra_cgroup_ids)
            if self.all_limits.by_any_id(cgroup_id, source="manual") is not None
        ]
        if manual_cgroups:
            logger.info(
                "Skipped auto limit for %s: cgroup(s) already manual-limited: %s",
                app_name, ", ".join(manual_cgroups),
            )
            return
        per_cg_mem_rss = target_app.get('per_cgroup_mem_rss', {})
        per_cg_cpu = target_app.get('per_cgroup_cpu', {})

        resource_limited = False
        io_limited = False
        limited_disks: list = []

        if not is_disk_io_stressed:
            # Memory pressure won this tick, and the CPU/memory cap about to land
            # here makes this app a reclaim target: L2 will ask the kernel to push
            # its anon pages out to swap.  That write goes down the very path an
            # io.max would be throttling, so an app carrying both caps reclaims at
            # the throttled rate -- or not at all.  Lift the disk cap first, through
            # the normal restore so the registry, the RECOVERED event and the UI all
            # agree; the disk channel gets re-capped by its own timer if the disk is
            # still hot once memory is off the critical list.
            self._clear_io_cap_before_sys_limit(app_id, app_name)

            cpu_rate = int(100 * limit_rates["cpu_rate"]) if limit_rates.get("cpu_rate") else None
            mem_rate = self._auto_mem_high_mb(
                app_id, extra_cgroup_ids, per_cg_mem_rss, total_mem,
                limit_rates.get("mem_rate"), app_name)

            if (cpu_rate is not None or mem_rate is not None) and self.is_running:
                if extra_cgroup_ids:
                    all_ids = [app_id] + extra_cgroup_ids
                    mem_dist = _split_proportionally(mem_rate, all_ids, per_cg_mem_rss)
                    cpu_dist = _split_proportionally(cpu_rate, all_ids, per_cg_cpu)
                    primary_ok = self.control_manager.adjust_resources(
                        app_id, "critical",
                        cpu_quota=cpu_dist.get(app_id, cpu_rate),
                        mem_high=mem_dist.get(app_id, mem_rate),
                    )
                    if primary_ok:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name} ({app_id})")
                    for extra_id in extra_cgroup_ids:
                        ok = self.control_manager.adjust_resources(
                            extra_id, "critical",
                            cpu_quota=cpu_dist.get(extra_id, cpu_rate),
                            mem_high=mem_dist.get(extra_id, mem_rate),
                        )
                        logger.info(
                            f"{'Successfully limited' if ok else 'Failed to limit'} "
                            f"CPU/Memory for extra cgroup {extra_id}"
                        )
                else:
                    auto_limit = self.control_manager.adjust_resources(
                        app_id,
                        "critical",
                        cpu_quota=cpu_rate,
                        mem_high=mem_rate,
                    )
                    if auto_limit:
                        resource_limited = True
                        logger.info(f"Successfully limited CPU/Memory for {app_name}")

        if is_disk_io_stressed and limit_rates.get("disk_io_rate"):
            io_limits = limit_rates.get("disk_io_rate", {})
            if io_limits and self.is_running:
                # Cap only the disks that are actually saturated. Without a filter the same
                # io.max is written to every physical disk, so an app is throttled on disks
                # that have nothing to do with the pressure. None/empty means "could not
                # tell" -- fall back to all disks rather than silently skipping the limit.
                stressed_disks = self._stressed_disks()
                limited_disks = list(stressed_disks)
                limits = self._scaled_io_limits(io_limits, stressed_disks or None)
                logger.info(
                    f"[disk-io] applying io.max to {app_name!r} (app_id={app_id!r}): "
                    f"base rd={io_limits['read']}MB/s wr={io_limits['write']}MB/s "
                    f"riops={io_limits['read_iops']} wiops={io_limits['write_iops']} -> "
                    f"per-disk {({d: v['wbps'] // 1024 ** 2 for d, v in limits.items() if d != 'default'})} "
                    f"MB/s write; disks={stressed_disks or 'ALL (no stressed-disk info)'} "
                    f"extra_cgroups={extra_cgroup_ids}"
                )
                io_limited = self.io_ctl.set_disk_io_throttle(
                    app_id, limits=limits, disk_filter=stressed_disks or None)
                if not io_limited:
                    logger.error(f"Failed to set IO limit for {app_name}")
                for extra_id in extra_cgroup_ids:
                    self.io_ctl.set_disk_io_throttle(
                        extra_id, limits=limits, disk_filter=stressed_disks or None)

        if resource_limited or io_limited:
            # app_id addresses cgroups (it may have been rewritten to the resolved primary
            # cgroup); the DB row and the UI know the app by its public id. Writing the
            # cgroup name into public_app_id makes every later status update miss its row.
            public_id = target_app.get('public_app_id') or app_id
            existing = self.all_limits.apps.get(app_id)
            if existing is None or existing.source != "auto":
                self._warn_on_shared_cgroups(
                    app_name,
                    target_app.get('resolved_cgroup_paths') or [],
                    target_app.get('pids') or [],
                )
            self._upsert_auto_limited(
                app_id, public_id, app_name, limit_rates,
                resource_limited=resource_limited,
                io_limited=io_limited,
                priority=target_app.get('resolved_priority'),
                is_controlled=is_controlled,
                limit_reason="disk_pressure" if is_disk_io_stressed else "system_pressure",
                pressure_level=pressure_level,
                cgroups=[app_id] + list(extra_cgroup_ids),
                limit_disks=limited_disks,
                pids=target_app.get('pids'),
                representative_pid=(target_app.get('process') or {}).get('pid'),
                # Only claim cpu/memory when this apply actually capped that channel:
                # under disk pressure resource_limited is False even though the
                # priority limit_rates still carry cpu_rate/mem_rate.
                resource_parts={
                    'cpu': resource_limited and bool(limit_rates.get('cpu_rate')),
                    'memory': resource_limited and bool(limit_rates.get('mem_rate')),
                },
            )

            if is_controlled:
                app_utils.update_app_status(public_id, "limited")

            app_utils.callback_manager.send_callback_notification({
                'app_id': public_id,
                'app_name': app_name,
                'status': "limited",
                'purpose': "app"
            }, False)

    def restore_resources(self, app_id, app_name, limit_rates, limit_parts, restore_type):
        """
        Common resource restore logic.
        :param app_id: application ID
        :param app_name: application name
        :param limit_rates: rate-limit configuration
        :param limit_parts: which resources to act on this call.  The separated
            policy passes a single channel's flag so only that channel's cap is
            lifted; a full restore therefore clears exactly the flags it was
            asked for on the registry entry and leaves the rest as they were.
        :param restore_type: restore scope ("partial" or "full")
        :return: (success, restored_parts)
        """
        restore_success = True
        entry = self.all_limits.apps.get(app_id)
        extra_ids = entry.cgroups[1:] if entry else []

        if self.is_running:
            if limit_parts.get('cpu_mem_limited', False):
                if restore_type == "partial":
                    cpu_restore = int(100 * limit_rates["cpu_rate"] * 2) if "cpu_rate" in limit_rates else None
                    mem_restore = int(self.resource_monitor.get_total_memory() * limit_rates[
                        "mem_rate"] * 2) if "mem_rate" in limit_rates else None
                    if not self.control_manager.adjust_resources(
                        app_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                    ):
                        logger.error(f"Failed to partially restore CPU/Memory for {app_name}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.control_manager.adjust_resources(
                            extra_id, "medium", cpu_quota=cpu_restore, mem_high=mem_restore, is_restore=False
                        )
                else:  # full restore
                    cpu_mem_restored = self.control_manager.adjust_resources(app_id, "low")
                    if not cpu_mem_restored:
                        logger.error(f"Failed to fully restore CPU/Memory for {app_name}")
                        restore_success = False
                    elif entry is not None:
                        entry.limit_parts['cpu_mem_limited'] = False
                    for extra_id in extra_ids:
                        self.control_manager.adjust_resources(extra_id, "low")
            if limit_parts.get('io_limited', False):
                # Only the disks the cap was written to -- a partial restore is still a cap.
                disks = (entry.limit_disks or None) if entry is not None else None
                if restore_type == "partial" and "disk_io_rate" in limit_rates:
                    io_limits = limit_rates["disk_io_rate"]
                    # Re-scaled by media, exactly like the original cap: doubling the
                    # raw rate row instead would hand a slow disk several times the cap
                    # it was actually given, since its coefficient is the smallest --
                    # "2x" has to mean 2x of what was written.
                    relaxed = {k: io_limits[k] * 2
                               for k in ('read', 'write', 'read_iops', 'write_iops')}
                    limits = self._scaled_io_limits(relaxed, disks)
                    logger.info(
                        f"[disk-io] partial restore for {app_name!r} (app_id={app_id!r}): "
                        f"relaxing io.max to 2x rd={io_limits['read']*2}MB/s wr={io_limits['write']*2}MB/s "
                        f"on disks={disks or 'ALL'} (extra_cgroups={extra_ids})"
                    )
                    if not self.io_ctl.set_disk_io_throttle(
                            app_id, limits=limits, disk_filter=disks):
                        logger.error(f"Failed to partially restore disk IO for {app_name}")
                        restore_success = False
                    for extra_id in extra_ids:
                        self.io_ctl.set_disk_io_throttle(
                            extra_id, limits=limits, disk_filter=disks)
                elif restore_type == "full":
                    logger.info(
                        f"[disk-io] full restore for {app_name!r} (app_id={app_id!r}): "
                        f"removing io.max cap on disks={disks or 'ALL'} "
                        f"(extra_cgroups={extra_ids})"
                    )
                    if not self.io_ctl.restore_disk_io_throttle(app_id, disk_filter=disks):
                        logger.error(f"Failed to fully restore disk IO for {app_name}")
                        restore_success = False
                    elif entry is not None:
                        entry.limit_parts['io_limited'] = False
                    for extra_id in extra_ids:
                        self.io_ctl.restore_disk_io_throttle(extra_id, disk_filter=disks)

        return restore_success

    def _handle_disk_io_stressed(self, top_consumers):
        """Pick the first *throttleable* disk-IO consumer among the top-N and the rates
        to cap it at.

        Walks the candidates in descending-IO order and selects the first that is:
          1. not a Critical app -- those are never throttled for disk IO;
          2. not already under an auto disk-IO limit, so successive critical ticks
             move on to the next heavy consumer instead of re-limiting one app;
          3. doing enough disk IO for a cap to matter -- see
             :meth:`_qualifies_for_throttle`, which judges each disk the app touches
             against that disk's media floor. Bandwidth alone is not sufficient: a 4k
             random workload is bandwidth-light but IOPS-heavy, and capping it does
             relieve the device.

        Controlled non-critical apps are capped at their own priority's rates;
        uncontrolled apps count as ``undefined`` (lowest tier), so a heavy unmanaged
        writer is throttled harder than a managed Low app. The gate uses the
        candidate's own sampled IO rate, with no extra per-app sampling.

        A controlled app's cgroup set is resolved via :meth:`_resolve_controlled_target`,
        as the CPU/memory critical path does: the top-consumer sample keys an app by a
        single cgroup (or, for ``process_names`` apps, by a public id that need not be a
        systemd unit), and without resolving it ``set_disk_io_throttle`` finds no
        ``io.max`` to write and the limit silently never lands.

        :return: ``(should_adjust, is_controlled, app_id, limit_rates, target_app, index)``,
            where ``index`` is the candidate's position in *top_consumers* so the caller
            can drop exactly the entry acted on. Returns
            ``(False, False, None, None, None, None)`` when nothing is worth limiting --
            including while our own reclaim is still echoing through the disk signal
            (:data:`_RECLAIM_DISK_QUIET_SEC`).
        """
        if self._in_reclaim_quiet_window():
            # Not a skip of one candidate but of the whole arm: the pressure
            # belongs to our reclaim, so *every* candidate here would be capped
            # for traffic it did not generate.
            logger.info(
                "[disk-io] stand down: disk pressure is our own reclaim's swap "
                "writeback (quiet window ends in %.0f s)",
                self._reclaim_quiet_until - time.time())
            return False, False, None, None, None, None

        for idx, app_info in enumerate(top_consumers or []):
            app_id = app_info['app'].get('id') if app_info.get('app') else None
            if not app_id:
                continue
            sampled_cgroup = (
                os.path.basename((app_info.get('cgroup') or '').rstrip('/'))
                or app_info.get('cgroup_id')
                or ''
            ).strip()
            proc = app_info.get('process', {})
            app_name = (proc.get('name') or '').lower()
            cand_mb = (proc.get('io_read_rate') or 0.0) + (proc.get('io_write_rate') or 0.0)
            cand_iops = (proc.get('io_read_iops') or 0.0) + (proc.get('io_write_iops') or 0.0)

            is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
            priority = controlled_data.get('priority') if controlled_data else None
            # Built once and reused by every outcome below, so a candidate costs one log line
            # whether it is skipped or selected.
            cand = (f"#{idx} {app_id!r} name={app_name!r} io={cand_mb:.1f}MB/s "
                    f"{cand_iops:.0f}iops priority={priority!r}")

            # 1) Critical apps are never throttled -- skip to the next candidate.
            if is_controlled and str(priority).lower() == 'critical':
                logger.info(f"[disk-io] skip {cand}: Critical app, never throttled")
                continue
            # 1b) Excluded by a user restore. Checked again here because this arm holds
            #     its batch across ticks while the disk sits at "high".
            candidate_cgroups = list(app_info.get('extra_cgroups') or [])
            if sampled_cgroup and sampled_cgroup not in candidate_cgroups:
                candidate_cgroups.append(sampled_cgroup)
            with self.all_limits.lock:  # the REST thread can add/remove entries mid-walk
                excluded = self.all_limits.is_excluded(
                    app_id, app_name, candidate_cgroups)
            if excluded is not None:
                logger.info(f"[disk-io] skip {cand}: excluded from auto-limit "
                             f"({excluded.get('key')})")
                continue
            # 2) Already io-limited (auto) -- skip so we advance to the next heavy user.
            #    Checked under every id the candidate can appear as: this runs *before* the
            #    cgroup resolution below, so the id in hand is the sample's, while the
            #    registry entry was keyed by the resolved cgroup. Matching only one of them
            #    would re-cap the same app on every critical tick and never reach the
            #    second-heaviest consumer.
            known_ids = [i for i in (app_id, sampled_cgroup, (controlled_data or {}).get('app_id')) if i]
            existing = next(
                (e for e in (self.all_limits.by_any_id(i, source="auto") for i in known_ids)
                 if e and e[1].limit_parts.get('io_limited')), None)
            if existing:
                logger.info(f"[disk-io] skip {cand}: already io-limited as {existing[0]!r}")
                continue
            # 3) Only limit a genuinely heavy disk user -- below its disk's floor a cap
            #    won't help. Judged per disk, per media: see _qualifies_for_throttle.
            qualifies, floor_reason = self._qualifies_for_throttle(proc)
            if not qualifies:
                logger.info(f"[disk-io] skip {cand}: too light -- {floor_reason}")
                continue

            # Resolve a controlled app to its real cgroup set before we try to write io.max.
            if is_controlled and controlled_data:
                resolved_id = self._resolve_controlled_target(app_info, controlled_data)
                if resolved_id:
                    app_id = resolved_id
            elif sampled_cgroup:
                # Disk-IO throttling writes cgroup io.max. Keep display identity from
                # process naming, but target the sampled cgroup basename for writes.
                app_id = sampled_cgroup

            rates = self.get_limited_rates(priority or "undefined")
            # On the candidate, not the return tuple: the apply path only needs it to
            # report the priority to the UI.
            app_info['resolved_priority'] = (priority or "undefined")
            io_rates = (rates or {}).get('disk_io_rate') or {}
            logger.info(
                f"[disk-io] SELECTED {cand} -> cap {app_id!r} at "
                f"'{priority or 'undefined'}' rates rd={io_rates.get('read')} "
                f"wr={io_rates.get('write')} MB/s riops={io_rates.get('read_iops')} "
                f"wiops={io_rates.get('write_iops')} (pre-media-scale; qualified on "
                f"{floor_reason}) extra_cgroups={app_info.get('extra_cgroups')}"
            )
            return True, is_controlled, app_id, rates, app_info, idx

        logger.info(
            "[disk-io] no throttleable disk consumer among %d candidates "
            "(all Critical / already-limited / below threshold)", len(top_consumers or [])
        )
        return False, False, None, None, None, None

    def _resolve_controlled_target(self, app_info: dict, controlled_data: dict) -> Optional[str]:
        """Resolve a controlled app's real cgroups and rewrite ``app_info`` so
        the auto-limit apply path fans out across all of them.

        The top-consumer sample keys an app by a single cgroup (or, for
        ``process_names`` apps, by a public app id that is not a systemd unit
        name), which makes multi-cgroup controlled apps either under-limited or
        not limited at all.  This resolves the controlled app's full cgroup set
        via :func:`app_utils.get_app_resource_usage` (the same source the manual
        path uses) and mutates ``app_info`` in place:

          * ``extra_cgroups``       -> the non-primary cgroup basenames
          * ``pids``                -> the app's live PIDs (for close-detection)
          * ``per_cgroup_mem_rss`` / ``per_cgroup_cpu`` -> basename-keyed weights
            used to split the limit proportionally.

        Returns the primary (lexicographically-first) cgroup basename to use as
        the limit key, or ``None`` if the cgroups could not be resolved (in
        which case the caller keeps the original top-consumer id).
        """
        public_id = controlled_data.get('app_id')
        name = controlled_data.get('app_name') or ''
        usage = app_utils.get_app_resource_usage(public_id, name) or {}
        cgroup_paths = usage.get('cgroup_paths') or (
            [usage['cgroup_path']] if usage.get('cgroup_path') else []
        )
        effective_ids = [os.path.basename(p) for p in cgroup_paths if p]
        if not effective_ids:
            logger.warning(
                f"Could not resolve cgroups for controlled app '{name}' "
                f"({public_id}); limiting the top-consumer cgroup only")
            return None

        primary = min(effective_ids)
        extras = [e for e in effective_ids if e != primary]

        app_info['extra_cgroups'] = extras
        app_info['resolved_cgroup_paths'] = cgroup_paths
        # The returned id addresses cgroups from here on, so the id the DB and the UI know
        # the app by has to survive separately -- see _apply_resource_limits.
        app_info['public_app_id'] = public_id
        if usage.get('pids'):
            app_info['pids'] = usage['pids']
        if usage.get('per_cgroup_mem'):
            app_info['per_cgroup_mem_rss'] = usage['per_cgroup_mem']
        if usage.get('per_cgroup_cpu_delta'):
            app_info['per_cgroup_cpu'] = usage['per_cgroup_cpu_delta']

        logger.info(
            f"Controlled app '{name}' resolved to cgroups {effective_ids}; "
            f"primary={primary}, extras={extras}")
        return primary

    def _warn_on_shared_cgroups(self, app_name: str, cgroup_paths: list, app_pids: list) -> None:
        """Log cgroups where a newly applied limit will also hit other processes.

        A limit is a property of a cgroup, not of a process, so an app that never got a
        cgroup of its own -- launched straight from a shell, so it lives in that terminal's
        ``vte-spawn-*.scope`` -- can only be limited by limiting everything in there with
        it. That is unavoidable and still the correct action, but it must not be silent:
        it is also exactly what an over-matched process name looks like, and the two are
        indistinguishable from the limit log alone.
        """
        mount = getattr(self.config, 'cgroup_mount', '/sys/fs/cgroup') or '/sys/fs/cgroup'
        mine = {int(p) for p in app_pids}
        if not mine:
            return
        for cg in cgroup_paths:
            try:
                with open(os.path.join(mount, str(cg).lstrip('/'), 'cgroup.procs')) as f:
                    resident = {int(line) for line in f if line.strip()}
            except (OSError, ValueError):
                continue
            others = resident - mine
            if others:
                logger.warning(
                    f"[limit-scope] cgroup {os.path.basename(str(cg))} for app {app_name!r} also holds "
                    f"{len(others)} process(es) not belonging to it (e.g. {sorted(others)[:5]}); "
                    f"the limit applies to them too")

    def _handle_critical_pressure(self, top_consumers, reach_threshold):
        """Handle resource pressure (processes one app per invocation)."""
        if not top_consumers or not top_consumers[0]:
            return False, False, None, None

        self._critical_counter = getattr(self, '_critical_counter', 0)
        self._last_notification_time = getattr(self, '_last_notification_time', 0)

        app_info = top_consumers[0]
        app_id = app_info['app'].get('id') if app_info.get('app') else None
        app_name = (app_info.get('process', {}).get('name') or '').lower()

        is_controlled, controlled_data = app_utils.get_app_control_info(app_id, app_name)
        priority = controlled_data.get('priority') if controlled_data else None

        usage_data = self.resource_monitor.get_resource_usage()
        is_sys_busy = usage_data['cpu']['is_busy'] or usage_data['memory']['is_busy']

        if is_sys_busy and not reach_threshold:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "high_usage_by_multiple_instances",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0
            return False, False, None, None

        if not is_controlled or priority != 'critical':
            self._critical_counter = 0
            # A controlled app may span several cgroups while the top-consumer
            # sample only surfaces one of them (and, for process_names apps,
            # reports a public app id that is not a valid systemd unit). Once
            # we know it is controlled, resolve the app's full cgroup set and
            # rewrite the target so the limit fans out to every cgroup — the
            # same treatment the manual limit path already applies.
            if is_controlled and controlled_data:
                resolved_id = self._resolve_controlled_target(app_info, controlled_data)
                if resolved_id:
                    app_id = resolved_id
            else:
                # Resource control operates on cgroups, while an unmanaged
                # sample can carry a bare process name as its app id. Resolve
                # to the sampled scope before applying and recording the cap;
                # otherwise controller resolves the scope only transiently and
                # shutdown cannot find the actual cgroup to restore.
                sampled_cgroup = os.path.basename(
                    (app_info.get('cgroup') or '').rstrip('/')) or app_info.get('cgroup_id')
                if sampled_cgroup:
                    app_info['public_app_id'] = app_id
                    app_id = sampled_cgroup
            # On the candidate, not the return tuple: the apply path only needs it to
            # report the priority to the UI.
            app_info['resolved_priority'] = (priority or "undefined")
            return True, is_controlled, app_id, self.get_limited_rates(priority or "undefined")

        self._critical_counter += 1
        if self._critical_counter >= 1:
            current_time = time.time()
            if current_time - self._last_notification_time >= self.config.cooldown_time:
                app_utils.callback_manager.send_callback_notification({
                    'app_id': "",
                    'app_name': "",
                    'status': "manual_app_limit_by_user",
                    'purpose': "notify"
                }, False)
                self._last_notification_time = current_time
            self._critical_counter = 0

        return False, False, None, None

    def _restore_entry(self, entry: "LimitedApp", notify: bool,
                       notify_status: str = "app_closed_limit_restored") -> bool:
        """Fully restore one already-removed limited app's cgroups.

        Shared restore path for the shutdown sweep
        (:meth:`restore_all_limited_apps_resources`, ``notify=False``) and
        the reaper (:meth:`_reap_closed_apps`, ``notify=True``).  The caller
        must have already popped ``entry`` from ``self.all_limits.apps`` (and its
        ``manual_limit_baseline``) under the lock — this method only touches
        cgroups, never the registry, so it is safe to run outside the lock.

        When ``notify`` is True and the restore succeeds, emits the
        app-status "running" callback plus a "notify" callback carrying
        *notify_status* so the UI can explain why the limit was lifted (the
        app closed, or the user asked for it).
        """
        cgroups = entry.cgroups or [entry.public_app_id]
        key = cgroups[0]
        app_name, source = entry.app_name, entry.source
        restore_success = True
        restored_parts = {'cpu_mem_limited': False, 'io_limited': False}
        failed_parts = {'cpu_mem_limited': False, 'io_limited': False}
        logger.info(f"Restoring resources for {source} limited app: {key}, name: {app_name}")
        try:
            gone = 0
            for idx, cg in enumerate(cgroups):
                is_primary = (idx == 0)
                # A closed app's cgroup is often already removed; there is
                # nothing to restore, so skip it quietly instead of retrying
                # systemctl and logging errors.
                if not self._cgroup_exists(cg):
                    gone += 1
                    logger.debug(f"Cgroup {cg} already gone; nothing to restore")
                    continue
                if entry.limit_parts.get('cpu_mem_limited', False):
                    if not self.control_manager.adjust_resources(cg, "low") and is_primary:
                        logger.error(f"Failed to restore CPU/Memory for {source} limited app {cg}")
                        restore_success = False
                        failed_parts['cpu_mem_limited'] = True
                if entry.limit_parts.get('io_limited', False):
                    if not self.io_ctl.restore_disk_io_throttle(
                            cg, disk_filter=entry.limit_disks or None) and is_primary:
                        logger.error(f"Failed to remove IO limits for {source} limited app {cg}")
                        restore_success = False
                        failed_parts['io_limited'] = True

            if entry.frozen and not self._thaw_cgroups(cgroups):
                restore_success = False

            if gone == len(cgroups):
                logger.info(f"All cgroups for {source} limited app {key} already gone; limit already cleared")
            elif restore_success:
                logger.info(f"{source.capitalize()} limited app resources restoration completed")
        except Exception as e:
            logger.error(f"Failed to restore resources for app {key}: {str(e)}")
            restore_success = False
            # An unexpected raise leaves the loop half-done, so the state of the
            # remaining parts is unknown. Fail safe: mark every limited part
            # failed rather than let the finally block below infer RECOVERED for
            # parts we never confirmed -- a false RECOVERED is worse than a
            # conservative FAILED in the lifecycle ledger.
            for part, limited in entry.limit_parts.items():
                if limited:
                    failed_parts[part] = True
        finally:
            for part, limited in entry.limit_parts.items():
                if limited and not failed_parts.get(part, False):
                    restored_parts[part] = True
                elif limited:
                    failed_parts[part] = True
            _emit_control_events(
                "RECOVERED", restored_parts, app_id=entry.public_app_id,
                app_name=app_name, protection_id=entry.protection_id,
                attributes={"app_name": app_name, "limit_parts": entry.limit_parts,
                            "priority": entry.priority, "resource_parts": entry.resource_parts,
                            "cgroups": entry.cgroups, "pids": entry.pids},
            )
            _emit_control_events(
                "FAILED", failed_parts, app_id=entry.public_app_id,
                app_name=app_name, protection_id=entry.protection_id,
                attributes={"app_name": app_name, "limit_parts": entry.limit_parts,
                            "priority": entry.priority, "resource_parts": entry.resource_parts,
                            "cgroups": entry.cgroups, "pids": entry.pids},
            )

        if notify and restore_success:
            # Recompute runtime state instead of forcing "stopped": when one
            # limited instance exits, other instances of the same app may
            # still be running and should keep the Limit button available.
            next_status = app_utils.check_app_running_status(
                entry.public_app_id,
                app_name,
                "",
            )
            app_utils.update_app_status(entry.public_app_id, next_status)
            app_utils.callback_manager.send_callback_notification({
                'app_id': entry.public_app_id,
                'app_name': app_name,
                'status': next_status,
                'purpose': "app"
            }, False)
            # Tell the UI why the limit is gone (app closed / user restored it).
            app_utils.callback_manager.send_callback_notification({
                'app_id': entry.public_app_id,
                'app_name': app_name,
                'status': notify_status,
                'purpose': "notify"
            }, False)

        return restore_success

    def restore_all_limited_apps_resources(self):
        """Restore all limited apps resources (called on shutdown)."""
        with self.all_limits.lock:
            if not self.all_limits.apps:
                logger.info("No limited apps to restore")
                return

            auto_n = sum(1 for a in self.all_limits.apps.values() if a.source == "auto")
            manual_n = sum(1 for a in self.all_limits.apps.values() if a.source == "manual")
            logger.info(
                f"Restoring resources for {auto_n} limited apps and "
                f"{manual_n} manual limited apps")

            for key in list(self.all_limits.apps):
                entry = self.all_limits.apps.pop(key, None)
                self.all_limits.manual_limit_baseline.pop(key, None)
                if entry is not None:
                    self._restore_entry(entry, notify=False)

        logger.info("All limited apps resources restoration completed")

    def _cgroup_exists(self, cgroup_id: str) -> bool:
        """Return True if a cgroup directory named *cgroup_id* still exists.

        Used before restoring a closed app: if the scope/service is already
        gone its limit died with it, so restoration is skipped to avoid noisy
        systemctl retries. On lookup errors, assume it exists so restoration proceeds.
        """
        mount = getattr(self.config, "cgroup_mount", "/sys/fs/cgroup")
        try:
            result = subprocess.run(
                ["find", mount, "-name", cgroup_id, "-type", "d"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5,
            )
            return bool(result.stdout.strip())
        except Exception:
            return True

    # Signals whose default action terminates the process.
    _FATAL_PENDING_SIGNALS = frozenset({2, 9, 15})  # SIGINT, SIGKILL, SIGTERM

    def _pid_gone_or_dying(self, pid: int) -> bool:
        """True if *pid* is dead, a zombie, or has a pending fatal signal.

        We treat a pending SIGINT/SIGTERM/SIGKILL as "dying" even before the
        task falls into 'D': this lets the reaper lift manual limits quickly so
        Ctrl+C exits are not delayed by strict throttles.
        """
        try:
            with open(f"/proc/{pid}/status") as f:
                content = f.read()
        except (FileNotFoundError, ProcessLookupError):
            return True
        except Exception:
            return False  # can't tell — treat as alive, don't restore

        fields = {}
        for line in content.splitlines():
            if line.startswith(("State:", "SigPnd:", "ShdPnd:")):
                key, _, value = line.partition(":")
                fields[key] = value.strip()

        pending = 0
        for key in ("ShdPnd", "SigPnd"):
            try:
                pending |= int(fields.get(key, "0").split()[0], 16)
            except (ValueError, IndexError):
                pass

        fatal_mask = 0
        for sig in self._FATAL_PENDING_SIGNALS:
            fatal_mask |= 1 << (sig - 1)

        state = (fields.get("State", "") or " ")[0]
        if state == 'Z':
            return True
        if pending & fatal_mask:
            logger.info(f"PID {pid} has a pending fatal signal; treating as gone")
            return True
        return False

    def _target_still_present(self, target_app: dict, app_id: str) -> bool:
        """Is the app this candidate names still around to be limited?

        A candidate is a snapshot: by the time it is acted on the app may have
        exited, and every limit call then fails three times against a unit that
        no longer exists ("Unit <x> not found") before being given up on.  PIDs
        are checked first because it is a cheap read of /proc; only when all of
        them are gone do we pay for the cgroup lookup, which still says "present"
        for a long-lived scope whose worker PIDs have churned.

        Unknown-by-construction cases (no PID snapshot) fall back to the cgroup
        check, and an unreadable cgroup tree is read as "present" so a lookup
        failure never silently suppresses a limit.
        """
        pids = [pid for pid in (target_app.get('pids') or []) if pid]
        if any(not self._pid_gone_or_dying(pid) for pid in pids):
            return True
        if self._cgroup_exists(app_id):
            return True
        logger.warning(
            "Skipping limit for %r: app is already gone (pids=%s, no cgroup)",
            app_id, sorted(pids)[:5] or "unknown",
        )
        return False

    def _is_app_closed(self, entry: "LimitedApp") -> bool:
        """Decide whether a limited app has closed, so its limit can be lifted.

        Detection is PID-based, not cgroup-emptiness-based, since an app may
        share its cgroup with other processes that keep it non-empty.

          * All snapshot PIDs gone or dying-but-pinned -> the app closed.

        For multi-cgroup apps, a subset of PIDs/cgroups can naturally exit
        earlier than others. We should keep the app in limited/running state
        while at least one tracked PID is still alive.

        Callers must have already filtered out entries with no PID snapshot.
        """
        alive = [pid for pid in entry.pids if not self._pid_gone_or_dying(pid)]
        if not alive:
            return True

        if entry.representative_pid and self._pid_gone_or_dying(entry.representative_pid):
            if entry.source == "auto" or entry.adopted_from_auto:
                try:
                    status = app_utils.check_app_running_status(
                        entry.public_app_id,
                        entry.app_name,
                        "",
                    )
                except Exception as e:
                    logger.warning(
                        f"Reaper: representative PID for limited app {entry.public_app_id} "
                        f"is gone, but running-status check failed: {e}")
                else:
                    if status == "stopped":
                        logger.info(
                            f"Reaper: representative PID {entry.representative_pid} for "
                            f"limited app {entry.public_app_id} is gone and app is stopped")
                        return True

        return False

    def _reap_closed_apps(self) -> None:
        """One reaper pass: restore limits for any app that has closed.

        Runs in the monitor thread (so it never races the auto limit/restore
        logic) on a short, fixed cadence independent of
        ``monitor_idle_check_interval``.  Closed entries are popped under the
        lock — so a concurrent manual restore cannot double-act — and the
        actual cgroup restore + notifications happen outside the lock to keep
        the hold time short.
        """
        closed: list = []
        with self.all_limits.lock:
            for key in list(self.all_limits.apps):
                entry = self.all_limits.apps.get(key)
                if entry is None:
                    continue
                if not entry.pids:
                    try:
                        status = app_utils.check_app_running_status(
                            entry.public_app_id,
                            entry.app_name,
                            "",
                        )
                    except Exception as e:
                        logger.warning(
                            f"Reaper: no PID snapshot for limited app {key} "
                            f"({entry.app_name}); running-status check failed: {e}")
                        continue
                    if status == "stopped":
                        logger.info(
                            f"Reaper: no PID snapshot for limited app {key} "
                            f"({entry.app_name}), but app is stopped; restoring")
                        self.all_limits.apps.pop(key, None)
                        self.all_limits.manual_limit_baseline.pop(key, None)
                        self.all_limits.remove_exclusion(key)
                        closed.append(entry)
                        continue
                    logger.warning(
                        f"Reaper: no PID snapshot for limited app {key} "
                        f"({entry.app_name}); app still appears to be running")
                    continue
                if self._is_app_closed(entry):
                    self.all_limits.apps.pop(key, None)
                    self.all_limits.manual_limit_baseline.pop(key, None)
                    # A closed app must not linger as an exemption: its id/cgroup can be
                    # reused by a different workload that should be eligible again.
                    self.all_limits.remove_exclusion(key)
                    closed.append(entry)

        for entry in closed:
            logger.info(
                f"Reaper: app '{entry.app_name}' ({entry.public_app_id}) closed; "
                f"restoring its {entry.source} limit")
            self._restore_entry(entry, notify=True)

        self._notify_long_frozen()

    def _notify_long_frozen(self) -> None:
        """Remind the UI, once per freeze, that an app has stayed frozen too long.

        Freeze has no timeout of its own (the user may mean it), but a forgotten
        freeze silently starves the app and times out its network peers.
        """
        after = float(getattr(self.config, "freeze_notify_after", 0) or 0)
        if after <= 0:
            return
        now = time.time()
        due = []
        with self.all_limits.lock:
            for entry in self.all_limits.apps.values():
                if entry.frozen and not entry.freeze_notified and now - entry.frozen_at >= after:
                    entry.freeze_notified = True
                    due.append((entry.public_app_id, entry.app_name))
        for app_id, app_name in due:
            logger.warning(f"App {app_name} ({app_id}) has been frozen for over {after:.0f}s")
            app_utils.callback_manager.send_callback_notification({
                'app_id': app_id,
                'app_name': app_name,
                'status': 'app_frozen_long',
                'purpose': "notify"
            }, False)

    def cancel_relaunch_by_app_id(self, app_id: str) -> bool:
        """Remove queue items for the given app_id and terminate the associated process."""
        def condition(item):
            data, _ = item
            return data.get('app_id') == app_id

        removed_items = self.app_priority_queue.remove_if(condition)
        killed = False
        for item in removed_items:
            data, _ = item
            pid = data.get('pid')
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    pass

        return killed

    def _get_limit_rate_bounds(self, priority: str) -> Dict[str, Dict[str, float]]:
        priority = (priority or "undefined").lower()
        cpu_bounds = {
            "high": {"min": 0.10, "max": 0.90},
            "medium": {"min": 0.05, "max": 0.70},
            "low": {"min": 0.01, "max": 0.50},
            "undefined": {"min": 0.01, "max": 0.40},
        }
        mem_bounds = {
            "high": {"min": 0.10, "max": 0.60},
            "medium": {"min": 0.05, "max": 0.40},
            "low": {"min": 0.01, "max": 0.30},
            "undefined": {"min": 0.01, "max": 0.30},
        }
        return {
            "cpu": cpu_bounds.get(priority, cpu_bounds["undefined"]),
            "memory": mem_bounds.get(priority, mem_bounds["undefined"]),
        }

    @staticmethod
    def _clamp_rate(value: Optional[float], low: float, high: float) -> Optional[float]:
        if value is None:
            return None
        return max(low, min(high, float(value)))

    def _get_policy_rate_options(self, resource: str, priority: str, current_rate: Optional[float]) -> list[float]:
        """Return sorted percentage options derived from yaml limit_policy rates."""
        policy = (self.config.limit_policy or {}).get(resource, {}) if hasattr(self.config, 'limit_policy') else {}
        rate_cfg = policy.get("rate", {}) if isinstance(policy, dict) else {}
        values: list[float] = []

        if isinstance(rate_cfg, dict):
            for raw in rate_cfg.values():
                try:
                    v = float(raw)
                    if v > 0:
                        values.append(v)
                except (TypeError, ValueError):
                    continue

            p_val = rate_cfg.get((priority or "undefined").lower())
            try:
                if p_val is not None:
                    pv = float(p_val)
                    if pv > 0:
                        values.append(pv)
            except (TypeError, ValueError):
                pass

        if current_rate is not None:
            values.append(float(current_rate))

        if not values:
            return []

        unique_sorted = sorted({round(v * 100, 1) for v in values if v > 0})
        return unique_sorted

    @staticmethod
    def _is_io_limit_reached(io_read_mb: float, io_write_mb: float, io_read_iops: float, io_write_iops: float) -> bool:
        return (
            (io_read_mb + io_write_mb) >= IO_LIMIT_MBPS_THRESHOLD or
            (io_read_iops + io_write_iops) >= IO_LIMIT_IOPS_THRESHOLD
        )

    def _load_app_limit_overrides(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Load per-app manually saved limit overrides from the DB."""
        try:
            from db.DatabaseModel import AIAppPriority
            record = AIAppPriority.query().filter(AIAppPriority.app_id == app_id).first()
            if record and record.limit_overrides_json:
                return json.loads(record.limit_overrides_json)
        except Exception as e:
            logger.debug(f"Could not load per-app limit overrides for '{app_id}': {e}")
        return None

    def get_resource_limit_profile(self, app_id: str, app_name: str, priority: str = "undefined") -> Dict[str, Any]:
        priority = (priority or "undefined").lower()
        app_overrides = self._load_app_limit_overrides(app_id)
        rates = self.get_limited_rates(priority, limit_overrides=app_overrides)
        bounds = self._get_limit_rate_bounds(priority)

        cpu_rate = rates.get("cpu_rate")
        mem_rate = rates.get("mem_rate")
        saved_disk_rate = (
            app_overrides.get("disk_io", {}).get("rate")
            if isinstance(app_overrides, dict) and isinstance(app_overrides.get("disk_io"), dict)
            else None
        )
        io_rate = rates.get("disk_io_rate") or (saved_disk_rate if isinstance(saved_disk_rate, dict) else {})
        cpu_options = self._get_policy_rate_options("cpu", priority, cpu_rate)
        mem_options = self._get_policy_rate_options("memory", priority, mem_rate)

        usage = app_utils.get_app_resource_usage(app_id, app_name) or {}
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        io_read_iops = usage.get("io_read_iops", 0)
        io_write_iops = usage.get("io_write_iops", 0)
        is_io_limit = self._is_io_limit_reached(io_read_mb, io_write_mb, io_read_iops, io_write_iops)

        process_names = app_utils._get_app_process_names(app_id=app_id, app_name=app_name) or []
        cgroup_paths = usage.get("cgroup_paths") or ([usage.get("cgroup_path")] if usage.get("cgroup_path") else [])
        cgroup_ids = [os.path.basename(path) for path in cgroup_paths if path]
        target_processes = []
        seen_pids = set()
        for pid in usage.get("pids", []) or []:
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                continue
            if pid_i in seen_pids:
                continue
            seen_pids.add(pid_i)
            try:
                pname = psutil.Process(pid_i).name()
            except Exception:
                pname = ""
            target_processes.append({
                "pid": pid_i,
                "name": pname,
            })
        target_processes.sort(key=lambda x: x["pid"])

        disk_policy = (self.config.limit_policy or {}).get('disk_io', {}) if hasattr(self.config, 'limit_policy') else {}
        disk_rates_cfg = disk_policy.get('rate', {}) if isinstance(disk_policy, dict) else {}
        cfg_disk_rate = (
            disk_rates_cfg.get(priority)
            or disk_rates_cfg.get('undefined')
            or {}
        )

        def _io_item(key: str, v: Any) -> Dict[str, int]:
            cfg_default = cfg_disk_rate.get(key) if isinstance(cfg_disk_rate, dict) else None
            if v is not None:
                value = max(1, int(v))
            elif cfg_default is not None:
                value = max(1, int(cfg_default))
            else:
                value = 1
            return {"value": value, "min": 1, "max": value}

        has_app_io_override = bool(
            isinstance(app_overrides, dict)
            and isinstance(app_overrides.get("disk_io"), dict)
            and app_overrides["disk_io"].get("enabled", False)
        )
        disk_io_enabled = has_app_io_override or (bool(io_rate) and is_io_limit)

        return {
            "cpu": {
                "enabled": cpu_rate is not None,
                "value": round((cpu_rate or 0) * 100, 2),
                "min": round(bounds["cpu"]["min"] * 100, 2),
                "max": round(bounds["cpu"]["max"] * 100, 2),
                "options": cpu_options,
            },
            "memory": {
                "enabled": mem_rate is not None,
                "value": round((mem_rate or 0) * 100, 2),
                "min": round(bounds["memory"]["min"] * 100, 2),
                "max": round(bounds["memory"]["max"] * 100, 2),
                "options": mem_options,
            },
            "disk_io": {
                "enabled": disk_io_enabled,
                "is_io_limit": is_io_limit,
                "write": _io_item("write", io_rate.get("write")),
                "read": _io_item("read", io_rate.get("read")),
                "write_iops": _io_item("write_iops", io_rate.get("write_iops")),
                "read_iops": _io_item("read_iops", io_rate.get("read_iops")),
            },
            "process_names": process_names,
            "cgroup_ids": sorted(set(cgroup_ids)),
            "target_processes": target_processes,
        }

    def get_limited_rates(
            self,
            priority: str,
            limit_overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Union[float, Dict[str, int], None]]:
        """
        Return all enabled resource limit configurations for the given priority.
        :return:
            {
                "cpu_rate": float or None,
                "mem_rate": float or None,
                "disk_io_rate": {"write": x, "read": y} or None
            }
        """
        priority = priority.lower()
        result = {
            "cpu_rate": None,
            "mem_rate": None,
            "disk_io_rate": None
        }

        if not hasattr(self.config, 'limit_policy'):
            return result

        bounds = self._get_limit_rate_bounds(priority)
        overrides = limit_overrides or {}

        limit_policy_cfg = self.config.limit_policy or {}

        cpu_cfg = limit_policy_cfg.get('cpu', {})
        cpu_rates = cpu_cfg.get('rate', {})
        cpu_ovr = overrides.get("cpu", {}) if isinstance(overrides.get("cpu", {}), dict) else {}
        cpu_enabled = cpu_ovr.get("enabled", cpu_cfg.get('enabled', False))
        cpu_rate = cpu_ovr.get("rate", cpu_rates.get(priority))
        if cpu_enabled and cpu_rate is not None:
            result['cpu_rate'] = self._clamp_rate(cpu_rate, bounds["cpu"]["min"], bounds["cpu"]["max"])

        mem_cfg = limit_policy_cfg.get('memory', {})
        mem_rates = mem_cfg.get('rate', {})
        mem_ovr = overrides.get("memory", {}) if isinstance(overrides.get("memory", {}), dict) else {}
        mem_enabled = mem_ovr.get("enabled", mem_cfg.get('enabled', False))
        mem_rate = mem_ovr.get("rate", mem_rates.get(priority))
        if mem_enabled and mem_rate is not None:
            result['mem_rate'] = self._clamp_rate(mem_rate, bounds["memory"]["min"], bounds["memory"]["max"])

        disk_cfg = limit_policy_cfg.get('disk_io', {})
        disk_rates = disk_cfg.get('rate', {})
        default_disk_rate = disk_rates.get(priority)
        disk_ovr = overrides.get("disk_io", {}) if isinstance(overrides.get("disk_io", {}), dict) else {}
        disk_enabled = disk_ovr.get("enabled", disk_cfg.get('enabled', False))
        disk_rate = disk_ovr.get("rate", default_disk_rate)
        if disk_enabled and isinstance(disk_rate, dict):
            def _to_pos_int(name: str, fallback: int) -> int:
                raw = disk_rate.get(name, fallback)
                try:
                    return max(1, int(float(raw)))
                except (TypeError, ValueError):
                    return max(1, int(fallback))

            default_write = default_disk_rate.get("write", 1) if default_disk_rate else 1
            default_read = default_disk_rate.get("read", 1) if default_disk_rate else 1
            default_wiops = default_disk_rate.get("write_iops", 1) if default_disk_rate else 1
            default_riops = default_disk_rate.get("read_iops", 1) if default_disk_rate else 1
            result['disk_io_rate'] = {
                "write": _to_pos_int("write", default_write),
                "read": _to_pos_int("read", default_read),
                "write_iops": _to_pos_int("write_iops", default_wiops),
                "read_iops": _to_pos_int("read_iops", default_riops),
            }

        logger.info(f"Priority '{priority}' limit rates: {result}")
        return result

    def set_resource_limit(
            self,
            app_id: str,
            app_name: str,
            priority: str = None,
            limit_overrides: Optional[Dict[str, Any]] = None,
            target_cgroups: Optional[list[str]] = None,
    ) -> bool:
        """Set resource limits for an application (balanced policy).

        Three phases, and the ordering is the point.  Installing the limit needs
        ``operation_lock`` (ownership checks and cgroup writes must not
        interleave with the pressure loop's own appliers), while pushing the
        app down to its new memory.high must *not* hold it -- a single reclaim
        can block for seconds, and the auto-limit path takes the same lock.

        The disk-IO cap goes on *last*, after the reclaim, whenever the same
        request asks for both.  Reclaim writes the app's anon pages to swap, and
        swap bios carry the owning memcg's blkcg, so an ``io.max`` installed
        first throttles the very reclaim the memory cap was asking for, turning
        seconds of work into minutes.  This is the manual-path counterpart of
        :meth:`_clear_io_cap_before_sys_limit` -- but it defers rather than
        lifts, because here the io cap is the user's own request and must end up
        applied, not dropped.
        """
        reclaim_plan: "list[tuple[str, int]]" = []
        io_plan: "list[dict]" = []
        result = self._install_resource_limit(
            app_id, app_name, priority=priority, limit_overrides=limit_overrides,
            target_cgroups=target_cgroups, reclaim_plan=reclaim_plan, io_plan=io_plan,
        )
        self._reclaim_to_manual_targets(reclaim_plan)
        for deferred in io_plan:
            self._apply_deferred_io_cap(deferred)
        return result

    @_serialized_limit_operation
    def _apply_deferred_io_cap(self, deferred: dict) -> None:
        """Install the io cap held back by :meth:`set_resource_limit`.

        Takes ``operation_lock`` again because it writes cgroup files and
        mutates the registry entry, both of which the pressure loop also
        touches.  The entry was recorded with ``io_limited=False``, so this is
        the write that makes the accounting true; if it fails the entry keeps
        saying False, which is correct -- there is then no io cap to restore.
        """
        key = deferred["key"]
        with self.all_limits.lock:
            entry = self.all_limits.apps.get(key)
        if entry is None:
            # Restored or reaped while the reclaim ran.  Applying the cap now
            # would leave an io.max with no registry entry to restore it.
            logger.info(
                "[io-defer] %s went away during reclaim; dropping its deferred io cap",
                key)
            return

        limits = deferred["limits"]
        app_name = deferred["app_name"]
        ok = self.io_ctl.set_disk_io_throttle(key, limits=limits)
        for extra_id in deferred["extra_ids"]:
            self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)
        logger.info(
            "[io-defer] %s disk IO cap applied after reclaim: ok=%s", app_name, ok)

        with self.all_limits.lock:
            entry = self.all_limits.apps.get(key)
            if entry is None:
                return
            entry.limit_parts['io_limited'] = bool(ok)
            protection_id = entry.protection_id
        _emit_control_events(
            "APPLIED" if ok else "FAILED", {'io_limited': True},
            app_id=deferred["app_id"], app_name=app_name,
            protection_id=protection_id,
            attributes={"app_name": app_name, "priority": deferred["priority"],
                        "limit_rates": deferred["limit_rates"],
                        "limit_overrides": deferred["limit_overrides"],
                        "parts": {'io_limited': bool(ok)},
                        "cgroups": [key] + list(deferred["extra_ids"])},
            control_mode="manual",
        )

    def _reclaim_to_manual_targets(self, plan: "list[tuple[str, int]]") -> None:
        """Push each freshly-limited cgroup down to its own memory.high.

        Best-effort by design: memory.high is already installed, so a reclaim
        that fails (full swap, cgroup gone) must not turn a working limit into
        a failed one.  Runs with no lock held -- see :meth:`set_resource_limit`.
        """
        for cgroup_id, target_mb in plan:
            self._mark_reclaim_noise()
            try:
                outcome = self.control_manager.reclaim_memory_to_target(
                    cgroup_id, int(target_mb) * _MB,
                    growth_gate=lambda: self._claim_swap_growth_slot(
                        time.time(), urgent=True),
                )
                logger.info(
                    "[manual-reclaim] %s target=%d MB reclaimed=%d MB "
                    "elapsed=%.0f ms ok=%s reason=%s",
                    cgroup_id, int(target_mb),
                    outcome.reclaimed_bytes // _MB,
                    outcome.elapsed_ms, outcome.ok, outcome.reason,
                )
            except Exception as exc:
                logger.warning(
                    f"[manual-reclaim] {cgroup_id} failed after limit applied: {exc}"
                )
            finally:
                # Re-arm from the end too: this call is synchronous and can run
                # for seconds, which would eat into the window it opened.
                self._mark_reclaim_noise()

    @_serialized_limit_operation
    def _install_resource_limit(
            self,
            app_id: str,
            app_name: str,
            priority: str = None,
            limit_overrides: Optional[Dict[str, Any]] = None,
            target_cgroups: Optional[list[str]] = None,
            reclaim_plan: "Optional[list[tuple[str, int]]]" = None,
            io_plan: "Optional[list[dict]]" = None,
    ) -> bool:
        """Install the manual limit and record it in the registry.

        ``target_cgroups`` optionally restricts the limit to a subset of the
        app's currently-running instances (by cgroup basename).  When omitted
        or empty, every cgroup the app currently occupies is limited.

        ``reclaim_plan`` and ``io_plan`` are *output* parameters: the
        ``(cgroup, target_mb)`` pairs the caller should reclaim once this
        method's lock is released, and the disk-IO cap the caller should install
        once that reclaim is done (see :meth:`set_resource_limit` for why the
        order matters).  They are filled rather than returned so that the method
        keeps a single return contract (``True`` / ``False`` /
        ``{"skipped": reason}``) across all of its early exits.
        """
        priority = priority or "undefined"
        if isinstance(limit_overrides, dict):
            try:
                from db.DatabaseModel import AIAppPriority
                AIAppPriority.update_record(id=app_id, limit_overrides_json=json.dumps(limit_overrides))
            except Exception as e:
                logger.warning(f"Failed to persist per-app limit overrides for '{app_id}': {e}")
        limit_rates = self.get_limited_rates(priority, limit_overrides=limit_overrides)
        if not limit_rates:
            logger.error(f"No limit rates defined for priority: {priority}")
            return False

        usage = app_utils.get_app_resource_usage(app_id, app_name)
        if usage is None:
            logger.warning(f"No resource usage data for {app_name}, using empty defaults")
            usage = {}

        all_cgroup_paths = usage.get("cgroup_paths") or (
            [usage["cgroup_path"]] if usage.get("cgroup_path") else []
        )
        effective_app_ids = [os.path.basename(p) for p in all_cgroup_paths if p]
        if not effective_app_ids:
            logger.warning(f"Could not determine cgroup path for {app_name} (ID: {app_id})")
            return False

        # Restrict to the user-selected instances (by cgroup basename) when
        # provided. The dashboard defaults to "all instances", so target_cgroups
        # is only a subset when the user unticked some rows.
        if target_cgroups:
            wanted = {os.path.basename(str(c)) for c in target_cgroups if str(c).strip()}
            selected = [e for e in effective_app_ids if e in wanted]
            if not selected:
                reason = (
                    f"None of the selected instances of {app_name} are still running; "
                    "nothing to limit. Please refresh and try again."
                )
                logger.warning(reason)
                return {"skipped": reason}
            effective_app_ids = selected

        effective_app_id = effective_app_ids[0]   # primary (lexicographically smallest cgroup)
        extra_effective_ids = effective_app_ids[1:]

        with self.all_limits.lock:
            auto_limited_cgroups = [
                cgroup_id for cgroup_id in effective_app_ids
                if (found := self.all_limits.by_any_id(cgroup_id, source="auto")) is not None
                and (entry := found[1]) is not None
                and entry.source == "auto"
            ]
        if auto_limited_cgroups:
            cgroups = ", ".join(auto_limited_cgroups)
            reason = (
                f"{app_name} is already auto-limited in cgroup(s): {cgroups}. "
                "Restore the auto limit or use Take Control before applying a manual limit."
            )
            logger.warning(reason)
            return {"skipped": reason}

        # Snapshot only PIDs that belong to the selected effective cgroups.
        # This drives per-row limit-status rendering in the dashboard; if we
        # keep all app PIDs here, unselected instances can be incorrectly
        # shown as Limited.
        selected_effective_set = set([effective_app_id] + list(extra_effective_ids))
        selected_scope_pids: set[int] = set()
        for raw_pid in (usage.get('pids') or []):
            try:
                pid_i = int(raw_pid)
            except (TypeError, ValueError):
                continue
            cg_path = app_utils.get_cgroup_path_by_pid(pid_i)
            cg_leaf = os.path.basename((cg_path or '').rstrip('/')) if cg_path else ''
            if cg_leaf in selected_effective_set:
                selected_scope_pids.add(pid_i)

        raw_cpu_percent = usage.get("cpu_percent", 0)
        mem_current = usage.get("mem_current", 0) + usage.get("mem_swap_current", 0)  # RSS + swap = true working set
        io_read_mb = usage.get("io_read_mb", 0)
        io_write_mb = usage.get("io_write_mb", 0)
        io_read_iops = usage.get("io_read_iops", 0)
        io_write_iops = usage.get("io_write_iops", 0)

        baseline = self.all_limits.manual_limit_baseline.get(effective_app_id, {})
        if baseline:
            raw_cpu_percent = max(raw_cpu_percent, baseline.get("cpu_percent", 0))
            mem_current = max(mem_current, baseline.get("mem_total", 0))
            io_read_mb = max(io_read_mb, baseline.get("io_read_mb", 0))
            io_write_mb = max(io_write_mb, baseline.get("io_write_mb", 0))
            io_read_iops = max(io_read_iops, baseline.get("io_read_iops", 0))
            io_write_iops = max(io_write_iops, baseline.get("io_write_iops", 0))
            logger.debug(
                f"[peak-latch] {app_name}: CPU {usage.get('cpu_percent', 0):.1f}%→{raw_cpu_percent:.1f}% "
                f"Mem {usage.get('mem_current', 0) + usage.get('mem_swap_current', 0):.1f}→{mem_current:.1f} MB"
            )

        cpu_usage_percent = raw_cpu_percent if raw_cpu_percent >= 2 else 0

        is_io_limit = self._is_io_limit_reached(io_read_mb, io_write_mb, io_read_iops, io_write_iops)
        force_user_io_limit = bool(
            isinstance(limit_overrides, dict) and isinstance(limit_overrides.get("disk_io"), dict)
        )

        cpu_quota = (max(1, int(cpu_usage_percent * limit_rates["cpu_rate"]))
                     if (limit_rates.get("cpu_rate") and cpu_usage_percent > 0) else None)
        mem_high = (max(1, int(mem_current * limit_rates["mem_rate"]))
                    if (limit_rates.get("mem_rate") and mem_current > 0) else None)
        io_limits = limit_rates.get("disk_io_rate", {})
        should_apply_io_limit = bool(io_limits) and (force_user_io_limit or is_io_limit)

        logger.debug(
            f"[set_resource_limit] {app_name}: cpu_usage_percent={cpu_usage_percent} "
            f"* cpu_rate={limit_rates.get('cpu_rate')} -> cpu_quota={cpu_quota}; "
            f"mem_current={mem_current}MB * mem_rate={limit_rates.get('mem_rate')} -> mem_high={mem_high}; "
            f"is_io_limit={is_io_limit} force_user_io_limit={force_user_io_limit} "
            f"should_apply_io_limit={should_apply_io_limit}"
        )

        no_cpu_limit = cpu_quota is None
        no_mem_limit = mem_high is None
        no_io_limit = not should_apply_io_limit
        if no_cpu_limit and no_mem_limit and no_io_limit:
            reason = (
                f"Unable to detect resource usage for {app_name}; skipping limit. Please select another application."
                if not usage
                else f"{app_name} has negligible resource usage (CPU<10%, memory≈0, IO<100 MB/s and <1000 IOPS); no limit needed. Please select another application."
            )
            logger.warning(reason)
            return {"skipped": reason}

        logger.debug(f"Calculated limits - CPU: {cpu_quota if cpu_quota else 'No Limit'}, "
                     f"Memory: {mem_high if mem_high else 'No Limit'}, is_io_limit: {is_io_limit}, "
                     f"force_user_io_limit: {force_user_io_limit}, should_apply_io_limit: {should_apply_io_limit}")

        resource_limited = False
        io_limited = False
        attempted_parts = {
            'cpu_mem_limited': (cpu_quota is not None or mem_high is not None) and self.is_running,
            'io_limited': should_apply_io_limit and bool(io_limits) and self.is_running,
        }

        per_cg_mem = usage.get('per_cgroup_mem', {})
        per_cg_cpu_delta = usage.get('per_cgroup_cpu_delta', {})
        limited_cgroups = []
        mem_high_by_cgroup = {effective_app_id: mem_high}

        if baseline:
            baseline_pcg_mem = baseline.get("per_cgroup_mem", {})
            baseline_pcg_cpu = baseline.get("per_cgroup_cpu_delta", {})
            if baseline_pcg_mem:
                per_cg_mem = {
                    cg: max(per_cg_mem.get(cg, 0), baseline_pcg_mem.get(cg, 0))
                    for cg in set(per_cg_mem) | set(baseline_pcg_mem)
                }
            if baseline_pcg_cpu:
                per_cg_cpu_delta = {
                    cg: max(per_cg_cpu_delta.get(cg, 0), baseline_pcg_cpu.get(cg, 0))
                    for cg in set(per_cg_cpu_delta) | set(baseline_pcg_cpu)
                }

        if (cpu_quota is not None or mem_high is not None) and self.is_running:
            if extra_effective_ids:
                all_ids = [effective_app_id] + extra_effective_ids
                mem_dist = _split_proportionally(mem_high, all_ids, per_cg_mem)
                mem_high_by_cgroup = mem_dist
                cpu_dist = _split_proportionally(cpu_quota, all_ids, per_cg_cpu_delta)
                primary_ok = self.control_manager.adjust_resources(
                    effective_app_id, "critical",
                    cpu_quota=cpu_dist.get(effective_app_id, cpu_quota),
                    mem_high=mem_dist.get(effective_app_id, mem_high),
                )
                if primary_ok:
                    resource_limited = True
                    limited_cgroups.append(effective_app_id)
                    self.control_manager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")
                for extra_id in extra_effective_ids:
                    ok = self.control_manager.adjust_resources(
                        extra_id, "critical",
                        cpu_quota=cpu_dist.get(extra_id, cpu_quota),
                        mem_high=mem_dist.get(extra_id, mem_high),
                    )
                    if ok:
                        limited_cgroups.append(extra_id)
                    logger.info(
                        f"{'Successfully set' if ok else 'Failed to set'} "
                        f"CPU/Memory limits for extra cgroup {extra_id}"
                    )
            else:
                if self.control_manager.adjust_resources(
                        effective_app_id, "critical",
                        cpu_quota=cpu_quota,
                        mem_high=mem_high
                ):
                    resource_limited = True
                    limited_cgroups.append(effective_app_id)
                    self.control_manager.set_limited_app_dominant(True)
                    logger.info(f"Successfully set CPU/Memory limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set CPU/Memory limits for {app_name} ({effective_app_id})")

        # Hold the io cap back when this same request also lands a memory cap
        # that something will have to be reclaimed down to: the reclaim writes
        # to swap over the device the io cap throttles.  Only when the cpu/mem
        # write actually succeeded -- otherwise there is no reclaim to protect
        # and deferring would just delay the user's io cap for nothing.
        defer_io = bool(
            should_apply_io_limit and io_limits and self.is_running
            and io_plan is not None and resource_limited
            and mem_high is not None and limited_cgroups
        )
        if should_apply_io_limit and io_limits and self.is_running:
            limits = {
                "default": {
                    "rbps": io_limits['read'] * 1024 ** 2,
                    "wbps": io_limits['write'] * 1024 ** 2,
                    "wiops": io_limits['write_iops'],
                    "riops": io_limits['read_iops']
                }
            }
            if defer_io:
                io_plan.append({
                    "key": effective_app_id, "extra_ids": list(extra_effective_ids),
                    "limits": limits, "app_id": app_id, "app_name": app_name,
                    "priority": priority, "limit_rates": limit_rates,
                    "limit_overrides": limit_overrides,
                })
                logger.info(
                    "[io-defer] %s (%s): holding the disk IO cap until the reclaim to "
                    "memory.high=%s MB finishes, so swap writeback is not throttled by it",
                    app_name, effective_app_id, mem_high)
            else:
                io_limited = self.io_ctl.set_disk_io_throttle(effective_app_id, limits=limits)
                if io_limited:
                    logger.info(f"Successfully set disk IO limits for {app_name} ({effective_app_id})")
                else:
                    logger.error(f"Failed to set disk IO limit for {app_name} ({effective_app_id})")
                for extra_id in extra_effective_ids:
                    self.io_ctl.set_disk_io_throttle(extra_id, limits=limits)

        with self.all_limits.lock:
            if resource_limited or io_limited:
                limited_at = time.time()
                self.all_limits.apps[effective_app_id] = LimitedApp(
                    public_app_id=app_id,
                    app_name=app_name,
                    source="manual",
                    limit_rates=limit_rates,
                    limit_parts={'cpu_mem_limited': resource_limited, 'io_limited': io_limited},
                    resource_parts={'cpu': cpu_quota is not None, 'memory': mem_high is not None},
                    state=None,
                    priority=(priority or "undefined"),
                    # Manual limits always come from the UI acting on a controlled app.
                    is_controlled=True,
                    cgroups=[effective_app_id] + list(extra_effective_ids),
                    pids=selected_scope_pids,
                    limited_at=limited_at,
                    protection_id=_protection_id("manual", app_id, effective_app_id, limited_at),
                )
                app_utils.update_app_status(app_id, "a_limited")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "a_limited",
                    'purpose': "app"
                }, False)
                self.all_limits.manual_limit_baseline[effective_app_id] = {
                    "cpu_percent": raw_cpu_percent,
                    "mem_total": mem_current,
                    "io_read_mb": io_read_mb,
                    "io_write_mb": io_write_mb,
                    "io_read_iops": io_read_iops,
                    "io_write_iops": io_write_iops,
                    "per_cgroup_mem": per_cg_mem,
                    "per_cgroup_cpu_delta": per_cg_cpu_delta,
                }
                # A user-owned manual limit must be off-limits to the pressure loop:
                # without this the app can still surface as a top consumer and be
                # re-limited as "auto", clobbering the user's rate and baseline. Cleared
                # again on manual restore (set_restore_resource) or when the app closes
                # (the reaper).
                self.all_limits.add_exclusion(
                    self.all_limits.apps[effective_app_id], reason="manual_limit")
                logger.info(f"Recorded resource limits for {app_name}")
                _emit_control_events(
                    "APPLIED", self.all_limits.apps[effective_app_id].limit_parts,
                    app_id=app_id, app_name=app_name,
                    protection_id=self.all_limits.apps[effective_app_id].protection_id,
                    # limit_rates carries the *actual* applied caps (same field the
                    # auto path emits) so the dashboard shows real numbers per
                    # resource; limit_overrides is the user's config intent, kept
                    # for audit but hidden from the human detail view.
                    attributes={"app_name": app_name, "priority": priority,
                                "limit_rates": limit_rates,
                                "limit_overrides": limit_overrides,
                                "parts": self.all_limits.apps[effective_app_id].limit_parts,
                                "resource_parts": {'cpu': cpu_quota is not None, 'memory': mem_high is not None},
                                "cgroups": [effective_app_id] + list(extra_effective_ids),
                                "pids": selected_scope_pids},
                    control_mode="manual",
                )
                _emit_control_events(
                    "FAILED",
                    # A deferred io cap has not been attempted yet, so it cannot
                    # have failed -- _apply_deferred_io_cap emits its own verdict.
                    {part: attempted and not (part == 'io_limited' and defer_io)
                     and not {'cpu_mem_limited': resource_limited, 'io_limited': io_limited}.get(part)
                     for part, attempted in attempted_parts.items()},
                    app_id=app_id, app_name=app_name,
                    protection_id=self.all_limits.apps[effective_app_id].protection_id,
                    attributes={"app_name": app_name, "priority": priority,
                                "resource_parts": {'cpu': cpu_quota is not None, 'memory': mem_high is not None},
                                "limit_overrides": limit_overrides},
                    control_mode="manual",
                )

        if resource_limited or io_limited:
            # Hand the reclaim targets to the caller instead of reclaiming here:
            # this runs under operation_lock, which the pressure loop's appliers
            # also take, and a reclaim can block for seconds.
            if mem_high is not None and reclaim_plan is not None:
                reclaim_plan.extend(
                    (cgroup_id, mem_high_by_cgroup[cgroup_id])
                    for cgroup_id in limited_cgroups
                    if mem_high_by_cgroup.get(cgroup_id) is not None
                )
            return True

        failed_at = time.time()
        _emit_control_events(
            "FAILED", attempted_parts, app_id=app_id, app_name=app_name,
            protection_id=_protection_id("manual", app_id, effective_app_id, failed_at),
            attributes={"app_name": app_name, "priority": priority,
                        "limit_overrides": limit_overrides},
            control_mode="manual",
        )
        logger.warning(f"No resource limits successfully applied for {app_name}")
        return False

    def _thaw_cgroups(self, cgroups: list) -> bool:
        """Thaw every cgroup in *cgroups*; True only if all are thawed (or gone)."""
        all_ok = True
        for cg in cgroups:
            ok, msg = self.control_manager.freeze_cgroup(cg, False)
            if not ok:
                logger.error(f"Failed to thaw cgroup {cg}: {msg}")
                all_ok = False
        return all_ok

    def set_freeze(self, app_id: str, freeze: bool) -> "tuple[bool, str]":
        """Freeze (or unfreeze, keeping the limits) a manually limited app.

        Manual limits only: freeze is the user's escalation when the ordinary
        limit cannot slow a GPU/NPU-bound app.  ``set_restore_resource`` lifts the
        limits and then thaws, so Restore always undoes both.
        """
        with self.all_limits.lock:
            found = self.all_limits.by_public_id(app_id, source="manual")
            if found is None:
                return False, "App has no manual limit; apply a limit first"
            key, entry = found
            cgroups = list(entry.cgroups) or [key]
            if entry.frozen == freeze:
                return True, "Already frozen" if freeze else "Not frozen"

        done = []
        for cg in cgroups:
            ok, msg = self.control_manager.freeze_cgroup(cg, freeze)
            if not ok:
                logger.error(f"{'Freeze' if freeze else 'Thaw'} failed for {app_id} ({cg}): {msg}")
                if freeze:
                    self._thaw_cgroups(done)   # don't leave a half-frozen app
                return False, msg
            done.append(cg)

        with self.all_limits.lock:
            found = self.all_limits.by_public_id(app_id, source="manual")
            if found is None:
                # Restored while we were writing: its thaw ran before our freeze landed.
                if freeze:
                    self._thaw_cgroups(done)
                return False, "Limit was restored while freezing"
            found[1].frozen = freeze
            found[1].frozen_at = time.time() if freeze else 0.0
            found[1].freeze_notified = False
        logger.info(f"{'Froze' if freeze else 'Thawed'} app {app_id} ({len(done)} cgroup(s))")
        return True, "Frozen" if freeze else "Unfrozen"

    def set_restore_resource(self, app_id: str) -> bool:
        """Restore resource limits for the given app_id (manual/UI path).

        Behaviour unchanged from the pre-registry-refactor version; the
        only additions are (1) locating the entry via the unified registry
        and (2) popping it under ``self.all_limits.lock`` so the reaper thread
        cannot restore the same app concurrently.  ``manual_limit_baseline``
        is intentionally left in place (peak latch survives a manual
        restore, as before).
        """
        with self.all_limits.lock:
            # Manual restore only ever targets manual limits — auto limits are
            # owned by the pressure loop's staged recovery (and the reaper on
            # close), never by an explicit user restore.
            found = self.all_limits.by_public_id(app_id, source="manual")
            if found is not None:
                effective_app_id, entry = found
                effective_app_ids = list(entry.cgroups) or [effective_app_id]
                app_name = entry.app_name
                limit_parts = entry.limit_parts
                protection_id = entry.protection_id
                resource_parts = entry.resource_parts
                priority = entry.priority
                was_frozen = entry.frozen
                self.all_limits.apps.pop(effective_app_id, None)
                # The user gave the app back; drop the "manual_limit" exemption so the
                # pressure loop may manage it again.
                self.all_limits.remove_exclusion(effective_app_id)
            else:
                # Compatibility path for callers that still pass an effective ID.
                effective_app_ids = [app_id]
                app_name, limit_parts = None, {}
                protection_id = ""
                resource_parts = {}
                priority = "undefined"
                was_frozen = False

        effective_app_id = effective_app_ids[0]
        extra_effective_ids = effective_app_ids[1:]
        restore_success = True
        restored_parts = {'cpu_mem_limited': False, 'io_limited': False}
        failed_parts = {'cpu_mem_limited': False, 'io_limited': False}
        try:
            logger.info(f"Restoring resources for app: {app_id}, name: {app_name}")

            if limit_parts.get('cpu_mem_limited', False):
                if not self.control_manager.adjust_resources(effective_app_id, "low"):
                    logger.error(f"Failed to restore CPU/Memory for {app_id} ({effective_app_id})")
                    restore_success = False
                    failed_parts['cpu_mem_limited'] = True
                else:
                    restored_parts['cpu_mem_limited'] = True
                for extra_id in extra_effective_ids:
                    self.control_manager.adjust_resources(extra_id, "low")

            if limit_parts.get('io_limited', False):
                if not self.io_ctl.restore_disk_io_throttle(effective_app_id):
                    logger.error(f"Failed to remove IO limits for {app_id} ({effective_app_id})")
                    restore_success = False
                    failed_parts['io_limited'] = True
                else:
                    restored_parts['io_limited'] = True
                for extra_id in extra_effective_ids:
                    self.io_ctl.restore_disk_io_throttle(extra_id)

            # Thaw last: limits are already lifted, so the app does not resume
            # straight into a memory.high / io.max stall.  A failed limit restore
            # must not leave the app frozen, so this runs regardless.
            if was_frozen and not self._thaw_cgroups(effective_app_ids):
                restore_success = False

            if restore_success:
                app_utils.update_app_status(app_id, "running")
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, False)
                logger.info(f"Resources restored for {app_id}")
            _emit_control_events(
                "RECOVERED", restored_parts, app_id=app_id, app_name=app_name or app_id,
                protection_id=protection_id,
                attributes={"app_name": app_name, "limit_parts": limit_parts,
                            "priority": priority, "resource_parts": resource_parts},
                control_mode="manual",
            )
            _emit_control_events(
                "FAILED", failed_parts, app_id=app_id, app_name=app_name or app_id,
                protection_id=protection_id,
                attributes={"app_name": app_name, "limit_parts": limit_parts,
                            "priority": priority, "resource_parts": resource_parts},
                control_mode="manual",
            )

            return restore_success
        except Exception as e:
            logger.error(f"Failed to restore resources for {app_id}: {str(e)}")
            return False
        finally:
            time.sleep(self.config.regular_update_sys_pressure_time)
            self.control_manager.set_limited_app_dominant(False)

    @staticmethod
    def _entry_control_view(entry: "LimitedApp") -> dict:
        """Derive the UI control contract (status + effective + auto detail) from an entry.

        Shared by ``get_limit_snapshot`` (controlled-app rows) and
        ``get_auto_limited_apps`` so both surfaces report the same shape.

        ``control_status`` is the coarse enum that drives interaction (tag colour +
        button gating): ``MANUAL_LIMITED`` / ``AUTO_LIMITED``.  The finer "partially
        restored" middle state is *not* an enum value -- it rides along under
        ``auto_detail.partial_parts`` for display only, since an auto limit that is
        half-relaxed is still auto-owned and its buttons stay locked.

        ``effective`` keeps the limit multi-dimensional on purpose (never collapsed to
        a single percent): CPU/memory travel together under one part, disk-IO is its
        own channel with the exact disk set it was written to (empty = every disk).
        """
        rates = entry.limit_rates or {}
        effective = {
            "cpu_mem": {
                "limited": bool(entry.limit_parts.get("cpu_mem_limited")),
                "cpu_rate": rates.get("cpu_rate"),
                "mem_rate": rates.get("mem_rate"),
            },
            "disk_io": {
                "limited": bool(entry.limit_parts.get("io_limited")),
                "disks": list(entry.limit_disks or []),
                "read_mb_s": (rates.get("disk_io_rate") or {}).get("read"),
                "write_mb_s": (rates.get("disk_io_rate") or {}).get("write"),
                "read_iops": (rates.get("disk_io_rate") or {}).get("read_iops"),
                "write_iops": (rates.get("disk_io_rate") or {}).get("write_iops"),
            },
        }
        auto_detail = None
        if entry.source == "auto":
            auto_detail = {
                "limit_reason": entry.limit_reason or "system_pressure",
                "pressure_level": entry.pressure_level or "critical",
                "partial_parts": dict(entry.partial_parts or {}),
            }
        return {
            "control_status": "AUTO_LIMITED" if entry.source == "auto" else "MANUAL_LIMITED",
            "effective": effective,
            "auto_detail": auto_detail,
        }

    def get_limit_snapshot(self, app_id: str) -> dict:
        """Return a lightweight runtime snapshot for a limited app.

        The snapshot is used by the dashboard to infer per-process limit scope
        without introducing a separate event-history subsystem.
        """
        with self.all_limits.lock:
            found = self.all_limits.by_public_id(app_id)
            if found is None:
                return {
                    "limited": False,
                    "source": None,
                    "control_status": "NORMAL",
                    "pids": [],
                    "cgroups": [],
                }

            effective_app_id, entry = found
            snapshot = {
                "limited": True,
                "effective_app_id": effective_app_id,
                "source": entry.source,
                "pids": sorted(int(pid) for pid in entry.pids),
                "cgroups": list(entry.cgroups),
                "limit_parts": dict(entry.limit_parts or {}),
                "limited_at": entry.limited_at or None,
                "adopted_from_auto": entry.adopted_from_auto,
                "frozen": entry.frozen,
                "frozen_at": entry.frozen_at or None,
            }
            snapshot.update(self._entry_control_view(entry))
            return snapshot

    def inspect_interrupted_limit(self, cgroups: list) -> dict:
        """Return the live kernel state for an interrupted control lifecycle."""
        resources = set()
        for cgroup in cgroups or []:
            result = self.control_manager.controller.inspect_cgroup_limits(cgroup)
            if not result.get("available"):
                return {"available": False, "resources": []}
            resources.update(result.get("resources") or [])
        return {"available": True, "resources": sorted(resources)}

    def get_auto_limited_apps(self) -> dict:
        """Every app the pressure loop currently holds a limit on, for the UI.

        Manual (UI-initiated) limits are excluded: they already show on the controlled
        app's own row.

        No restore deadline is reported. Auto restore is not a timer: pressure has to fall
        back to medium/low, hold for the stability window, and then the limits go in
        stages, so any "restores in N seconds" number would be fiction.

        The current levels ride along so the UI can paint them on first render without a
        second request; after that :meth:`_on_pressure_level_changed` keeps them fresh.
        """
        with self.all_limits.lock:
            rows = []
            for key, entry in self.all_limits.apps.items():
                if entry.source != "auto":
                    continue
                row = {
                    "app_id": entry.public_app_id,
                    "effective_app_id": key,
                    "app_name": entry.app_name,
                    "priority": entry.priority or "undefined",
                    "is_controlled": bool(entry.is_controlled),
                    "status": ("partially_restored"
                               if entry.state == "partially_restored" else "limited"),
                    "limit_reason": entry.limit_reason or "system_pressure",
                    "pressure_level": entry.pressure_level or "critical",
                    "limited_at": entry.limited_at or None,
                    "limit_parts": dict(entry.limit_parts or {}),
                    "cgroups": list(entry.cgroups or []),
                    "pids": sorted(int(pid) for pid in entry.pids),
                    "representative_pid": entry.representative_pid,
                }
                row.update(self._entry_control_view(entry))
                rows.append(row)

        # The registry keeps the PID(s) sampled when the limit was applied, but
        # those can exit while other processes remain in the limited scope.
        # Refresh the scope members outside the registry lock so the dashboard
        # can describe the actual cgroup currently under control.
        for row in rows:
            scope_processes = {}
            for cgroup in row["cgroups"]:
                processes = []
                for pid in app_utils.get_pids_in_cgroup(cgroup):
                    try:
                        process = psutil.Process(pid)
                        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                            continue
                        processes.append({
                            "pid": pid,
                            "process_name": process.name() or row["app_name"],
                            "cmdline": " ".join(process.cmdline()).strip(),
                        })
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                if row.get("representative_pid"):
                    representative_pid = int(row["representative_pid"])
                    processes.sort(key=lambda process: process["pid"] != representative_pid)
                scope_processes[cgroup] = processes
            row["scope_processes"] = scope_processes

        try:
            sys_level = self.control_manager.current_level or "low"
            disk_level = (self.control_manager.get_disk_io_stress() or {}).get("level") or "low"
        except Exception as exc:
            logger.error("Failed to read current pressure levels: %s", exc)
            sys_level, disk_level = "", ""

        return {
            "apps": rows,
            "sys_pressure_level": sys_level,
            "disk_pressure_level": disk_level,
        }

    def restore_auto_limited(self, app_id: str, exclude: bool = True) -> "tuple[bool, str]":
        """Restore an auto-limited app on the user's request.

        Unlike :meth:`set_restore_resource`, which only targets manual limits, this also
        takes the app out of the auto-limit candidate pool (*exclude*) -- otherwise the
        next critical tick would re-limit the app the user just freed.

        :returns: ``(ok, message)`` -- *message* names the failure for the REST layer.
        """
        with self.all_limits.lock:
            found = (self.all_limits.by_public_id(app_id, source="auto")
                     or self.all_limits.by_any_id(app_id, source="auto"))
            if found is None:
                return False, "No auto-limited app found for this id"
            key, entry = found

        restored = self._restore_entry(
            entry, notify=True, notify_status="auto_limit_restored_by_user")
        if not restored:
            return False, "Failed to restore resources for this app"

        with self.all_limits.lock:
            # Retain the entry until cgroup restoration succeeds. Otherwise a transient
            # write failure leaves a live throttle with no registry entry to retry.
            if self.all_limits.apps.get(key) is entry:
                self.all_limits.apps.pop(key, None)
                self.all_limits.manual_limit_baseline.pop(key, None)
            record = self.all_limits.add_exclusion(entry) if exclude else None

        if record is not None:
            logger.info(
                "User restored auto-limited app %r (%s); excluded from auto-limit as %s",
                entry.app_name, entry.public_app_id, record["key"],
            )

        # Mirrors the manual restore path: the app we were discounting PSI for is no
        # longer limited, so the dominant-app flag must not outlive it.
        self.control_manager.set_limited_app_dominant(False)
        return True, "Restored"

    def lock_to_manual(self, app_id: str) -> "tuple[bool, str]":
        """Take an auto-limited app over as a manual limit WITHOUT touching cgroup.

        The "safe handoff": the kernel cgroup caps stay exactly where the pressure
        engine left them (e.g. CPU=20%); only ownership flips from auto to manual.
        This eliminates the crash window a restore-then-relimit would open under
        sustained pressure -- the app never leaves its safe throttled water line.
        Afterwards the app is MANUAL_LIMITED and the UI's Limit/Restore/Edit buttons
        unlock, so the operator can act at leisure (restore, or edit to 40%, once the
        real fix has landed).

        No cgroup write, no override snapshot: Restore reads the flipped registry
        entry's ``limit_parts``/``cgroups`` and Edit reloads a fresh profile, so
        neither depends on a persisted template. Manual limits are process-lifetime
        anyway (lost on service restart), so lock-to-manual inherits that semantics.

        :returns: ``(ok, message)`` -- *message* names the failure for the REST layer.
        """
        with self.all_limits.lock:
            found = (self.all_limits.by_public_id(app_id, source="auto")
                     or self.all_limits.by_any_id(app_id, source="auto"))
            if found is None:
                return False, "No auto-limited app found for this id"
            _key, entry = found
            # A manual limit only lives on a *controlled* app: the UI surfaces manual
            # limits via the controlled-app row, and the restore/edit paths key off the
            # DB entry. Locking a genuinely uncontrolled instance to manual would drop it
            # out of both the auto list and the controlled list -- invisible while still
            # throttled. So refuse; the operator must Take Control (adopt) it first.
            #
            # But entry.is_controlled is stamped once, when the limit *fired*, from
            # the sampled process identity -- so an app auto-limited before its
            # process_names matched carries a stale False even though it now has a
            # DB row. Re-check the *current* control state: if it is controlled now,
            # reconcile the entry in place (the same re-tag adopt does) and proceed.
            # Only refuse when there is still no DB identity to keep it visible.
            if not entry.is_controlled:
                current_controlled, _ = app_utils.get_app_control_info(app_id)
                if not current_controlled:
                    return False, "Take this app under control first, then lock it to manual"
                entry.is_controlled = True
                entry.public_app_id = app_id
            _emit_auto_handoff_recovery(entry)
            # Flip ownership in place; the cgroup caps are deliberately untouched.
            entry.source = "manual"
            entry.state = None
            entry.adopted_from_auto = True
            entry.limited_at = time.time()
            entry.protection_id = _protection_id(
                "manual", entry.public_app_id, _key, entry.limited_at)
            # Keep it out of the auto candidate pool for this run of the service, so
            # the pressure loop never re-grabs an app the operator now owns.
            record = self.all_limits.add_exclusion(entry, reason="manual_limit")
            public_id = entry.public_app_id
            app_name = entry.app_name
            protection_id = entry.protection_id
            limit_parts = entry.limit_parts.copy()
            resource_parts = entry.resource_parts.copy()
            cgroups = list(entry.cgroups)
            priority = entry.priority

        app_utils.update_app_status(public_id, "a_limited")
        app_utils.callback_manager.send_callback_notification({
            'app_id': public_id,
            'app_name': app_name,
            'status': "locked_to_manual",
            'purpose': "notify",
        }, False)
        logger.info(
            "Locked auto-limited app %r (%s) to manual; cgroup untouched, excluded as %s",
            app_name, public_id, record["key"],
        )
        _emit_control_events(
            "APPLIED", limit_parts, app_id=public_id, app_name=app_name,
            protection_id=protection_id,
            attributes={"app_name": app_name, "priority": priority,
                        "resource_parts": resource_parts,
                        "cgroups": cgroups, "handoff": "auto"},
            control_mode="manual",
        )
        return True, "Locked to manual"

    def lock_all_auto_to_manual(self) -> int:
        """Batch lock-to-manual over every currently auto-limited app.

        This is the safe meaning of disabling the global passive switch: rather than
        releasing every cgroup at once -- which under high pressure would let the
        suppressed load stampede back and crush the box -- we hand sovereignty to the
        operator with zero release. Every *controlled* ``source=auto`` entry flips to
        ``manual`` with its caps intact; the operator then restores each at their own pace.

        Uncontrolled auto entries are skipped: they have no DB row to become a manual
        limit on (see :meth:`lock_to_manual`). With passive control now off, the staged
        restore path lifts them as pressure eases -- or the operator can Take Control of
        one first to keep its cap.

        :returns: number of apps converted.
        """
        with self.all_limits.lock:
            auto_ids = [e.public_app_id for e in self.all_limits.apps.values()
                        if e.source == "auto" and e.is_controlled]
        converted = 0
        for app_id in auto_ids:
            ok, _msg = self.lock_to_manual(app_id)
            if ok:
                converted += 1
        if converted:
            # The apps we were discounting PSI for are now operator-owned, not auto;
            # the dominant-app flag must not outlive the auto ownership.
            self.control_manager.set_limited_app_dominant(False)
            logger.info("Passive control disabled: locked %d auto-limited app(s) to manual", converted)
        return converted

    def adopt_auto_limit(self, effective_app_id: str, new_app_id: str,
                         new_app_name: str = "", priority: str = "") -> "tuple[bool, str]":
        """Adopt a running auto-limit into a newly-controlled app identity.

        When the operator takes an auto-limited but *unmanaged* process under control
        (the "Take Control" action), the live limit must follow it -- otherwise the new
        controlled row shows NORMAL while the kernel still throttles the process, and the
        old uncontrolled entry lingers as a duplicate row. So instead of releasing and
        re-applying (which would open a crash window), we re-tag the existing entry in
        place: mark it controlled and point its public id at the new DB app_id. The
        cgroup caps and the registry key (the cgroup) are untouched, so staged restore
        and a later manual Restore still target the right cgroup.

        Take Control also performs the manual handoff in the same step: the entry flips
        ``source: auto -> manual`` and is added to the exclusion list (exactly what
        :meth:`lock_to_manual` does). So the app lands as a single MANUAL_LIMITED managed
        row with Limit/Restore/Edit already unlocked -- the operator does *not* need a
        second Lock to Manual click, and the pressure loop never re-grabs it. The cgroup
        caps are carried over untouched, so there is no release / crash window.

        :returns: ``(ok, message)`` -- *message* names the failure for the REST layer.
        """
        with self.all_limits.lock:
            found = (self.all_limits.apps.get(effective_app_id)
                     and (effective_app_id, self.all_limits.apps[effective_app_id])) \
                or self.all_limits.by_any_id(effective_app_id, source="auto")
            if found is None:
                return False, "No auto-limited entry found for this instance"
            key, entry = found
            if entry.source != "auto":
                return False, "This instance is no longer auto-limited"
            entry.is_controlled = True
            entry.public_app_id = new_app_id
            if new_app_name:
                entry.app_name = new_app_name
            if priority:
                entry.priority = priority
            _emit_auto_handoff_recovery(entry)
            # Hand ownership to manual in the same atomic step (see docstring): flip the
            # source and exclude it from the auto candidate pool. add_exclusion keys off
            # is_controlled/public_app_id, so it must run after the re-tag above.
            entry.source = "manual"
            entry.state = None
            entry.adopted_from_auto = True
            entry.limited_at = time.time()
            entry.protection_id = _protection_id("manual", new_app_id, key, entry.limited_at)
            self.all_limits.add_exclusion(entry, reason="manual_limit")
            protection_id = entry.protection_id
            limit_parts = entry.limit_parts.copy()
            resource_parts = entry.resource_parts.copy()
            cgroups = list(entry.cgroups)
            entry_priority = entry.priority

        app_utils.update_app_status(new_app_id, "a_limited")
        app_utils.callback_manager.send_callback_notification({
            'app_id': new_app_id,
            'app_name': new_app_name or entry.app_name,
            'status': "a_limited",
            'purpose': "app",
        }, False)
        logger.info(
            "Adopted auto-limit %r into controlled app %s as a manual limit; "
            "cgroup and key untouched, excluded from auto",
            key, new_app_id,
        )
        _emit_control_events(
            "APPLIED", limit_parts, app_id=new_app_id,
            app_name=new_app_name or entry.app_name, protection_id=protection_id,
            attributes={"app_name": new_app_name or entry.app_name,
                        "priority": entry_priority,
                        "resource_parts": resource_parts, "cgroups": cgroups,
                        "handoff": "auto"},
            control_mode="manual",
        )
        return True, "Adopted"

    def get_auto_limit_exclusions(self) -> list:
        """Apps the user has taken out of the auto-limit candidate pool."""
        with self.all_limits.lock:
            return self.all_limits.list_exclusions()

    def remove_auto_limit_exclusion(self, ident: str) -> bool:
        """Put an excluded app back into the auto-limit candidate pool."""
        with self.all_limits.lock:
            record = self.all_limits.remove_exclusion(ident)
        if record is None:
            return False
        logger.info("Auto-limit exclusion removed for %r (%s); app is a candidate again",
                    record.get("app_name"), record.get("key"))
        return True

    def _release_swap_on_exit(self) -> None:
        """Give back the swapfiles we added, but only the ones held at zero.

        Shutdown undoes what smartune did -- same principle as restoring limits
        and clearing network rules just above.  Swap is the one thing where
        "undo it" can cost more than leaving it: swapoff faults every page of
        the file back into RAM, so handing back a file that is actually holding
        pages would be a multi-gigabyte fault storm on the way out the door, on
        a machine nobody is left watching.  A file at zero used costs nothing,
        so take those and leave the rest to the next run's idle path.

        Never raises: a failure here must not stop the service from exiting.
        """
        try:
            self.control_manager.release_swap(dry_run=False, only_unused=True)
        except Exception as exc:
            logger.warning(f"[swap] shutdown release failed: {exc}")

    def shutdown(self):
        """
        Stop the service thread, wait for it to finish, and ensure all queued tasks are processed.
        """
        logger.info("Service is stopping.")
        if not self.is_running:
            logger.debug("Service is already stopped; no action needed")
            return
        self.is_running = False

        self.restore_all_limited_apps_resources()
        self.network_controller.clear_network_rules_on_exit()
        self._release_swap_on_exit()
        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=1)
        if hasattr(self, "handle_thread"):
            self.handle_thread.join(timeout=1)
        if hasattr(self, "app_intercept_thread"):
            self.app_intercept_thread.join(timeout=1)
        logger.info("Service stopped; all threads have exited")
