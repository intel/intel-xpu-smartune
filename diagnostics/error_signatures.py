# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Error signatures: track error logs that no rule recognises.
#
# Each error line is reduced to a template (numbers, ids, paths replaced) and fingerprinted.
# NEW: a fingerprint never seen on this host (recorded as evidence, no alert). BURST: one far
# above its own long-run rate, re-emitted every _BURST_HEARTBEAT_SECONDS while it lasts.
# Decisions use event time, so a rescan of the same records makes the same choices.

import collections
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field

from utils.logger import get_logger

logger = get_logger(__name__)

NEW = "LOG_ERROR_SIGNATURE_NEW"
BURST = "LOG_ERROR_SIGNATURE_BURST"

_BURST_WINDOW_SECONDS = 300
_BURST_MIN_COUNT = 10
_BURST_RATIO = 5.0
_BURST_HEARTBEAT_SECONDS = 300
# Minimum age for the long-run rate, so early occurrences cannot make a storm look normal.
_BASELINE_MIN_AGE_SECONDS = 3600
_LEARNING_SECONDS = 3600
# Learning period of the first boot after the install.
_REBOOT_LEARNING_SECONDS = 600
_NEW_PER_HOUR = 10
# The burst window is counted in buckets, so memory per template is bounded.
_BUCKET_SECONDS = 10
_WINDOW_BUCKETS = _BURST_WINDOW_SECONDS // _BUCKET_SECONDS
_MAX_TRACKED = 5000
_RETENTION_SECONDS = 30 * 24 * 3600
_PRUNE_INTERVAL_SECONDS = 3600
_TEMPLATE_MAX = 240
_SAMPLE_MAX = 500
# Recently observed record keys kept for replay (see SignatureTracker.observe).
_REPLAY_KEYS = 4096

# Order matters: the specific shapes first, bare numbers last.
_SUBSTITUTIONS = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<ID>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<HEX>"),
    (re.compile(r"\b[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\b", re.I), "<PCI>"),
    # MAC before IPv6, which would also match it.
    (re.compile(r"\b[0-9a-f]{2}([:-])[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}\b", re.I), "<MAC>"),
    # Hex digits and colons only, 2+ colons, and a hex letter or "::" -- so a
    # clock time (12:34:56) is left to the number rule.
    (re.compile(r"(?<![\w:.])(?=[0-9a-f:]{2,39}(?![\w:.]))(?=[0-9a-f]*:[0-9a-f]*:)"
                r"(?=[0-9a-f:]*(?:[a-f]|::))[0-9a-f:]{2,39}(?![\w:.])", re.I), "<IP>"),
    # Ids: 7+ hex characters with a digit (a short git or container id).
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.I), "<ID>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"(?:~|\.{1,2})?(?:/[\w.@+%-]+){2,}/?"), "<PATH>"),
    # An opening quote follows a non-word character, so the apostrophe in
    # "can't open 'x'" does not pair with the quote before x.
    (re.compile(r"(?<!\w)'[^'\n]{0,200}'(?!\w)"), "'<S>'"),
    (re.compile(r'"[^"\n]{0,200}"'), '"<S>"'),
    # A long number glued to a name (pid12345, task_88120) is an id; a short one
    # (sqlite3, nvme0n1, e1000e, c17) is part of the name and stays.
    (re.compile(r"(?<=[A-Za-z_])\d{5,}"), "<N>"),
    # A whole number only, never its tail: "c17" must not become "c1<N>".
    (re.compile(r"(?<![A-Za-z_<\d])\d+(?:[.:]\d+)*"), "<N>"),
    (re.compile(r"\s+"), " "),
)
# The exception line at the end of a traceback, e.g. "sqlite3.OperationalError: ...".
_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault)\b")


def normalize(message):
    """The template of an error message: its first line (plus, for a traceback,
    the exception line) with the variable parts replaced by placeholders."""
    lines = [line for line in (message or "").splitlines() if line.strip()]
    if not lines:
        return ""
    text = lines[0].strip()
    if len(lines) > 1 and "Traceback" in (message or ""):
        for line in reversed(lines):
            if _EXCEPTION_LINE_RE.match(line.strip()):
                text = f"{text} | {line.strip()}"
                break
    for pattern, placeholder in _SUBSTITUTIONS:
        text = pattern.sub(placeholder, text)
    return text.strip()[:_TEMPLATE_MAX]


def _current_boot():
    try:
        from diagnostics.system_info import read_boot_id

        return read_boot_id()
    except Exception:  # no boot id (a container): no boot-time learning
        return None


# Per-instance parts of systemd unit names (session-12.scope), applied in order.
_UNIT_INSTANCE_SUBSTITUTIONS = (
    (re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I), "<ID>"),
    # docker-<64 hex>.scope: an id of 8+ hex characters with a digit.
    (re.compile(r"(?<=[-@])(?=[0-9a-f]*\d)[0-9a-f]{8,}(?=[.@-]|$)", re.I), "<ID>"),
    # run-r<hex>.scope / run-u<hex>.service (systemd-run transient units)
    (re.compile(r"(?<=-)([ur])(?=[0-9a-f]*\d)[0-9a-f]{8,}(?=[.@-]|$)", re.I), r"\1<ID>"),
    # session-12.scope, run-u345.service, app-gnome-foo-4711.scope, user-1000.slice
    (re.compile(r"(?<=[-@])([ur]?)\d+(?=[.@-]|$)"), r"\1<N>"),
)


def normalize_unit(unit):
    """A systemd unit name with its instance numbers and ids replaced, e.g.
    ``session-12.scope`` -> ``session-<N>.scope``."""
    text = unit or ""
    for pattern, placeholder in _UNIT_INSTANCE_SUBSTITUTIONS:
        text = pattern.sub(placeholder, text)
    return text


def fingerprint(source, origin, template):
    raw = f"{source or '-'}|{origin or '-'}|{template}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class Finding:
    """One NEW or BURST fact for the detector to emit."""

    event_type: str
    fingerprint: str
    source: str
    origin: str
    template: str
    sample: str
    ts_epoch: float
    attributes: dict = field(default_factory=dict)


class _Signature:
    __slots__ = ("source", "origin", "template", "sample", "first_seen", "last_seen",
                 "total", "buckets", "last_burst_emit", "burst_expected", "dirty", "announced")

    def __init__(self, source, origin, template, sample, first_seen, last_seen=None, total=0,
                 announced=True):
        self.source = source
        self.origin = origin
        self.template = template
        self.sample = sample
        self.first_seen = first_seen
        self.last_seen = last_seen if last_seen is not None else first_seen
        self.total = total
        # [bucket index, count] per _BUCKET_SECONDS of the burst window.
        self.buckets = collections.deque()
        self.last_burst_emit = None
        # Expected count per window, taken when the current burst started.
        self.burst_expected = None
        self.dirty = False
        # Reported as NEW, learned or loaded. False (and unsaved) while the hourly NEW cap
        # holds it back, so it is reported when it recurs.
        self.announced = announced


class SignatureTracker:
    """Per-host error-signature state. Thread-safe."""

    def __init__(self, learning_seconds=_LEARNING_SECONDS, store=None, clock=time.time):
        self._lock = threading.Lock()
        self._learning_seconds = learning_seconds
        self._store = store
        self._clock = clock
        self._sigs = collections.OrderedDict()
        self._loaded = False
        self._learning_until = 0.0
        self._new_times = collections.deque()
        self._new_suppressed = 0
        self._last_prune = 0.0
        # (source, origin) -> event time of its last BURST, and the bursting
        # templates folded into that report since.
        self._origin_burst = {}
        self._origin_folded = collections.Counter()
        # Record key -> findings of its first observation, replayed when the detector
        # re-reads a record whose events failed to store.
        self._replays = collections.OrderedDict()

    # -- persistence ---------------------------------------------------------

    def _model(self):
        if self._store is not None:
            return self._store
        from db.DatabaseModel import ErrorSignature
        return ErrorSignature

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        model = self._model()
        rows = model.load_recent(_MAX_TRACKED)
        for row in reversed(rows):  # oldest first, so the newest end up most recent
            self._sigs[row.fingerprint] = _Signature(
                row.source, row.origin, row.template, row.sample,
                float(row.first_seen or 0.0), float(row.last_seen or 0.0), int(row.total_count or 0))
        self._learning_until = self._load_learning(model, fresh=not rows)

    def _load_learning(self, model, *, fresh):
        """End of the learning period (persisted). It starts at the first scan of a fresh
        install; the first boot after the install also learns, to absorb boot-time errors."""
        now = self._clock()
        boot = _current_boot()
        state = model.load_learning() or {}
        if not state:
            learning = bool(self._learning_seconds) and fresh
            state = {"until": now + self._learning_seconds if learning else 0.0,
                     "install_boot": boot, "reboot_learned": not learning}
            if learning:
                logger.info("Error signatures: learning existing errors for %ds", self._learning_seconds)
            model.save_learning(state)
        elif (not state.get("reboot_learned") and boot and state.get("install_boot")
              and boot != state["install_boot"]):
            state["until"] = max(float(state.get("until") or 0.0), now + _REBOOT_LEARNING_SECONDS)
            state["reboot_learned"] = True
            logger.info("Error signatures: learning boot-time errors for %ds", _REBOOT_LEARNING_SECONDS)
            model.save_learning(state)
        return float(state.get("until") or 0.0)

    def _lookup(self, fp):
        sig = self._sigs.get(fp)
        if sig is not None:
            self._sigs.move_to_end(fp)
            return sig
        # Evicted from memory but maybe known on disk: not new.
        row = self._model().lookup(fp)
        if row is None:
            return None
        sig = _Signature(row.source, row.origin, row.template, row.sample,
                         float(row.first_seen or 0.0), float(row.last_seen or 0.0),
                         int(row.total_count or 0))
        self._remember(fp, sig)
        return sig

    def _remember(self, fp, sig):
        self._sigs[fp] = sig
        self._sigs.move_to_end(fp)
        while len(self._sigs) > _MAX_TRACKED:
            old_fp, old = self._sigs.popitem(last=False)
            if old.dirty and old.announced:
                self._model().upsert_many([self._row(old_fp, old)])

    @staticmethod
    def _row(fp, sig):
        return {"fingerprint": fp, "source": sig.source, "origin": sig.origin,
                "template": sig.template, "sample": sig.sample, "first_seen": sig.first_seen,
                "last_seen": sig.last_seen, "total_count": sig.total}

    def flush(self):
        """Persist changed signatures and prune long-unseen ones. Best-effort. Also loads
        state, which starts the learning period on a fresh install."""
        with self._lock:
            self._ensure_loaded()
            # Unannounced templates are not saved, so after a restart they are still new.
            dirty = [(fp, sig) for fp, sig in self._sigs.items() if sig.dirty and sig.announced]
            if dirty and self._model().upsert_many([self._row(fp, sig) for fp, sig in dirty]):
                for _, sig in dirty:
                    sig.dirty = False
            now = self._clock()
            if now - self._last_prune >= _PRUNE_INTERVAL_SECONDS:
                self._last_prune = now
                self._model().prune(now - _RETENTION_SECONDS)
            return len(dirty)

    # -- detection -----------------------------------------------------------

    def observe(self, *, source, origin, message, ts_epoch, key=None, template=None):
        """Account one error occurrence; return the Findings it produces. The same ``key``
        again replays the first findings. ``template``: normalize(message), if known."""
        template = normalize(message) if template is None else template
        if not template:
            return []
        fp = fingerprint(source, origin, template)
        sample = (message or "").strip().splitlines()[0][:_SAMPLE_MAX]
        with self._lock:
            if key is not None and key in self._replays:
                self._replays.move_to_end(key)
                return list(self._replays[key])
            findings = self._observe(fp, source, origin, template, sample, ts_epoch)
            if key is not None:
                self._replays[key] = list(findings)
                while len(self._replays) > _REPLAY_KEYS:
                    self._replays.popitem(last=False)
            return findings

    def _observe(self, fp, source, origin, template, sample, ts_epoch):
        """observe() for a record not seen before. Caller holds the lock."""
        self._ensure_loaded()
        sig = self._lookup(fp)
        announce_new = False
        if sig is None:
            sig = _Signature(source, origin, template, sample, ts_epoch,
                             announced=ts_epoch < self._learning_until)  # learned, not reported
            self._remember(fp, sig)
        if not sig.announced:
            # Count each held-back template once in new_signatures_suppressed.
            announce_new = self._admit_new(ts_epoch, count_refusal=sig.total == 0)
            sig.announced = announce_new
        prior_total = sig.total
        sig.total += 1
        sig.last_seen = max(sig.last_seen, ts_epoch)
        sig.first_seen = min(sig.first_seen, ts_epoch)
        sig.dirty = True
        findings = []
        if announce_new:
            findings.append(self._finding(NEW, fp, sig, ts_epoch, {
                "new_signatures_suppressed": self._take_suppressed()}))
        burst = self._burst(sig, ts_epoch, prior_total)
        if burst is not None:
            findings.append(self._finding(BURST, fp, sig, ts_epoch, burst))
        return findings

    def _admit_new(self, ts_epoch, count_refusal=True):
        while self._new_times and ts_epoch - self._new_times[0] >= 3600:
            self._new_times.popleft()
        if len(self._new_times) >= _NEW_PER_HOUR:
            if count_refusal:
                self._new_suppressed += 1
            return False
        self._new_times.append(ts_epoch)
        return True

    def _take_suppressed(self):
        count, self._new_suppressed = self._new_suppressed, 0
        return count

    def _burst(self, sig, ts_epoch, prior_total):
        index = int(ts_epoch // _BUCKET_SECONDS)
        buckets = sig.buckets
        if buckets and buckets[-1][0] >= index:
            buckets[-1][1] += 1  # same bucket (or a late record: count it as current)
        else:
            buckets.append([index, 1])
        while buckets and buckets[0][0] <= buckets[-1][0] - _WINDOW_BUCKETS:
            buckets.popleft()
        count = sum(n for _, n in buckets)
        if count < _BURST_MIN_COUNT:
            return None
        ongoing = (sig.last_burst_emit is not None and sig.burst_expected is not None
                   and 0 <= ts_epoch - sig.last_burst_emit
                   < _BURST_HEARTBEAT_SECONDS + _BURST_WINDOW_SECONDS)
        if ongoing:
            # Judged against the prediction from its start, which its own occurrences
            # have not raised yet.
            expected = sig.burst_expected
        else:
            # History's prediction for one window, excluding the occurrences inside it.
            before = max(0, prior_total + 1 - count)
            age = max(_BASELINE_MIN_AGE_SECONDS, buckets[0][0] * _BUCKET_SECONDS - sig.first_seen)
            expected = before / age * _BURST_WINDOW_SECONDS
        if count < _BURST_RATIO * expected:
            return None
        if ongoing and ts_epoch - sig.last_burst_emit < _BURST_HEARTBEAT_SECONDS:
            return None
        if not ongoing:
            sig.burst_expected = expected
        sig.last_burst_emit = ts_epoch
        component = (sig.source, sig.origin)
        last = self._origin_burst.get(component)
        if last is not None and 0 <= ts_epoch - last < _BURST_HEARTBEAT_SECONDS:
            # Another message of the same component is already being reported.
            self._origin_folded[component] += 1
            return None
        self._origin_burst[component] = ts_epoch
        return {"window_seconds": _BURST_WINDOW_SECONDS, "count_in_window": count,
                "expected_in_window": round(expected, 2),
                "other_templates_bursting": self._origin_folded.pop(component, 0)}

    @staticmethod
    def _finding(event_type, fp, sig, ts_epoch, extra):
        attributes = {"first_seen_epoch": sig.first_seen, "total_count": sig.total}
        attributes.update(extra or {})
        return Finding(event_type=event_type, fingerprint=fp, source=sig.source, origin=sig.origin,
                       template=sig.template, sample=sig.sample, ts_epoch=ts_epoch,
                       attributes=attributes)


__all__ = ["BURST", "NEW", "Finding", "SignatureTracker", "fingerprint", "normalize"]
