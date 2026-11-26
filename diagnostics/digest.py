# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""On-demand, read-only diagnostics digests."""

import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone

# Alert rows a digest lists; ``coverage.alerts_truncated`` says when there were more.
_ALERT_LIMIT = 100
_DIGEST_CATEGORY = "diagnostic.digest"


@dataclass
class Digest:
    period: dict
    generated_at: str
    event_stats: dict
    alert_summary: list
    range_alerts: list
    config_changes: list
    benchmark_regressions: list
    active_alerts: list
    comparison: dict = None
    # Retention window; ``complete`` is false when the range reaches back further.
    coverage: dict = None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, date):
        return value
    raise ValueError("end_date must be an ISO date")


def _local_midnight(day):
    """Epoch seconds of 00:00 on *day* in the server's local time zone.

    A calendar date means the operator's day, not the UTC one: in UTC+8 a UTC
    day runs 08:00 to 08:00 and would leave the small hours out of "today".
    Each boundary is resolved on its own so a DST change (a 23 or 25 hour day)
    still lands on midnight.
    """
    return int(datetime.combine(day, time.min).astimezone().timestamp())


def _daily_window(end_date):
    day = _as_date(end_date)
    return _local_midnight(day), _local_midnight(day + timedelta(days=1))


def _weekly_window(end_date):
    """The seven local days ending with *end_date*."""
    day = _as_date(end_date)
    return _local_midnight(day - timedelta(days=6)), _local_midnight(day + timedelta(days=1))


def _previous_week_start(end_date):
    return _local_midnight(_as_date(end_date) - timedelta(days=13))


class DigestGenerator:
    """Generate read-only period summaries from diagnostics data."""

    def generate_daily(self, end_date):
        start_time, end_time = _daily_window(end_date)
        return self._generate("day", start_time=start_time, end_time=end_time)

    def generate_weekly(self, end_date):
        start_time, end_time = _weekly_window(end_date)
        previous_start = _previous_week_start(end_date)
        if previous_start < _retained_from():
            # Outside retention: all-zero counts would read as a problem-free week.
            comparison = {"previous_period": None, "unavailable_reason": "outside_retention"}
        else:
            comparison = {"previous_period": self._summary_for_window(previous_start, start_time)}
        return self._generate("week", start_time=start_time, end_time=end_time, comparison=comparison)

    def generate_range(self, start_time, end_time):
        try:
            start_time, end_time = int(start_time), int(end_time)
        except (TypeError, ValueError):
            raise ValueError("from and to must be integer epoch seconds")
        if end_time <= start_time:
            raise ValueError("to must be later than from")
        return self._generate("range", start_time=start_time, end_time=end_time)

    def generate_per_boot(self, boot_id):
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("boot_id is required")
        return self._generate("boot", boot_id=boot_id)

    def _generate(self, kind, *, start_time=None, end_time=None, boot_id=None, comparison=None):
        from db.DatabaseModel import OperationalEvent
        from diagnostics import config_revision, event_store, retention

        counts, first, last = OperationalEvent.severity_counts(
            start_time=start_time, end_time=end_time - 1 if end_time is not None else None,
            boot_id=boot_id, exclude_category=_DIGEST_CATEGORY)
        if kind == "boot":
            # A boot spans its events; with none there is no range, so no range alerts.
            start_time, end_time = (first, last + 1) if first is not None else (None, None)
        bounded = start_time is not None and end_time is not None
        revisions = config_revision.query_revisions_range(start_time, end_time - 1) if bounded else []
        config_changes = [revision for revision in revisions if revision.get("change_summary")]
        active_alerts = event_store.query_alerts(active_only=True, limit=_ALERT_LIMIT)
        # Every occurrence active at some point in the range, matching the
        # dashboard's alert history view.
        range_alerts = event_store.query_alerts(
            active_only=False, limit=_ALERT_LIMIT + 1,
            start_time=start_time, end_time=end_time - 1,
        ) if bounded else []
        alerts_truncated = len(range_alerts) > _ALERT_LIMIT
        range_alerts = range_alerts[:_ALERT_LIMIT]
        retained_from = _retained_from()
        digest = Digest(
            period={"kind": kind, "from": start_time, "to": end_time, "boot_id": boot_id},
            generated_at=datetime.now(timezone.utc).isoformat(),
            event_stats=self._event_stats(counts),
            alert_summary=self._top_alerts(range_alerts),
            range_alerts=range_alerts,
            config_changes=config_changes,
            benchmark_regressions=[],
            active_alerts=active_alerts,
            comparison=comparison,
            coverage={
                "retention_days": retention.configured_days(),
                "retained_from": retained_from,
                "complete": start_time is None or start_time >= retained_from,
                "alerts_truncated": alerts_truncated,
            },
        )
        return asdict(digest)

    @staticmethod
    def _event_stats(counts):
        by_severity = {severity: 0 for severity in ("info", "warning", "error", "critical")}
        for severity, count in counts.items():
            key = (severity or "info").lower()
            by_severity[key] = by_severity.get(key, 0) + count
        return {"total": sum(by_severity.values()), "by_severity": by_severity}

    @staticmethod
    def _top_alerts(active_alerts):
        return sorted(
            active_alerts,
            key=lambda alert: (int(alert.get("fire_count") or 0), alert.get("last_fired_at") or ""),
            reverse=True,
        )[:3]

    def _summary_for_window(self, start_time, end_time):
        from db.DatabaseModel import OperationalEvent

        counts, _, _ = OperationalEvent.severity_counts(
            start_time=start_time, end_time=end_time - 1, exclude_category=_DIGEST_CATEGORY)
        return self._event_stats(counts)


def _retained_from():
    """Epoch of the oldest event the ledger still keeps."""
    from diagnostics import retention

    return int(_time.time()) - retention.configured_days() * 86400
