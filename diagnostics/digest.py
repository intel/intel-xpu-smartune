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


def _daily_window(end_date):
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    elif isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)
    if not isinstance(end_date, date):
        raise ValueError("end_date must be an ISO date")
    start = datetime.combine(end_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _weekly_window(end_date):
    day_start, day_end = _daily_window(end_date)
    return day_start - 6 * 86400, day_end


class DigestGenerator:
    """Generate read-only period summaries from diagnostics data."""

    def generate_daily(self, end_date):
        start_time, end_time = _daily_window(end_date)
        return self._generate("day", start_time=start_time, end_time=end_time)

    def generate_weekly(self, end_date):
        start_time, end_time = _weekly_window(end_date)
        previous = self._summary_for_window(start_time - 7 * 86400, start_time)
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
        range_alerts = event_store.query_alerts(active_only=False, limit=100)
        if start_time is not None and end_time is not None:
            range_alerts = [
                alert for alert in range_alerts
                if start_time <= (self._event_epoch({"ts_utc": alert.get("last_fired_at")}) or -1) < end_time
            ]
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
