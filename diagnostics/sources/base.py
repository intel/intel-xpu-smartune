# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Pluggable log-source contract.
#
# A LogSource adapts one physical storage tier (SmartTune's own JSON files,
# journald, benchmark artifacts, platform driver nodes) into a stream of uniform
# LogRecord objects. Adding a new log type is: implement this protocol, then
# register_source() it -- log_query / detectors / bundle consume it with zero
# changes. This is the extension seam, NOT a second storage: sources only READ.

from dataclasses import dataclass, field, asdict
from typing import Optional


# Numeric ranks so a "level >= X" filter works across sources regardless of the
# textual level a given source uses. Unknown -> INFO.
_LEVEL_RANK = {
    "debug": 10, "info": 20, "warning": 30, "warn": 30,
    "error": 40, "err": 40, "critical": 50, "crit": 50, "fatal": 50,
}


def level_rank(level) -> int:
    return _LEVEL_RANK.get((level or "").strip().lower(), 20)


@dataclass
class LogRecord:
    """One normalized log line. ``ts_epoch`` (wall-clock seconds) is the join key
    across sources; ``fields`` carries source-specific structured extras."""

    ts_epoch: float
    ts_iso: str
    level: str
    source: str          # smartune / journal / benchmark / platform
    logger: str
    message: str
    service: Optional[str] = None
    app_id: Optional[str] = None
    job_id: Optional[str] = None
    fields: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass
class LogQueryFilter:
    """Normalized query passed to every source. A source ignores filters it does
    not support (e.g. the benchmark source keys off job_id)."""

    start_time: Optional[int] = None      # epoch seconds, inclusive
    end_time: Optional[int] = None        # epoch seconds, inclusive
    min_level: Optional[str] = None       # keep records with level >= this
    job_id: Optional[str] = None
    boot_id: Optional[str] = None         # journald boot session; ignored by sources without a boot concept
    keyword: Optional[str] = None
    # Kernel-only lens for journald: restrict to the kernel ring buffer (OOM, GPU
    # reset, driver faults, panics) -- journal's diagnostic value next to SmartTune's
    # own logs. User-facing /diag/logs sets this; detector scans leave it False so
    # they still see userspace lines (e.g. systemd restart loops). Non-journald
    # sources ignore it.
    kernel_only: bool = False
    # Reserved for detector/background scans that need to inspect a source
    # globally; user-facing /diag/logs queries keep this False.
    allow_unscoped: bool = False
    limit: int = 500

    def accepts(self, rec: "LogRecord") -> bool:
        """Generic post-filter a source can apply after cheap coarse reads."""
        if self.start_time is not None and rec.ts_epoch < self.start_time:
            return False
        if self.end_time is not None and rec.ts_epoch > self.end_time:
            return False
        if self.min_level and level_rank(rec.level) < level_rank(self.min_level):
            return False
        if self.job_id and rec.job_id != self.job_id:
            return False
        if self.boot_id:
            # Lenient by design: only records that carry a boot_id are scoped by
            # it. Sources without a boot concept (smartune / benchmark) leave the
            # field empty, so a boot filter narrows the journal without culling
            # application logs from the same time window.
            rec_boot = (rec.fields or {}).get("boot_id")
            if rec_boot is not None and rec_boot != self.boot_id:
                return False
        if self.keyword and self.keyword.lower() not in (rec.message or "").lower():
            return False
        return True


class LogSource:
    """Interface every source implements. Subclasses override ``name`` and the
    methods they can serve; unsupported capabilities return empty results."""

    name = "base"

    def describe(self) -> dict:
        """Static self-description for the UI/source picker."""
        return {"name": self.name, "available": self.available()}

    def available(self) -> bool:
        return True

    def query(self, flt: "LogQueryFilter"):
        """Return a list[LogRecord] matching ``flt``. Best-effort, never raises."""
        raise NotImplementedError

    def tail(self, flt: "LogQueryFilter"):
        """Optional: most-recent records for a follow view. Defaults to query()."""
        return self.query(flt)

    def list_boots(self, limit=15):
        """Optional: enumerate boot sessions this source can scope by.

        Returns a list of dicts, newest first, each with ``boot_id`` and, when
        known, ``index`` / ``first_ts`` / ``last_ts`` (epoch seconds) and
        ``running``. Sources without a boot concept return an empty list, which is
        exactly why the /diag/boots façade only surfaces the ones that do."""
        return []
