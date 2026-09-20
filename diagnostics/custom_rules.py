# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Evaluate user-defined diagnostic rules against normalized log records."""

from diagnostics import emit_event


def event_type_for(rule_id):
    """Build the stable event type used for a user-defined rule."""
    return f"CUSTOM_RULE::{rule_id}"


def compile_rules(raw_rules, source):
    """Return enabled rules for one supported source, skipping invalid legacy data."""
    compiled = []
    for rule in raw_rules or []:
        if not isinstance(rule, dict) or not rule.get("enabled") or rule.get("source") != source:
            continue
        pattern = rule.get("message_pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        compiled.append((rule, pattern))
    return compiled


def configured_rules(source):
    """Read the current configuration so rule changes take effect without restart."""
    try:
        from config.config import b_config

        return compile_rules(getattr(b_config, "diagnostic_rules", None), source)
    except Exception:
        return []


def emit_matches(record, rules, identity):
    """Emit one event for each configured rule that matches a normalized record."""
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
                "excerpt": message[:2000],
                "domain": rule.get("domain", "services"),
                "event_kind": rule.get("event_kind", "status"),
                "fatal_signatures": rule.get("fatal_signatures", []),
            },
            ts_utc=record.ts_iso or None,
            identity=f"custom:{rule['id']}:{identity}",
        )
        emitted += int(event is not None)
    return emitted