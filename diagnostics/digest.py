# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""On-demand, read-only diagnostics digests."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone


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
        previous = self._summary_for_window(_previous_week_start(end_date), start_time)
        return self._generate("week", start_time=start_time, end_time=end_time,
                              comparison={"previous_period": previous})

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
        from diagnostics import config_revision, event_store

        filters = {"limit": 5000}
        if start_time is not None:
            filters["start_time"] = start_time
            filters["end_time"] = end_time - 1
        if boot_id:
            filters["boot_id"] = boot_id
        events = self._business_events(event_store.query_events(**filters))
        if kind == "boot" and events:
            timestamps = [self._event_epoch(event) for event in events]
            timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
            start_time = min(timestamps) if timestamps else None
            end_time = max(timestamps) + 1 if timestamps else None
        revisions = (config_revision.query_revisions_range(start_time, end_time - 1)
                     if start_time is not None and end_time is not None else [])
        config_changes = [revision for revision in revisions if revision.get("change_summary")]
        active_alerts = event_store.query_alerts(active_only=True, limit=100)
        # Every occurrence active at some point in the range, matching the
        # dashboard's alert history view.
        range_alerts = event_store.query_alerts(
            active_only=False, limit=100,
            start_time=start_time, end_time=end_time - 1 if end_time is not None else None,
        )
        digest = Digest(
            period={"kind": kind, "from": start_time, "to": end_time, "boot_id": boot_id},
            generated_at=datetime.now(timezone.utc).isoformat(),
            event_stats=self._event_stats(events),
            alert_summary=self._top_alerts(range_alerts),
            range_alerts=range_alerts,
            config_changes=config_changes,
            benchmark_regressions=[],
            active_alerts=active_alerts,
            comparison=comparison,
        )
        return asdict(digest)

    @staticmethod
    def _business_events(events):
        return [event for event in events if event.get("category") != "diagnostic.digest"]

    @staticmethod
    def _event_epoch(event):
        try:
            return int(datetime.fromisoformat(event.get("ts_utc") or "").timestamp())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _event_stats(events):
        severity_counts = {severity: 0 for severity in ("info", "warning", "error", "critical")}
        for event in events:
            severity = (event.get("severity") or "info").lower()
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        return {"total": len(events), "by_severity": severity_counts}

    @staticmethod
    def _top_alerts(active_alerts):
        return sorted(
            active_alerts,
            key=lambda alert: (int(alert.get("fire_count") or 0), alert.get("last_fired_at") or ""),
            reverse=True,
        )[:3]

    def _summary_for_window(self, start_time, end_time):
        from diagnostics import event_store

        events = event_store.query_events(
            start_time=start_time, end_time=end_time - 1, limit=5000)
        return self._event_stats(self._business_events(events))
