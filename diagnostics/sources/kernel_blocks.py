# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Kernel multi-line report coalescing: printk logs one journal entry per line, so the lines of
# one report (a WARN and its Call Trace, an OOM dump, ...) are grouped into a block. Pure, no
# I/O. A block holds kernel lines of one boot at most _BLOCK_GAP_SECONDS apart: a header takes
# every following line, otherwise only clear continuation lines attach. Audit lines never
# join or close a block.

import re
from dataclasses import replace

from diagnostics.sources.base import level_rank

_BLOCK_GAP_SECONDS = 0.5
_BLOCK_MAX_LINES = 400
# An OOM report lists every process, so it can run to thousands of lines.
_BLOCK_MAX_LINES_BY_KIND = {"oom": 5000}


def max_lines(kind):
    """How many lines a report of ``kind`` may hold before it is cut."""
    return _BLOCK_MAX_LINES_BY_KIND.get(kind, _BLOCK_MAX_LINES)

_CUT_HERE_RE = re.compile(r"^-+\[ cut here \]-+\s*$")

# (kind, header regex). Order matters only for the reported kind.
_HEADERS = (
    ("warning", _CUT_HERE_RE),
    ("warning", re.compile(r"^WARNING: (?:CPU: \d+ PID: \d+ at )?\S+\.[chS]:\d+")),
    ("hung_task", re.compile(r"^INFO: task \S.* blocked for more than \d+ seconds")),
    ("soft_lockup", re.compile(r"^(?:watchdog: )?BUG: soft lockup")),
    ("hard_lockup", re.compile(r"^(?:NMI watchdog|watchdog): .*hard LOCKUP", re.IGNORECASE)),
    ("rcu_stall", re.compile(r"^rcu: INFO: \S+ (?:self-)?detected (?:expedited )?stalls?")),
    ("bug", re.compile(r"^(?:kernel BUG at |BUG: )")),
    ("oops", re.compile(r"^Oops: ")),
    ("gpf", re.compile(r"^general protection fault")),
    ("panic", re.compile(r"^Kernel panic - not syncing")),
    ("oom", re.compile(r"\binvoked oom-killer: ")),
    # A task name (comm) may contain spaces: "Web Content[4242]: segfault at ...".
    ("segfault", re.compile(r"^\S[^\[]*\[\d+\]: segfault at ")),
    ("trap", re.compile(r"^traps: \S[^\[]*\[\d+\] ")),
    ("mce", re.compile(r"^mce: \[Hardware Error\]: ")),
    ("aer", re.compile(r"\bAER: (?:Multiple )?(?:Corrected|Correctable|Uncorrected|Uncorrectable)\b"
                       r".*error(?: message)? received", re.IGNORECASE)),
)

# Header kind -> open report kinds it continues instead of starting a new block
# (an Oops after its BUG line, a panic after its cause, one MCE line per field).
_JOINS = {
    "oops": {"bug", "gpf"},
    "panic": {"bug", "gpf", "oops", "hung_task", "soft_lockup", "hard_lockup", "rcu_stall",
              "mce", "warning"},
    "mce": {"mce"},
}

# Lines that only ever continue a report started on an earlier line.
_CONTINUATION_RE = re.compile(
    r"^\s"                                   # indented: trace frames, Tainted:, ...
    r"|^Call Trace:"
    r"|^</?(?:TASK|IRQ|NMI|SOFTIRQ)>"
    r"|^Modules linked in:"
    r"|^Hardware name: "
    r"|^Workqueue: "
    r"|^CPU: \d+ (?:UID: \d+ )?PID: \d+ "
    r"|^(?:RIP|RSP|RAX|RDX|RBP|R\d\d|FS|CS|CR2|DR[0-7]|PKRU|EFLAGS): "
    r"|^Code: "
    r"|^---\[ end "
    r"|^Kernel Offset: "
    r"|^mce: \[Hardware Error\]: "
)

# A line that completes a report: nothing after it belongs to the same block.
_END_RE = re.compile(
    r"^---\[ end "
    r"|^(?:Out of memory|Memory cgroup out of memory): Killed process "
)

_AUDIT_RE = re.compile(r"^audit: ")


def is_kernel(rec):
    return (rec.fields or {}).get("transport") == "kernel"


def _clock(rec):
    """The printk monotonic stamp of ``rec`` (exact even within a burst), else its wall clock."""
    mono = (rec.fields or {}).get("monotonic_s")
    return mono if isinstance(mono, (int, float)) else rec.ts_epoch


def is_cut_here(message):
    return bool(_CUT_HERE_RE.match((message or "").strip()))


def header_kind(message):
    first = (message or "").split("\n", 1)[0]
    for kind, pattern in _HEADERS:
        if pattern.search(first):
            return kind
    return None


def kind_after_cut_here(lines):
    """Kind of the first header after "[ cut here ]"; a driver message may come in between."""
    for rec in lines[1:]:
        kind = header_kind(rec.message)
        if kind is not None and not is_cut_here(rec.message):
            return kind
    return "warning"


class _Block:
    __slots__ = ("lines", "kind", "header", "closed", "awaiting_header")

    def __init__(self, rec, kind):
        self.lines = [rec]
        self.kind = kind            # header kind, or None for an un-headed line
        self.header = kind is not None
        self.closed = False
        # After "[ cut here ]" the real header (WARNING / BUG) is still to come.
        self.awaiting_header = bool(_CUT_HERE_RE.match(rec.message or ""))

    @property
    def last(self):
        return self.lines[-1]

    def accepts(self, rec):
        if self.closed or len(self.lines) >= max_lines(self.kind):
            return False
        last = self.last
        if (rec.fields or {}).get("boot_id") != (last.fields or {}).get("boot_id"):
            return False
        gap = _clock(rec) - _clock(last)
        if gap < 0 or gap > _BLOCK_GAP_SECONDS:
            return False
        message = rec.message or ""
        if _AUDIT_RE.match(message):
            return False
        kind = header_kind(message)
        if kind is not None:
            # The first header after "[ cut here ]" belongs to the same report.
            if self.awaiting_header and not _CUT_HERE_RE.match(message):
                return True
            return self.header and self.kind in _JOINS.get(kind, ())
        if self.header:
            return True
        return bool(_CONTINUATION_RE.match(message))

    def add(self, rec):
        if self.awaiting_header:
            kind = header_kind(rec.message)
            if kind is not None:
                self.kind = kind
                self.awaiting_header = False
        self.lines.append(rec)
        if _END_RE.match(rec.message or ""):
            self.closed = True


class KernelBlockTracker:
    """Line-by-line grouping: ``continues`` peeks whether a record joins the open block,
    ``push`` adds it and returns its group list."""

    def __init__(self):
        self._open = None

    def continues(self, rec):
        return bool(is_kernel(rec) and self._open is not None and self._open.accepts(rec))

    def open_kind(self):
        """Kind of the report still open, or None."""
        return self._open.kind if self._open is not None and not self._open.closed else None

    def push(self, rec):
        if not is_kernel(rec):
            return [rec]
        if self._open is not None and self._open.accepts(rec):
            self._open.add(rec)
            return self._open.lines
        if _AUDIT_RE.match(rec.message or ""):
            # Interleaved audit noise: a group of its own, the open block stays open.
            return [rec]
        self._open = _Block(rec, header_kind(rec.message))
        return self._open.lines


def group_records(records):
    """Group ``records`` oldest first: each kernel report's lines together, other records alone."""
    tracker = KernelBlockTracker()
    groups = []
    seen = set()
    # Stable sort: journalctl already yields journal order, keep it on ties.
    for rec in sorted(records, key=lambda r: r.ts_epoch):
        group = tracker.push(rec)
        if id(group) not in seen:
            seen.add(id(group))
            groups.append(group)
    return groups


def block_title(lines):
    """The line a human would call the report's headline."""
    for rec in lines:
        message = (rec.message or "").strip()
        if message and not _CUT_HERE_RE.match(message):
            return message
    return (lines[0].message or "").strip()


def merge_block(lines):
    """One LogRecord for a group. A single line is returned unchanged."""
    if len(lines) == 1:
        return lines[0]
    head = lines[0]
    worst = max(lines, key=lambda r: level_rank(r.level))
    kind = header_kind(head.message) or "stack"
    if kind == "warning" and _CUT_HERE_RE.match(head.message or ""):
        kind = kind_after_cut_here(lines)
    fields = dict(head.fields or {})
    fields.update({
        "block": kind,
        "block_title": block_title(lines),
        "line_count": len(lines),
        "block_truncated": len(lines) >= max_lines(header_kind(head.message)),
        "last_cursor": (lines[-1].fields or {}).get("cursor"),
        "end_monotonic_s": (lines[-1].fields or {}).get("monotonic_s"),
    })
    return replace(
        head,
        level=worst.level,
        message="\n".join(rec.message or "" for rec in lines),
        fields=fields,
    )


def coalesce(records):
    """Records with kernel reports merged into one record each, oldest first."""
    return [merge_block(group) for group in group_records(records)]


def block_excerpt(text, limit=4000):
    """Head and tail of a long block; the tail holds the OOM victim or the faulting RIP."""
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head - 5
    return f"{text[:head]}\n...\n{text[-tail:]}"


__all__ = [
    "KernelBlockTracker", "block_excerpt", "block_title", "coalesce", "group_records",
    "header_kind", "is_cut_here", "is_kernel", "kind_after_cut_here", "max_lines", "merge_block",
]
