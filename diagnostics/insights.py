# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Insight and expert-advice engine: deterministic, evidence-driven rules
# over the context assembled by context.assemble_context() (events + metrics +
# alerts in a time window). A rule is read-only and produces at most one
# Finding -- observation, evidence, confidence, recommendation, validation
# steps -- or stays silent. No ML, no automatic remediation; low/insufficient
# evidence must lower confidence rather than assert a cause.
#
# Rules are independent and best-effort: one rule raising must never blank out
# the others, so evaluate_rules() isolates each call and logs failures instead
# of propagating them (the context assembly it feeds must never break either).

from dataclasses import dataclass, field, asdict

from utils.logger import get_logger

logger = get_logger(__name__)

# Placeholder thermal/power thresholds until real per-SKU limits are sourced
# from hardware inventory / config_revision. Kept as named constants so the
# eventual wiring is a one-line change per rule, not a rule rewrite.
_CPU_THERMAL_THRESHOLD_C = 85.0
_CPU_THERMAL_HOT_MARGIN_C = 5.0

_MEMORY_AVAILABLE_LOW_GB = 1.0
_MEMORY_AVAILABLE_CRITICAL_GB = 0.5

_MEM_GROWTH_MIN_SAMPLES = 3
_MEM_GROWTH_MIN_SPAN_SECONDS = 300  # need >=5 minutes of samples to trust a slope
_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN = 0.05  # ~3 GB/hour sustained decline
_MEM_GROWTH_EXHAUSTION_ERROR_MIN = 10
_MEM_GROWTH_EXHAUSTION_WARNING_MIN = 30

_KERNEL_HEALTH_EVENT_TYPES = {
    "PLATFORM_KERNEL_PANIC",
    "RESOURCE_MEMORY_OOM_KILL",
}
_CONTROL_OSCILLATION_MIN_TRANSITIONS = 3
_DIAGNOSTICS_SAMPLE_GAP_SECONDS = 60


@dataclass
class Finding:
    id: str
    title: str
    severity: str  # info | warning | error | critical, mirrors event severity
    confidence: float  # 0.0-1.0; low means "signal present, evidence thin"
    category: str  # system_health | collection_health | workload_performance
    observation: str
    evidence: list = field(default_factory=list)  # [{"label","value","threshold"}]
    recommendations: list = field(default_factory=list)
    validation_steps: list = field(default_factory=list)
    related_event_ids: list = field(default_factory=list)
    time_window: dict = None


class InsightRule:
    """One deterministic check. Subclasses implement evaluate() and return a
    Finding or None -- never raise for "no data" (that is just None)."""

    id = None
    title = None
    category = "system_health"

    def evaluate(self, context):
        raise NotImplementedError


def _monitor_series(context):
    return ((context.get("metrics") or {}).get("monitor") or {}).get("series") or []


def _memory_samples(context):
    """[(ts_epoch, available_gb)] oldest-first, skipping samples without it."""
    out = []
    for sample in _monitor_series(context):
        available_gb = ((sample.get("data") or {}).get("memory") or {}).get("available_gb")
        if available_gb is not None:
            out.append((sample.get("ts_epoch"), available_gb))
    return out


def _cpu_temperature_samples(context):
    """[(ts_epoch, temperature_c, package_tjmax_c)] oldest-first."""
    out = []
    for sample in _monitor_series(context):
        cpu = (sample.get("data") or {}).get("cpu") or {}
        temp = cpu.get("temperature_c")
        if temp is not None:
            out.append((sample.get("ts_epoch"), temp, cpu.get("package_tjmax_c")))
    return out


def _monitor_sample_gaps(context):
    timestamps = sorted({sample.get("ts_epoch") for sample in _monitor_series(context)
                         if isinstance(sample.get("ts_epoch"), (int, float))})
    return [(previous, current, current - previous)
            for previous, current in zip(timestamps, timestamps[1:])]


class MemoryGrowthRisk(InsightRule):
    """Available memory trending down fast enough to project exhaustion."""

    id = "memory_growth_risk"
    title = "Memory available is trending down"
    category = "system_health"

    def evaluate(self, context):
        samples = _memory_samples(context)
        if len(samples) < _MEM_GROWTH_MIN_SAMPLES:
            return None
        first_ts, first_gb = samples[0]
        last_ts, last_gb = samples[-1]
        span_seconds = (last_ts or 0) - (first_ts or 0)
        if span_seconds < _MEM_GROWTH_MIN_SPAN_SECONDS:
            return None
        rate_gb_per_min = (last_gb - first_gb) / (span_seconds / 60.0)
        if rate_gb_per_min > -_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN:
            return None  # not declining fast enough to be worth flagging

        projected_min = (last_gb / -rate_gb_per_min) if rate_gb_per_min < 0 else None
        if projected_min is not None and projected_min <= _MEM_GROWTH_EXHAUSTION_ERROR_MIN:
            severity = "error"
        elif projected_min is not None and projected_min <= _MEM_GROWTH_EXHAUSTION_WARNING_MIN:
            severity = "warning"
        else:
            severity = "info"
        confidence = min(0.9, len(samples) / 10.0)

        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=(
                f"Available memory fell from {first_gb:.2f} GB to {last_gb:.2f} GB "
                f"over {span_seconds // 60:.0f} min in this window"),
            evidence=[
                {"label": "available_gb_start", "value": round(first_gb, 2)},
                {"label": "available_gb_end", "value": round(last_gb, 2)},
                {"label": "decline_rate_gb_per_min", "value": round(rate_gb_per_min, 4),
                 "threshold": -_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN},
                {"label": "projected_minutes_to_exhaustion", "value":
                    round(projected_min, 1) if projected_min is not None else None},
            ],
            recommendations=[
                "Identify the process(es) driving the growth (per-app memory usage) "
                "before it forces a control action or an OOM kill.",
            ],
            validation_steps=[
                "Compare available_gb across a longer window to rule out a one-off spike.",
                "Check for a matching CONTROL_MEMORY_LIMIT_APPLIED or RESOURCE_MEMORY_OOM_KILL "
                "event shortly after this window.",
            ],
            time_window=context.get("window"),
        )


class OomRisk(InsightRule):
    """Kernel already killed a process for memory, or available memory is
    critically low even without a kill (yet)."""

    id = "oom_risk"
    title = "Out-of-memory risk"
    category = "system_health"

    def evaluate(self, context):
        oom_events = [e for e in (context.get("events") or [])
                     if e.get("event_type") == "RESOURCE_MEMORY_OOM_KILL"]
        if oom_events:
            processes = sorted({(e.get("attributes") or {}).get("process")
                                for e in oom_events if (e.get("attributes") or {}).get("process")})
            return Finding(
                id=self.id, title=self.title, severity="critical", confidence=1.0,
                category=self.category,
                observation=f"The kernel OOM-killed {len(oom_events)} process(es) in this window",
                evidence=[{"label": "oom_kill_count", "value": len(oom_events)},
                         {"label": "processes", "value": processes}],
                recommendations=[
                    "Reduce the workload's memory footprint or lower its concurrency; "
                    "the kernel already reclaimed by force.",
                ],
                validation_steps=[
                    "Confirm the killed process(es) match the intended workload, "
                    "not an unrelated system service.",
                ],
                related_event_ids=[e.get("event_id") for e in oom_events if e.get("event_id")],
                time_window=context.get("window"),
            )

        samples = _memory_samples(context)
        if not samples:
            return None
        _, latest_gb = samples[-1]
        if latest_gb > _MEMORY_AVAILABLE_LOW_GB:
            return None
        severity = "error" if latest_gb <= _MEMORY_AVAILABLE_CRITICAL_GB else "warning"
        confidence = 0.6  # a low headroom reading alone predicts risk, it did not observe a kill
        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=f"Available memory is {latest_gb:.2f} GB, close to exhaustion",
            evidence=[{"label": "available_gb", "value": round(latest_gb, 2),
                      "threshold": _MEMORY_AVAILABLE_LOW_GB}],
            recommendations=[
                "Watch for an imminent OOM kill or a memory control action; "
                "consider reducing concurrent workload memory use proactively.",
            ],
            validation_steps=[
                "Re-check available_gb after a few minutes to confirm this is not a transient dip.",
            ],
            time_window=context.get("window"),
        )


class ThermalThrottlingDetected(InsightRule):
    """CPU package temperature at/above its throttle point during the window."""

    id = "thermal_throttling_detected"
    title = "CPU thermal throttling risk"
    category = "system_health"

    def evaluate(self, context):
        samples = _cpu_temperature_samples(context)
        if not samples:
            return None
        # Prefer the sensor's own tjmax when a reading carries one; otherwise
        # fall back to the placeholder constant (see module docstring).
        thresholds = [tjmax for _, _, tjmax in samples if tjmax is not None]
        threshold = min(thresholds) if thresholds else _CPU_THERMAL_THRESHOLD_C

        over = [(ts, temp) for ts, temp, _ in samples if temp >= threshold]
        if not over:
            return None
        max_ts, max_temp = max(over, key=lambda item: item[1])
        severity = "error" if max_temp >= threshold + _CPU_THERMAL_HOT_MARGIN_C else "warning"
        # A single sample over threshold could be sensor noise; require at
        # least two hits (or the one hit being clearly hot) for full confidence.
        confidence = 1.0 if len(over) >= 2 or severity == "error" else 0.5

        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=(
                f"CPU package temperature reached {max_temp:.1f}C "
                f"({len(over)} sample(s) at/above {threshold:.1f}C) in this window"),
            evidence=[
                {"label": "max_temperature_c", "value": round(max_temp, 1),
                 "threshold": round(threshold, 1)},
                {"label": "samples_at_or_above_threshold", "value": len(over)},
            ],
            recommendations=[
                "Check cooling (fan curve, thermal paste, airflow) if this recurs "
                "across runs; a control action may already be capping frequency.",
            ],
            validation_steps=[
                "Correlate with a CONTROL_CPU_LIMIT_APPLIED event or a frequency drop "
                "in the same window to confirm throttling actually engaged.",
            ],
            time_window=context.get("window"),
        )


class KernelHealthWarning(InsightRule):
    """Surface confirmed kernel faults that require host-level investigation."""

    id = "kernel_health_warning"
    title = "Kernel health warning"
    category = "system_health"

    def evaluate(self, context):
        events = [event for event in (context.get("events") or [])
                  if event.get("event_type") in _KERNEL_HEALTH_EVENT_TYPES]
        if not events:
            return None
        event_types = sorted({event.get("event_type") for event in events})
        panic_detected = "PLATFORM_KERNEL_PANIC" in event_types
        return Finding(
            id=self.id, title=self.title,
            severity="critical" if panic_detected else "error", confidence=1.0,
            category=self.category,
            observation=(
                f"Detected {len(events)} kernel health fault(s) in this window: "
                f"{', '.join(event_types)}"),
            evidence=[
                {"label": "fault_count", "value": len(events)},
                {"label": "event_types", "value": event_types},
            ],
            recommendations=[
                "Inspect the kernel journal and preserve the affected workload logs before restarting "
                "or changing host configuration.",
            ],
            validation_steps=[
                "Confirm the journal cursor and boot identifier for each fault event.",
            ],
            related_event_ids=[event.get("event_id") for event in events if event.get("event_id")],
            time_window=context.get("window"),
        )


class ControlOscillation(InsightRule):
    """Detect repeated apply/recover switching of one control resource."""

    id = "control_oscillation"
    title = "Resource control is oscillating"
    category = "system_health"

    def evaluate(self, context):
        grouped = {}
        for event in context.get("control_actions") or context.get("events") or []:
            event_type = event.get("event_type") or ""
            if not event_type.startswith("CONTROL_"):
                continue
            if event_type.endswith("_LIMIT_APPLIED"):
                action = "applied"
            elif event_type.endswith("_LIMIT_RECOVERED"):
                action = "recovered"
            else:
                continue
            key = (event.get("protection_id") or event.get("app_id") or "unknown",
                   event.get("resource_type") or event_type.split("_LIMIT_")[0])
            grouped.setdefault(key, []).append((event, action))

        candidates = []
        for key, entries in grouped.items():
            entries.sort(key=lambda entry: (entry[0].get("ts_utc") or "", entry[0].get("event_id") or ""))
            transitions = sum(
                current[1] != previous[1] for previous, current in zip(entries, entries[1:]))
            if transitions >= _CONTROL_OSCILLATION_MIN_TRANSITIONS:
                candidates.append((transitions, key, entries))
        if not candidates:
            return None
        transitions, (scope, resource), entries = max(candidates, key=lambda item: item[0])
        return Finding(
            id=self.id, title=self.title, severity="warning", confidence=1.0,
            category=self.category,
            observation=(
                f"{resource} control for {scope} switched state {transitions} times "
                "in this window"),
            evidence=[
                {"label": "state_transitions", "value": transitions,
                 "threshold": _CONTROL_OSCILLATION_MIN_TRANSITIONS},
                {"label": "resource", "value": resource},
                {"label": "scope", "value": scope},
            ],
            recommendations=[
                "Review the pressure threshold and recovery hysteresis for this resource before "
                "the repeated changes affect workload stability.",
            ],
            validation_steps=[
                "Inspect the pressure trend and control lifecycle events around each switch.",
            ],
            related_event_ids=[event.get("event_id") for event, _ in entries if event.get("event_id")],
            time_window=context.get("window"),
        )


class IncompleteDiagnosticsWindow(InsightRule):
    """Detect persisted-monitor gaps that leave a context only partly observed."""

    id = "incomplete_diagnostics_window"
    title = "Diagnostics window has a telemetry gap"
    category = "collection_health"

    def evaluate(self, context):
        gaps = [gap for gap in _monitor_sample_gaps(context)
                if gap[2] > _DIAGNOSTICS_SAMPLE_GAP_SECONDS]
        if not gaps:
            return None
        _, _, largest_gap = max(gaps, key=lambda gap: gap[2])
        return Finding(
            id=self.id, title=self.title, severity="warning", confidence=0.8,
            category=self.category,
            observation=(
                f"Monitor telemetry has {len(gaps)} gap(s) longer than "
                f"{_DIAGNOSTICS_SAMPLE_GAP_SECONDS} seconds in this window"),
            evidence=[
                {"label": "gap_count", "value": len(gaps)},
                {"label": "largest_gap_seconds", "value": largest_gap,
                 "threshold": _DIAGNOSTICS_SAMPLE_GAP_SECONDS},
            ],
            recommendations=[
                "Treat correlations across the gap as incomplete and check the monitor service "
                "health before drawing a root-cause conclusion.",
            ],
            validation_steps=[
                "Confirm dynamic snapshot persistence resumed after the reported gap.",
            ],
            time_window=context.get("window"),
        )


class TelemetrySourceUnavailable(InsightRule):
    """Report explicit telemetry adapter failures without treating absent hardware as a fault."""

    id = "telemetry_source_unavailable"
    title = "Telemetry source is unavailable"
    category = "collection_health"

    def evaluate(self, context):
        failures = []
        for sample in _monitor_series(context):
            data = sample.get("data") or {}
            sources = (
                ("gpu", (data.get("gpu") or {}).get("gpu_usage") or {}),
                ("npu", (data.get("npu") or {}).get("npu_smi") or {}),
            )
            for source, status in sources:
                if status.get("available") is False and status.get("error"):
                    failures.append({"source": source, "error": str(status["error"]),
                                     "ts_epoch": sample.get("ts_epoch")})
        if not failures:
            return None
        latest_by_source = {}
        for failure in failures:
            latest_by_source[failure["source"]] = failure
        sources = sorted(latest_by_source)
        return Finding(
            id=self.id, title=self.title, severity="warning", confidence=0.9,
            category=self.category,
            observation=(
                f"{', '.join(sources).upper()} telemetry reported an adapter error "
                "in this window"),
            evidence=[
                {"label": "unavailable_sources", "value": sources},
                {"label": "latest_errors", "value": list(latest_by_source.values())},
            ],
            recommendations=[
                "Treat metrics from the affected source as unavailable and inspect the adapter "
                "or driver before using them for diagnosis.",
            ],
            validation_steps=[
                "Confirm a later dynamic snapshot reports the source as available without an error.",
            ],
            time_window=context.get("window"),
        )


_RULES = (
    MemoryGrowthRisk(), OomRisk(), ThermalThrottlingDetected(), KernelHealthWarning(),
    ControlOscillation(), IncompleteDiagnosticsWindow(), TelemetrySourceUnavailable(),
)


def evaluate_rules(context):
    """Run every registered rule against context, isolated and best-effort.
    Returns a list of Finding dicts (empty when nothing triggers)."""
    findings = []
    for rule in _RULES:
        try:
            finding = rule.evaluate(context)
        except Exception as exc:
            logger.warning("Insight rule %s failed: %s", getattr(rule, "id", rule), exc)
            continue
        if finding is not None:
            findings.append(asdict(finding))
    return findings
