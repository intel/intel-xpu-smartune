# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Evaluate user-defined diagnostic rules against normalized log records."""

from diagnostics import emit_event

EVENT_TYPE_PREFIX = "CUSTOM_RULE::"

# How a rule's alert closes: "until_ack" on acknowledgement, "until_quiet" after
# alert_quiet_minutes without a match.
ALERT_MODES = ("off", "until_ack", "until_quiet")
DEFAULT_QUIET_MINUTES = 10
ALERT_SEVERITIES = ("warning", "error", "critical")


def event_type_for(rule_id):
    """Build the stable event type used for a user-defined rule."""
    return f"{EVENT_TYPE_PREFIX}{rule_id}"


def _all_rules():
    try:
        from config.config import b_config

        rules = getattr(b_config, "diagnostic_rules", None)
    except Exception:
        return []
    return [rule for rule in rules or [] if isinstance(rule, dict)]


def _policy(rule):
    mode = rule.get("alert_mode") or "off"
    if not rule.get("enabled") or mode not in ALERT_MODES or mode == "off":
        return None
    if rule.get("severity") not in ALERT_SEVERITIES:
        return None
    if mode == "until_ack":
        return {"severity": rule["severity"], "close_on_ack": True}
    try:
        minutes = int(rule.get("alert_quiet_minutes") or DEFAULT_QUIET_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_QUIET_MINUTES
    return {"severity": rule["severity"], "ttl_seconds": max(1, minutes) * 60}


def alert_policy(rule_id):
    """The alert policy of a configured rule, or None when it does not alert."""
    for rule in _all_rules():
        if rule.get("id") == rule_id:
            return _policy(rule)
    return None


def alerting_event_types():
    """Event types of the configured rules that currently alert."""
    return [event_type_for(rule["id"]) for rule in _all_rules()
            if rule.get("id") and _policy(rule) is not None]


def alerting_rules():
    """``(rule_id, name, source, policy)`` of every configured rule that currently alerts."""
    rules = []
    for rule in _all_rules():
        policy = _policy(rule) if rule.get("id") else None
        if policy is not None:
            rules.append((rule["id"], str(rule.get("name") or rule["id"]),
                          str(rule.get("source") or "log"), policy))
    return rules


_EVENT_SEVERITIES = ("info",) + ALERT_SEVERITIES


def compile_rules(raw_rules, source):
    """Return enabled rules for one supported source, skipping invalid legacy data.
    A missing name, severity or category gets a default, since emit_matches() must not raise."""
    compiled = []
    for rule in raw_rules or []:
        if not isinstance(rule, dict) or not rule.get("enabled") or rule.get("source") != source:
            continue
        pattern = rule.get("message_pattern")
        rule_id = rule.get("id")
        if not isinstance(pattern, str) or not pattern.strip() or rule_id in (None, ""):
            continue
        rule = dict(rule)
        if rule.get("severity") not in _EVENT_SEVERITIES:
            rule["severity"] = "warning"
        rule["category"] = str(rule.get("category") or "service.custom")  # the settings API default
        rule["name"] = str(rule.get("name") or rule_id)
        compiled.append((rule, pattern))
    return compiled


def configured_rules(source):
    """Read the current configuration so rule changes take effect without restart."""
    try:
        from config.config import b_config

        return compile_rules(getattr(b_config, "diagnostic_rules", None), source)
    except Exception:
        return []


def emit_matches(record, rules, identity, excerpt=None):
    """Emit one event for each configured rule that matches a normalized record.
    ``excerpt`` replaces only the evidence text; rules match the record's own message."""
    emitted = 0
    message = record.message or ""
    service = record.service or record.logger
    message_folded = message.casefold()
    for rule, pattern in rules:
        if rule.get("service") and rule["service"] != service:
            continue
        if pattern.casefold() not in message_folded:
            continue
        event = emit_event(
            event_type_for(rule["id"]),
            severity=rule["severity"],
            category=rule["category"],
            summary=f"{rule['name']}: {message.strip().splitlines()[0][:240]}",
            source=record.source,
            app_id=record.app_id,
            job_id=record.job_id,
            attributes={
                "custom_rule_id": rule["id"],
                "custom_rule_name": rule["name"],
                "logger": record.logger,
                "service": service,
                "excerpt": (excerpt or message)[:4000 if excerpt else 2000],
                "domain": rule.get("domain", "services"),
                "event_kind": rule.get("event_kind", "status"),
                "fatal_signatures": rule.get("fatal_signatures", []),
                "alert_mode": rule.get("alert_mode") or "off",
            },
            ts_utc=record.ts_iso or None,
            identity=f"custom:{rule['id']}:{identity}",
        )
        emitted += int(event is not None)
    return emitted