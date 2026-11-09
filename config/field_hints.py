# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Static guidance for the Settings page, served by GET /monitor/config-hints.

Each entry describes how the UI should help a user fill one field: the legal range,
a suggested step, optional preset levels for fields people cannot reason about in raw
numbers, and what moving the value up or down does.  There is deliberately no default
value here: the default is whatever the backend was started with (see
monitor_api._startup_defaults), so it follows config.yaml instead of duplicating it.

Keys are "<section>.<form field path>" using the names the Settings forms use.  A "*"
stands for exactly one path segment, so one entry can cover a whole priority or media row.

Entry fields:
  kind     "number" | "preset"
  min/max/step/unit/integer   input constraints (all optional)
  presets  [{"label", "value", "desc"}] for "preset"
  custom   preset kind only: also allow typing an arbitrary value
  up/down  one line each on what raising / lowering the value does
  note     optional caveat shown under the field
  apply    when a saved change takes effect, if that is not "immediately"
"""

from typing import Any, Dict

_PRIORITY_NOTE = "Keep High > Medium > Low > Undefined so lower-priority apps are held harder."

_LEVELS = ("low", "medium", "high", "critical")


def _num(min=None, max=None, step=1, unit="", up="", down="", note="", apply="", integer=False) -> Dict[str, Any]:
    hint: Dict[str, Any] = {"kind": "number", "step": step, "up": up, "down": down}
    for key, val in (("min", min), ("max", max), ("unit", unit), ("note", note), ("apply", apply)):
        if val not in (None, ""):
            hint[key] = val
    if integer:
        hint["integer"] = True
    return hint


def _preset(presets, min=None, max=None, step=1, unit="", up="", down="", note="", apply="") -> Dict[str, Any]:
    hint = _num(min=min, max=max, step=step, unit=unit, up=up, down=down, note=note, apply=apply)
    hint.update(kind="preset", presets=presets, custom=True)
    return hint


def _level_thresholds(prefix: str, what: str, note: str) -> Dict[str, Dict[str, Any]]:
    return {
        f"{prefix}.{level}": _num(
            min=1, max=100, step=5, unit="%", note=note,
            up=f"Higher: {what} reaches {level} later, so alerts and control fire less often.",
            down=f"Lower: {what} reaches {level} sooner, so alerts and control fire more often.",
        )
        for level in _LEVELS
    }


_RATE_APPLY = ("Applies the next time a limit is set; limits already in force keep their old "
               "value until the next limit or restore step.")

FIELD_HINTS: Dict[str, Dict[str, Any]] = {
    # ---- system_pressure ----------------------------------------------------------------
    "system_pressure.regular_update_sys_pressure_time": _preset(
        [
            {"label": "1 s", "value": 1, "desc": "Fastest reaction, highest sampling cost"},
            {"label": "2 s", "value": 2, "desc": "Fast"},
            {"label": "5 s", "value": 5, "desc": "Balanced"},
            {"label": "10 s", "value": 10, "desc": "Low overhead, slower reaction"},
            {"label": "30 s", "value": 30, "desc": "Very low overhead"},
            {"label": "60 s", "value": 60, "desc": "Coarse monitoring only"},
        ],
        min=1, max=3600, step=1, unit="s",
        up="Higher: less CPU spent sampling, but pressure changes are noticed later.",
        down="Lower: faster reaction to pressure, at the cost of more sampling work.",
        apply="Takes effect once the current wait ends, so a large old value delays the change.",
    ),
    **_level_thresholds(
        "system_pressure.thresholds", "system pressure",
        "Levels must stay in order: low < medium < high < critical. Critical is what triggers throttling.",
    ),
    "system_pressure.weights.*": _num(
        min=0, step=1, integer=True,
        up="Higher: this resource contributes a larger share of the overall score.",
        down="Lower: this resource contributes less; 0 removes it from the score.",
        note="Only the ratio between the three weights matters, not their absolute size.",
    ),
    "system_pressure.mem_gate_steepness": _preset(
        [
            {"label": "Gentle", "value": 4, "desc": "Wide, soft transition around the busy point"},
            {"label": "Standard", "value": 8, "desc": "Spreads low to critical over about 12 points of memory use"},
            {"label": "Sharp", "value": 16, "desc": "Narrow transition; can skip the medium level on a fast ramp"},
        ],
        min=1, max=50, step=0.5,
        up="Higher: memory pressure jumps more abruptly near the busy threshold; medium may be skipped.",
        down="Lower: memory pressure rises more gradually, so limits start earlier and ease in.",
    ),
    "system_pressure.memory_busy_threshold": _num(
        min=0, max=100, step=5, unit="%",
        up="Higher: memory is treated as busy later, so memory pressure and limits start later.",
        down="Lower: memory is treated as busy earlier, so memory pressure and limits start sooner.",
    ),
    "system_pressure.cpu_busy_threshold": _num(
        min=0, max=100, step=5, unit="%",
        up="Higher: CPU is treated as busy later, so CPU pressure builds later.",
        down="Lower: CPU is treated as busy earlier, so CPU pressure builds sooner.",
    ),

    # ---- disk_pressure ------------------------------------------------------------------
    **_level_thresholds(
        "disk_pressure.disk_thresholds", "disk pressure",
        "Levels must stay in order. Critical on this channel is what authorises an io.max cap.",
    ),
    "disk_pressure.sub_weights.*": _num(
        min=0, max=1, step=0.05,
        up="Higher: this signal drives a disk's pressure score more.",
        down="Lower: this signal matters less to a disk's pressure score.",
        note="The three weights must add up to at most 1. Latency is what users feel, so keep it the largest.",
    ),
    "disk_pressure.sigmoid_k": _preset(
        [
            {"label": "Gentle", "value": 4, "desc": "Gradual ramp around each signal's half-point"},
            {"label": "Standard", "value": 8, "desc": "Balanced ramp"},
            {"label": "Sharp", "value": 16, "desc": "Switch-like: near-idle below the half-point, near-saturated above"},
        ],
        min=1, max=50, step=0.5,
        up="Higher: disk pressure flips more abruptly around the half-point.",
        down="Lower: disk pressure rises more gradually and reads higher at light load.",
    ),
    "disk_pressure.max_p_weight": _preset(
        [
            {"label": "Standard", "value": 0.8, "desc": "Lowest value that keeps io.max reachable on any disk count"},
            {"label": "Worst-disk led", "value": 0.9, "desc": "The busiest disk counts for more"},
            {"label": "Worst disk decides", "value": 1.0, "desc": "The busiest disk alone sets the score"},
        ],
        min=0, max=1, step=0.05,
        up="Higher: one hammered disk among many raises the score more.",
        down="Lower: a hammered disk is averaged away by idle ones.",
        note="Below about 0.8, machines with several disks may never reach critical, so io.max never triggers.",
    ),

    # ---- network_pressure ---------------------------------------------------------------
    **_level_thresholds(
        "network_pressure.network_thresholds", "network pressure",
        "Levels must stay in order. The level drives pressure-based bandwidth shaping.",
    ),
    "network_pressure.network_pressure_model_rx_drop_weight": _num(
        min=0, max=1, step=0.05,
        up="Higher: dropped received packets push network pressure up more.",
        down="Lower: dropped received packets matter less; 0 ignores them.",
        note="Generic rx_dropped is driver-specific, which is why it counts at reduced strength.",
    ),

    # ---- limit_policy -------------------------------------------------------------------
    "limit_policy.cpu.rate.*": _num(
        min=1, max=100, step=5, unit="%", note=_PRIORITY_NOTE, apply=_RATE_APPLY,
        up="Higher: apps at this priority keep more CPU while throttled.",
        down="Lower: apps at this priority are held to a smaller CPU share.",
    ),
    "limit_policy.memory.rate.*": _num(
        min=1, max=100, step=5, unit="%", note=_PRIORITY_NOTE, apply=_RATE_APPLY,
        up="Higher: apps at this priority may use more memory before being pushed back.",
        down="Lower: apps at this priority are pushed back at a smaller memory share.",
    ),
    "limit_policy.disk_io.rate.*.write": _num(
        min=1, step=10, unit="MB/s", note="Calibrated for NVMe; see Per-disk adjustment for slower media.",
        apply=_RATE_APPLY,
        up="Higher: a throttled app may write faster.",
        down="Lower: a throttled app's writes are held back harder.",
    ),
    "limit_policy.disk_io.rate.*.read": _num(
        min=1, step=10, unit="MB/s", note="Calibrated for NVMe; see Per-disk adjustment for slower media.",
        apply=_RATE_APPLY,
        up="Higher: a throttled app may read faster.",
        down="Lower: a throttled app's reads are held back harder.",
    ),
    "limit_policy.disk_io.rate.*.write_iops": _num(
        min=1, step=500, unit="IOPS", apply=_RATE_APPLY,
        up="Higher: more small writes per second are allowed.",
        down="Lower: small random writes are held back harder.",
    ),
    "limit_policy.disk_io.rate.*.read_iops": _num(
        min=1, step=500, unit="IOPS", apply=_RATE_APPLY,
        up="Higher: more small reads per second are allowed.",
        down="Lower: small random reads are held back harder.",
    ),
    "limit_policy.disk_io.media_scale.*": _num(
        min=0.01, max=1, step=0.05, unit="x", apply=_RATE_APPLY,
        up="Higher: the cap on this media type is closer to the NVMe rate.",
        down="Lower: slower media get a proportionally smaller cap.",
        note="Written cap = rate x this factor. Use 1.0 for NVMe and lower values for slower media.",
    ),
    "limit_policy.disk_io.candidate_floor.*.mb_s": _num(
        min=0.1, step=1, unit="MB/s",
        up="Higher: only heavier apps are considered for throttling on this media.",
        down="Lower: lighter apps become throttle candidates too.",
        note="Either the bandwidth or the IOPS floor being met qualifies an app.",
    ),
    "limit_policy.disk_io.candidate_floor.*.iops": _num(
        min=1, step=50, unit="IOPS",
        up="Higher: only apps issuing more I/O operations are considered for throttling on this media.",
        down="Lower: apps with fewer I/O operations become throttle candidates too.",
        note="Either the bandwidth or the IOPS floor being met qualifies an app.",
    ),

    # ---- network_control ----------------------------------------------------------------
    "network_control.network_pps_policy.app_pps_high": _num(
        min=1, step=5000, unit="PPS", integer=True,
        up="Higher: only apps sending more packets per second can be picked for flood protection.",
        down="Lower: lighter senders can be picked too, which risks limiting a normal app.",
        note="Also the upper bound for the per-app cap.",
    ),
    "network_control.network_pps_policy.pps_cap_rate": _num(
        min=1, step=5000, unit="PPS", integer=True,
        up="Higher: a limited app may keep sending faster.",
        down="Lower: a limited app is held to a smaller packet rate.",
        note="Must not exceed the candidate threshold.",
    ),
    "network_control.network_pps_policy.pps_cap_burst": _num(
        min=1, step=500, unit="packets", integer=True,
        up="Higher: short bursts above the cap are tolerated before packets are dropped.",
        down="Lower: bursts are dropped sooner, so the cap is enforced more strictly.",
    ),
    "network_control.config_network_bw.*.min": _num(
        min=0, max=100, step=5, unit="%",
        up="Higher: this priority is guaranteed more of the link under contention.",
        down="Lower: this priority may be squeezed harder by higher priorities.",
        note="Application minimums plus the system reservation cannot exceed 100%.",
    ),
    "network_control.config_network_bw.*.max": _num(
        min=0, max=100, step=5, unit="%",
        up="Higher: this priority may use more of the link when it is free.",
        down="Lower: this priority is capped harder even when the link is idle.",
        note="Keep it at or above the minimum of the same priority.",
    ),
}

# Per-section behaviour of the "restore startup defaults" button.  Sections whose content
# is user-authored or hardware-derived are not restorable, or keep part of their form.
SECTION_HINTS: Dict[str, Dict[str, Any]] = {
    "system_pressure": {"restorable": True},
    "disk_pressure": {"restorable": True},
    "network_pressure": {"restorable": True},
    "limit_policy": {"restorable": True},
    "network_control": {
        "restorable": True,
        "keep": ["available_network_interfaces", "selected_network_interfaces", "network_system_ports"],
    },
    "diagnostic_events": {"restorable": False},
    "diagnostic_rules": {"restorable": False},
}
