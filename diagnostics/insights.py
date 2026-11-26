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
from datetime import datetime, timezone

from utils.logger import get_logger

logger = get_logger(__name__)

# Placeholder thermal/power thresholds until real per-SKU limits are sourced
# from hardware inventory / config_revision. Kept as named constants so the
# eventual wiring is a one-line change per rule, not a rule rewrite.
_CPU_THERMAL_THRESHOLD_C = 85.0
_CPU_THERMAL_HOT_MARGIN_C = 5.0

# Low-memory floors: the larger of an absolute amount and a share of RAM.
_MEMORY_AVAILABLE_LOW_GB = 1.0
_MEMORY_AVAILABLE_LOW_FRACTION = 0.05
_MEMORY_AVAILABLE_CRITICAL_GB = 0.5
_MEMORY_AVAILABLE_CRITICAL_FRACTION = 0.02

_MEM_GROWTH_MIN_SAMPLES = 3
_MEM_GROWTH_MIN_SPAN_SECONDS = 300  # need >=5 minutes of samples to trust a slope
_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN = 0.05  # ~3 GB/hour sustained decline
_MEM_GROWTH_EXHAUSTION_ERROR_MIN = 10
_MEM_GROWTH_EXHAUSTION_WARNING_MIN = 30
# A decline that reaches the low floor later than this is not reported.
_MEM_GROWTH_REPORT_HORIZON_MIN = 240
# The trend is fitted over the end of the window: at least this much of it.
_MEM_GROWTH_RECENT_SECONDS = 600

# Kernel/host faults -> advice severity (the most severe present wins); a type that also raises
# an alert uses its alert policy's severity (a test checks this). OOM kills are OomRisk's.
_KERNEL_HEALTH_EVENT_TYPES = {
    "PLATFORM_KERNEL_PANIC": "critical",
    "PLATFORM_HOST_UNCLEAN_SHUTDOWN": "error",
    "PLATFORM_KERNEL_OOPS": "critical",
    "PLATFORM_KERNEL_LOCKUP": "critical",
    "DEVICE_HARDWARE_ERROR_UNCORRECTED": "critical",
    "DEVICE_GPU_HANG": "critical",
    "PLATFORM_KERNEL_HUNG_TASK": "error",
    "DEVICE_STORAGE_IO_ERROR": "error",
    "PLATFORM_KERNEL_WARNING": "warning",
    "DEVICE_HARDWARE_ERROR_CORRECTED": "warning",
}
_KERNEL_HEALTH_ADVICE = {
    "PLATFORM_KERNEL_PANIC": "A kernel panic rebooted or halted the host: keep the pre-panic journal "
                             "(journalctl -b -1 -k) and check for a matching Oops, lockup or machine "
                             "check just before it.",
    "PLATFORM_HOST_UNCLEAN_SHUTDOWN": "The host stopped without a clean shutdown: look at the end of "
                                      "the previous boot's kernel log (journalctl -b -1 -k) and "
                                      "/var/lib/systemd/pstore for a panic, lockup or machine check, "
                                      "and rule out power loss or a forced reset.",
    "PLATFORM_KERNEL_OOPS": "The kernel oopsed; the host may be unstable until it reboots. Note the "
                            "function and module in the report -- a driver module (e.g. xe, i915, "
                            "intel_vpu) points at a driver or firmware update.",
    "PLATFORM_KERNEL_LOCKUP": "A CPU stopped scheduling (lockup/RCU stall). Check the stuck task and "
                              "module in the report; repeated lockups on one CPU suggest hardware or "
                              "firmware, on one module a driver bug.",
    "DEVICE_HARDWARE_ERROR_UNCORRECTED": "An uncorrected hardware error was reported: plan to service "
                                         "the reported component (CPU bank, DIMM, PCIe device) and do "
                                         "not trust results produced around it.",
    "DEVICE_GPU_HANG": "The GPU hung or was reset: check which process owned the timed-out job and "
                       "whether GPU work was lost; repeated hangs point at the driver/firmware or "
                       "the workload's kernels.",
    "PLATFORM_KERNEL_HUNG_TASK": "A task was stuck in uninterruptible sleep. The report's stack shows "
                                 "what it waited on: storage (fsync/writeback), a GPU fence, memory "
                                 "reclaim or a lock -- check that resource's pressure at that time.",
    "DEVICE_STORAGE_IO_ERROR": "A storage device reported I/O errors: check its health "
                               "(smartctl / nvme smart-log) and the filesystem before the next run; "
                               "a read-only remount needs immediate attention.",
    "PLATFORM_KERNEL_WARNING": "A driver or kernel WARN was logged. It is not fatal, but recurring "
                               "warnings from one module are worth reporting with the stack trace.",
    "DEVICE_HARDWARE_ERROR_CORRECTED": "Hardware corrected some errors. A rising rate on one device or "
                                       "memory controller is an early sign of failure; watch the count.",
}
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
# A limit re-applied this soon after it was lifted is a "quick re-limit"; this
# many of them for one app and resource is oscillation.
_CONTROL_OSCILLATION_RELIMIT_SECONDS = 600
_CONTROL_OSCILLATION_MIN_RELIMITS = 2
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


def _linear_trend(points):
    """Least-squares slope of ``[(ts_epoch, value)]`` per minute, and the fit's
    R² (0..1). ``(None, 0.0)`` for fewer than two distinct times."""
    if len(points) < 2:
        return None, 0.0
    n = float(len(points))
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in points)
    if var_t <= 0:
        return None, 0.0
    cov = sum((t - mean_t) * (v - mean_v) for t, v in points)
    slope = cov / var_t  # per second
    var_v = sum((v - mean_v) ** 2 for _, v in points)
    r2 = (cov * cov) / (var_t * var_v) if var_v > 0 else 0.0
    return slope * 60.0, max(0.0, min(1.0, r2))


def _memory_samples(context):
    """[(ts_epoch, available_gb)] oldest-first, skipping samples without it."""
    out = []
    for sample in _monitor_series(context):
        available_gb = ((sample.get("data") or {}).get("memory") or {}).get("available_gb")
        if available_gb is not None:
            out.append((sample.get("ts_epoch"), available_gb))
    return out


def _memory_total_gb(context):
    """Installed RAM from the newest sample that reports it, or None."""
    for sample in reversed(_monitor_series(context)):
        total = ((sample.get("data") or {}).get("memory") or {}).get("total_gb")
        if isinstance(total, (int, float)) and total > 0:
            return float(total)
    return None


def _memory_floors(context):
    """(low_gb, critical_gb) for this host: see _MEMORY_AVAILABLE_*."""
    total = _memory_total_gb(context)
    if total is None:
        return _MEMORY_AVAILABLE_LOW_GB, _MEMORY_AVAILABLE_CRITICAL_GB
    return (max(_MEMORY_AVAILABLE_LOW_GB, total * _MEMORY_AVAILABLE_LOW_FRACTION),
            max(_MEMORY_AVAILABLE_CRITICAL_GB, total * _MEMORY_AVAILABLE_CRITICAL_FRACTION))


def _occurrences(event):
    """Detections one stored event stands for (1 + its ``suppressed_since_last``)."""
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    try:
        return 1 + max(0, int(attributes.get("suppressed_since_last") or 0))
    except (TypeError, ValueError):
        return 1


def _cpu_temperature_samples(context):
    """[(ts_epoch, temperature_c, package_tjmax_c)] oldest-first."""
    out = []
    for sample in _monitor_series(context):
        cpu = (sample.get("data") or {}).get("cpu") or {}
        temp = cpu.get("temperature_c")
        if temp is not None:
            out.append((sample.get("ts_epoch"), temp, cpu.get("package_tjmax_c")))
    return out


def _monitor_gap_threshold(context):
    """Gap length (s) that counts as missing telemetry, allowing for a downsampled window's step."""
    monitor = (context.get("metrics") or {}).get("monitor") or {}
    step = monitor.get("step_seconds") or 0
    return 2 * step + _DIAGNOSTICS_SAMPLE_GAP_SECONDS


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
        samples = [(ts, gb) for ts, gb in _memory_samples(context) if isinstance(ts, (int, float))]
        if len(samples) < _MEM_GROWTH_MIN_SAMPLES:
            return None
        # Fit every sample at the end of the window, so a one-off drop (a model loading)
        # followed by a flat line is not read as a leak.
        last_ts = samples[-1][0]
        whole_span = last_ts - samples[0][0]
        recent_from = last_ts - max(_MEM_GROWTH_RECENT_SECONDS, whole_span / 3.0)
        recent = [(ts, gb) for ts, gb in samples if ts >= recent_from]
        span_seconds = recent[-1][0] - recent[0][0]
        if len(recent) < _MEM_GROWTH_MIN_SAMPLES or span_seconds < _MEM_GROWTH_MIN_SPAN_SECONDS:
            return None
        rate_gb_per_min, fit = _linear_trend(recent)
        tail_rate, _ = _linear_trend(recent[len(recent) // 2:])
        if rate_gb_per_min > -_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN:
            return None  # not declining fast enough to be worth flagging
        if tail_rate is None or tail_rate > -_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN / 2:
            return None  # it fell, but it is not falling any more
        first_gb = recent[0][1]
        last_gb = recent[-1][1]

        # Project to the low floor, not 0 GB: OOM kills come well before zero.
        floor_gb, _ = _memory_floors(context)
        projected_min = max(0.0, last_gb - floor_gb) / -rate_gb_per_min
        if projected_min > _MEM_GROWTH_REPORT_HORIZON_MIN:
            return None
        if projected_min <= _MEM_GROWTH_EXHAUSTION_ERROR_MIN:
            severity = "error"
        elif projected_min <= _MEM_GROWTH_EXHAUSTION_WARNING_MIN:
            severity = "warning"
        else:
            severity = "info"
        # As sure as the line fits the samples, and only with enough of them.
        confidence = round(min(0.9, fit * min(1.0, len(recent) / 10.0)), 2)

        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=(
                f"Available memory fell from {first_gb:.2f} GB to {last_gb:.2f} GB "
                f"over the last {span_seconds // 60:.0f} min of this window and is still falling"),
            evidence=[
                {"label": "available_gb_start", "value": round(first_gb, 2)},
                {"label": "available_gb_end", "value": round(last_gb, 2)},
                {"label": "trend_fit_r2", "value": round(fit, 2)},
                {"label": "decline_rate_gb_per_min", "value": round(rate_gb_per_min, 4),
                 "threshold": -_MEM_GROWTH_DECLINE_RATE_GB_PER_MIN},
                {"label": "projected_minutes_to_low_memory", "value": round(projected_min, 1),
                 "threshold": _MEM_GROWTH_REPORT_HORIZON_MIN},
                {"label": "low_memory_floor_gb", "value": round(floor_gb, 2)},
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
            return self._kills(context, oom_events)
        return self._low_headroom(context)

    def _kills(self, context, oom_events):
        def attributes(event):
            return event.get("attributes") if isinstance(event.get("attributes"), dict) else {}

        processes = sorted({attributes(e).get("process") for e in oom_events if attributes(e).get("process")})
        # ``cgroup_oom`` kills were forced by a cgroup's memory.max, not by the host running out.
        cgroup_kills = sum(1 for e in oom_events if attributes(e).get("cgroup_oom") is True)
        oomd_kills = sum(1 for e in oom_events if attributes(e).get("killer") == "systemd-oomd")
        host_kills = len(oom_events) - cgroup_kills - oomd_kills
        recommendations = []
        if oomd_kills:
            recommendations.append(
                "systemd-oomd killed a whole unit or scope under sustained memory pressure (or "
                "with swap exhausted) before the kernel ran out: reduce the workload's memory "
                "footprint, add swap, or tune the slice's ManagedOOM* settings.")
        if cgroup_kills:
            recommendations.append(
                "The kill was forced by a cgroup memory limit (memory.max of a container or "
                "systemd slice), not by the host running out: raise that limit or size the "
                "workload to it. SmarTune's own memory limits throttle (memory.high) and do "
                "not kill.")
        if host_kills:
            recommendations.append(
                "The host ran out of memory: reduce the workload's memory footprint or its "
                "concurrency, and compare the killed task in the report with the memory trend "
                "before the kill.")
        return Finding(
            id=self.id, title=self.title, severity="critical", confidence=1.0,
            category=self.category,
            observation=f"{len(oom_events)} process(es) or scope(s) were killed for memory in this "
                        f"window (kernel OOM killer or systemd-oomd)",
            evidence=[{"label": "oom_kill_count", "value": len(oom_events)},
                     {"label": "cgroup_limit_kills", "value": cgroup_kills},
                     {"label": "processes", "value": processes}],
            recommendations=recommendations,
            validation_steps=[
                "Confirm the killed process(es) match the intended workload, "
                "not an unrelated system service.",
            ],
            related_event_ids=[e.get("event_id") for e in oom_events if e.get("event_id")],
            time_window=context.get("window"),
        )

    def _low_headroom(self, context):
        # Judge the window's lowest point, not its last sample: a dip that recovered counts.
        samples = [(ts, gb) for ts, gb in _memory_samples(context) if isinstance(gb, (int, float))]
        if not samples:
            return None
        low_gb, critical_gb = _memory_floors(context)
        below = [gb for _, gb in samples if gb <= low_gb]
        if not below:
            return None
        min_gb = min(below)
        latest_gb = samples[-1][1]
        still_low = latest_gb <= low_gb
        if min_gb <= critical_gb:
            severity = "error" if still_low else "warning"
        else:
            severity = "warning" if still_low else "info"
        # A low reading predicts risk; it did not observe a kill. One sample may
        # be a transient dip.
        confidence = 0.7 if len(below) >= 2 else 0.5
        if still_low:
            observation = f"Available memory is {latest_gb:.2f} GB, close to exhaustion"
        else:
            observation = (f"Available memory dropped to {min_gb:.2f} GB in this window "
                           f"and has since recovered to {latest_gb:.2f} GB")
        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=observation,
            evidence=[{"label": "min_available_gb", "value": round(min_gb, 2),
                      "threshold": round(low_gb, 2)},
                      {"label": "latest_available_gb", "value": round(latest_gb, 2)},
                      {"label": "samples_below_threshold", "value": len(below)},
                      {"label": "critical_threshold_gb", "value": round(critical_gb, 2)},
                      {"label": "memory_total_gb", "value": _memory_total_gb(context)}],
            recommendations=[
                "Watch for an imminent OOM kill or a memory control action; "
                "consider reducing concurrent workload memory use proactively."
                if still_low else
                "Find what used the memory at the low point (per-app memory usage at that "
                "time); a repeat under the same workload is likely.",
            ],
            validation_steps=[
                "Re-check available_gb after a few minutes to confirm this is not a transient dip."
                if still_low else
                "Compare the low point with the workload and control actions running then.",
            ],
            time_window=context.get("window"),
        )


_THERMAL_VALIDATION = (
    "Compare the per-core frequency (per_core_freq_mhz) with the temperature in the same "
    "samples: clocks dropping while the package is hot confirm throttling engaged. A SmarTune "
    "CPU limit caps CPU time, not the clock, so it is not the cause; a device frequency cap "
    "(CONTROL_DEVICE_FREQ_LIMITED, `smartune control freq-status`) does lower the clock.")


def _cpu_freq_caps(context):
    """Device frequency caps on CPU policies recorded in the window."""
    caps = []
    for event in context.get("control_actions") or context.get("events") or []:
        if event.get("event_type") != "CONTROL_DEVICE_FREQ_LIMITED":
            continue
        attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
        units = [unit.strip() for unit in str(attributes.get("units") or "").split(",")]
        cpu_units = [unit for unit in units if unit.startswith("cpu:")]
        if cpu_units:
            caps.append({"units": cpu_units, "range": attributes.get("range"),
                         "event_id": event.get("event_id")})
    return caps


def _thermal_validation(caps):
    if not caps:
        return [_THERMAL_VALIDATION]
    ranges = "; ".join(f"{', '.join(cap['units'])} at {cap['range']}" for cap in caps)
    return [_THERMAL_VALIDATION,
            f"SmarTune capped the CPU frequency in this window ({ranges}): clocks at or below "
            f"that cap come from the cap, so only a drop below it points at throttling."]


class ThermalThrottlingDetected(InsightRule):
    """CPU package temperature at/above its throttle point during the window."""

    id = "thermal_throttling_detected"
    title = "CPU thermal throttling risk"
    category = "system_health"

    def evaluate(self, context):
        samples = _cpu_temperature_samples(context)
        # The kernel's throttle report counts even when no sampled temperature shows it.
        kernel_events = [event for event in (context.get("events") or [])
                         if event.get("event_type") == "DEVICE_CPU_THERMAL_THROTTLED"]
        if not samples and not kernel_events:
            return None
        # Prefer the sensor's own tjmax when a reading carries one; otherwise
        # fall back to the placeholder constant (see module docstring).
        thresholds = [tjmax for _, _, tjmax in samples if tjmax is not None]
        threshold = min(thresholds) if thresholds else _CPU_THERMAL_THRESHOLD_C

        kernel_reports = sum(_occurrences(event) for event in kernel_events)
        caps = _cpu_freq_caps(context)
        cap_evidence = [{"label": "cpu_frequency_caps", "value": caps}] if caps else []
        over = [(ts, temp) for ts, temp, _ in samples if temp >= threshold]
        if not over and kernel_events:
            return Finding(
                id=self.id, title=self.title, severity="warning", confidence=0.9,
                category=self.category,
                observation=(f"The kernel reported CPU clock throttling for temperature "
                             f"{kernel_reports} time(s) in this window"),
                evidence=[{"label": "kernel_throttle_events", "value": kernel_reports},
                          *cap_evidence],
                recommendations=[
                    "Check cooling (fan curve, thermal paste, airflow) and sustained CPU load in "
                    "this window; throttling lowers throughput and skews benchmark results.",
                ],
                validation_steps=_thermal_validation(caps),
                related_event_ids=[e.get("event_id") for e in kernel_events if e.get("event_id")],
                time_window=context.get("window"),
            )
        if not over:
            return None
        max_ts, max_temp = max(over, key=lambda item: item[1])
        known_limit = bool(thresholds)
        severity = "error" if max_temp >= threshold + _CPU_THERMAL_HOT_MARGIN_C else "warning"
        # A single sample over threshold could be sensor noise; require at
        # least two hits (or the one hit being clearly hot) for full confidence.
        confidence = 1.0 if len(over) >= 2 or severity == "error" else 0.5
        if not known_limit and not kernel_events:
            # Without the CPU's own throttle point the threshold is a guess: many
            # CPUs run at 90-100C under load by design.
            severity, confidence = "warning", min(confidence, 0.4)
        limit_text = f"{threshold:.1f}C" if known_limit else f"{threshold:.1f}C (assumed; the CPU's throttle point is unknown)"

        return Finding(
            id=self.id, title=self.title, severity=severity, confidence=confidence,
            category=self.category,
            observation=(
                f"CPU package temperature reached {max_temp:.1f}C "
                f"({len(over)} sample(s) at/above {limit_text}) in this window"),
            evidence=[
                {"label": "max_temperature_c", "value": round(max_temp, 1),
                 "threshold": round(threshold, 1)},
                {"label": "samples_at_or_above_threshold", "value": len(over)},
                *([{"label": "kernel_throttle_events", "value": kernel_reports}]
                  if kernel_events else []),
                *cap_evidence,
            ],
            recommendations=[
                "Check cooling (fan curve, thermal paste, airflow) if this recurs across runs; "
                "sustained heat lowers throughput and skews benchmark results.",
            ],
            validation_steps=_thermal_validation(caps),
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
        counts = {}
        for event in events:
            # Folded repeats (a GPU hang logs one line per stuck context) count too.
            counts[event.get("event_type")] = counts.get(event.get("event_type"), 0) + _occurrences(event)
        # Most severe type first: the dashboard shows the first recommendation as the next step.
        event_types = sorted(counts, key=lambda t: (-_SEVERITY_RANK[_KERNEL_HEALTH_EVENT_TYPES[t]], t))
        severity = _KERNEL_HEALTH_EVENT_TYPES[event_types[0]]
        summary = ", ".join(f"{t} x{counts[t]}" for t in event_types)
        observation = f"Detected {sum(counts.values())} kernel/host fault(s) in this window: {summary}"
        subjects = sorted({str((event.get("attributes") or {}).get("subject"))
                           for event in events
                           if isinstance(event.get("attributes"), dict)
                           and (event.get("attributes") or {}).get("subject")})
        return Finding(
            id=self.id, title=self.title,
            severity=severity, confidence=1.0,
            category=self.category,
            observation=observation,
            evidence=[
                {"label": "fault_count", "value": sum(counts.values())},
                {"label": "event_types", "value": event_types},
                {"label": "counts", "value": counts},
                *([{"label": "subjects", "value": subjects[:10]}] if subjects else []),
            ],
            recommendations=[_KERNEL_HEALTH_ADVICE[t] for t in event_types] + [
                "Inspect the kernel journal and preserve the affected workload logs before restarting "
                "or changing host configuration.",
            ],
            validation_steps=[
                "Confirm the journal cursor and boot identifier for each fault event.",
            ],
            related_event_ids=[event.get("event_id") for event in events if event.get("event_id")],
            time_window=context.get("window"),
        )


def _event_epoch(event):
    """Epoch of an event's ts_utc (the strings mix UTC offsets, so they do not compare as text)."""
    value = event.get("ts_utc")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class ControlOscillation(InsightRule):
    """An automatic limit that is lifted and re-applied again and again soon after.

    Grouped per app and resource, not per protection id: a new limit after a full restore
    gets a new protection id. Manual limits are left out."""

    id = "control_oscillation"
    title = "Resource control is oscillating"
    category = "system_health"

    def evaluate(self, context):
        from diagnostics import event_catalog

        grouped = {}
        for event in context.get("control_actions") or context.get("events") or []:
            definition = event_catalog.lifecycle_event(event.get("event_type"))
            if definition is None or definition.action not in ("applied", "recovered"):
                continue
            attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
            if attributes.get("control_mode") == "manual":
                continue
            ts = _event_epoch(event)
            if ts is None:
                continue
            key = (event.get("app_id") or event.get("protection_id") or "unknown",
                   event.get("resource_type") or definition.resource)
            grouped.setdefault(key, []).append((ts, event, definition.action))

        candidates = []
        for key, entries in grouped.items():
            entries.sort(key=lambda entry: (entry[0], entry[1].get("event_id") or ""))
            transitions = 0
            relimits = []  # seconds each quick re-limit came after its release
            for previous, current in zip(entries, entries[1:]):
                if previous[2] == current[2]:
                    continue
                transitions += 1
                gap = current[0] - previous[0]
                if (previous[2], current[2]) == ("recovered", "applied") \
                        and gap <= _CONTROL_OSCILLATION_RELIMIT_SECONDS:
                    relimits.append(gap)
            if len(relimits) >= _CONTROL_OSCILLATION_MIN_RELIMITS:
                candidates.append((len(relimits), transitions, relimits, key, entries))
        if not candidates:
            return None
        count, transitions, relimits, (app_id, resource), entries = max(
            candidates, key=lambda item: (item[0], item[1]))
        names = [(event.get("attributes") or {}).get("app_name") for _, event, _ in entries
                 if isinstance(event.get("attributes"), dict)]
        subject = next((name for name in reversed(names) if name), None) or app_id
        lifecycles = len({event.get("protection_id") for _, event, _ in entries
                          if event.get("protection_id")})
        window_min = _CONTROL_OSCILLATION_RELIMIT_SECONDS // 60
        return Finding(
            id=self.id, title=self.title,
            severity="warning",
            # Twice is a pattern; more than that leaves little doubt.
            confidence=0.7 if count < _CONTROL_OSCILLATION_MIN_RELIMITS + 1 else 0.9,
            category=self.category,
            observation=(
                f"The automatic {resource} limit on {subject} was re-applied {count} times "
                f"within {window_min} min of being lifted in this window (soonest after "
                f"{min(relimits):.0f} s)"),
            evidence=[
                {"label": "quick_relimits", "value": count,
                 "threshold": _CONTROL_OSCILLATION_MIN_RELIMITS},
                {"label": "shortest_release_seconds", "value": round(min(relimits)),
                 "threshold": _CONTROL_OSCILLATION_RELIMIT_SECONDS},
                {"label": "state_transitions", "value": transitions},
                {"label": "limit_lifecycles", "value": lifecycles},
                {"label": "resource", "value": resource},
                {"label": "app_id", "value": app_id},
            ],
            recommendations=[
                "The limit is lifted while the pressure that caused it returns soon after: widen "
                "the recovery hysteresis (a lower restore threshold or a longer hold before "
                "restoring) for this resource, or keep a lasting manual limit on this app.",
            ],
            validation_steps=[
                "Inspect the pressure trend and control lifecycle events around each switch.",
            ],
            related_event_ids=[event.get("event_id") for _, event, _ in entries if event.get("event_id")],
            time_window=context.get("window"),
        )


# Causes of a telemetry gap, most telling first; any but "unexplained" is downtime.
_GAP_CAUSE_ORDER = ("crashed", "stopped", "restarted")
_GAP_CAUSE_TEXT = {
    "crashed": "SmarTune crashed",
    "stopped": "SmarTune was stopped",
    "restarted": "SmarTune restarted",
    "unexplained": "unexplained",
}
_GAP_LIFECYCLE_SLACK_SECONDS = 60


def _in_gap(event, gap):
    """Whether a lifecycle event falls in a (previous_ts, current_ts, length) gap, with slack."""
    ts = _event_epoch(event)
    previous, current, _ = gap
    return ts is not None and \
        previous - _GAP_LIFECYCLE_SLACK_SECONDS <= ts <= current + _GAP_LIFECYCLE_SLACK_SECONDS


def _gap_cause(gap, lifecycle):
    """Cause of one gap: see _GAP_CAUSE_ORDER."""
    from diagnostics import service_lifecycle

    kinds = {service_lifecycle.CRASHED: "crashed", service_lifecycle.STOPPED: "stopped",
             service_lifecycle.STARTED: "restarted"}
    seen = {kinds[e.get("event_type")] for e in lifecycle
            if e.get("event_type") in kinds and _in_gap(e, gap)}
    return next((cause for cause in _GAP_CAUSE_ORDER if cause in seen), "unexplained")


class IncompleteDiagnosticsWindow(InsightRule):
    """Detect persisted-monitor gaps while SmarTune was up, which leave a context partly observed.

    Gaps with a SmarTune stop, crash or restart inside are downtime and are only counted."""

    id = "incomplete_diagnostics_window"
    title = "Diagnostics window has a telemetry gap"
    category = "collection_health"

    def evaluate(self, context):
        threshold = _monitor_gap_threshold(context)
        gaps = [gap for gap in _monitor_sample_gaps(context) if gap[2] > threshold]
        if not gaps:
            return None
        lifecycle = context.get("service_lifecycle")
        if lifecycle is None:  # a context assembled without it: look in the events
            from diagnostics import service_lifecycle

            types = (service_lifecycle.STARTED, service_lifecycle.STOPPED, service_lifecycle.CRASHED)
            lifecycle = [e for e in context.get("events") or [] if e.get("event_type") in types]
        causes = [_gap_cause(gap, lifecycle) for gap in gaps]
        counts = {cause: causes.count(cause) for cause in dict.fromkeys(causes)}
        unexplained = counts.get("unexplained", 0)
        if not unexplained:
            return None
        largest_gap = max(gap[2] for gap in gaps)
        breakdown = ", ".join(f"{count} {_GAP_CAUSE_TEXT[cause]}" for cause, count in counts.items())
        return Finding(
            id=self.id, title=self.title,
            severity="warning",
            confidence=0.8,
            category=self.category,
            observation=(
                f"Monitor telemetry has {len(gaps)} gap(s) longer than "
                f"{threshold} seconds in this window ({breakdown})"),
            evidence=[
                {"label": "gap_count", "value": len(gaps)},
                {"label": "unexplained_gap_count", "value": unexplained},
                {"label": "gap_causes", "value": counts},
                {"label": "largest_gap_seconds", "value": largest_gap,
                 "threshold": threshold},
            ],
            recommendations=[
                "SmarTune was running through the unexplained gap(s) but stored no snapshots: "
                "treat correlations across them as incomplete and check the monitor collector "
                "(its log, disk space, database locks) before drawing a root-cause conclusion.",
            ],
            validation_steps=["Confirm dynamic snapshot persistence resumed after the reported gap."],
            related_event_ids=[e.get("event_id") for e in lifecycle if e.get("event_id")
                               and any(_in_gap(e, gap) for gap in gaps)],
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


class ProcessCrashes(InsightRule):
    """Processes killed by a fatal signal, grouped by program and faulting object."""

    id = "process_crashes"
    title = "Processes crashed"
    category = "system_health"

    def evaluate(self, context):
        events = [event for event in (context.get("events") or [])
                  if event.get("event_type") == "PLATFORM_PROCESS_CRASHED"]
        if not events:
            return None
        groups = {}
        for event in events:
            attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
            key = (attributes.get("process") or "?", attributes.get("object") or "?")
            # Folded repeats (gate) still count as crashes.
            groups[key] = groups.get(key, 0) + _occurrences(event)
        total = sum(groups.values())
        ranked = sorted(groups.items(), key=lambda item: -item[1])
        (top_process, top_object), top_count = ranked[0]
        repeated = top_count >= 3
        lines = [f"{process} in {obj} x{count}" for (process, obj), count in ranked[:5]]
        recommendations = []
        if ".so" in top_object.rsplit("/", 1)[-1]:
            recommendations.append(
                f"{top_process} crashed inside {top_object}: check that library's version against "
                f"the workload's requirements (runtime/driver/plugin mismatch is the usual cause) and "
                f"capture a core dump of the next crash.")
        recommendations.append(
            "Correlate each crash with the app/job running at that time and its own log; a crash "
            "loop usually means a bad input, model or library version rather than the host.")
        return Finding(
            id=self.id, title=self.title,
            severity="error" if repeated else "warning",
            confidence=1.0 if repeated else 0.8,
            category=self.category,
            observation=f"{total} process crash(es) in this window: {'; '.join(lines)}",
            evidence=[
                {"label": "crash_count", "value": total},
                {"label": "by_process_and_object", "value": lines},
            ],
            recommendations=recommendations,
            validation_steps=[
                "Open the crash report in Evidence search (keyword: the process name) to see the "
                "faulting instruction and library offset.",
            ],
            related_event_ids=[event.get("event_id") for event in events if event.get("event_id")],
            time_window=context.get("window"),
        )


_RULES = (
    MemoryGrowthRisk(), OomRisk(), ThermalThrottlingDetected(), KernelHealthWarning(),
    ProcessCrashes(), ControlOscillation(), IncompleteDiagnosticsWindow(), TelemetrySourceUnavailable(),
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
