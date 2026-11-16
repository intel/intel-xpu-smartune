# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""One-click "relax / tighten" stepping for the three pressure models.

Each pressure (system, disk, network) owns a set of calibration parameters.  A click on
Relax / Tighten moves every one of them one step in the direction that makes the pressure
read lower / higher.  The module is pure: it takes the current values and the factory
values and returns proposed values; it never reads or writes the live config.

Model
-----
* Every parameter has a step rule that maps a *step index* ``x`` (0 = factory value, +1 per
  Relax click) to a value ``v``.  Three rules, each with a closed-form inverse:
    mult(r)         v = v0 * r**x                       (r > 1: Relax raises, r < 1: lowers)
    headroom(B, q)  v = B - (B - v0) * q**x             (Relax raises toward ceiling B,
                                                         never reaching it)
    add(s)          v = v0 + s * x
* Nothing about the position is stored.  A click first inverts the *current* value into its
  step index, then moves one index.  So the position survives restarts and manual edits,
  and clicking Relax then Tighten returns to the same value (barring a bound).
* Bounds are absolute -- expressed against the *factory* value, never the moving one -- so
  headroom does not shift when the user edits a value by hand.  A bound only stops further
  movement in its direction; it never pulls a hand-edited value back.
* Level thresholds and the cross-resource weights are deliberately not tuned here (see the
  design doc): a threshold would cancel the sensitivity change, and weights are shares.
  Parameters whose adjustment would not change sensitivity (loss detectors, hardware
  estimates, per-media calibration tables) stay in code / config and are not listed.

Paths are dotted names into the config, e.g. ``disk_pressure_model.await_half_ms.hdd``.
"""

import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# Stand-in for an unbounded step index (value at/over a headroom ceiling).
_FAR = 1e6

RELAX = "relax"
TIGHTEN = "tighten"


@dataclass(frozen=True)
class Param:
    path: str
    label: str
    rule: str                      # "mult" | "headroom" | "add"
    step: float                    # r (mult), q (headroom) or s (add)
    ceiling: Optional[float] = None  # headroom only: the value that is never reached
    lo: Optional[float] = None     # absolute lower bound
    hi: Optional[float] = None     # absolute upper bound
    lo_x: Optional[float] = None   # lower bound as a multiple of the factory value
    hi_x: Optional[float] = None   # upper bound as a multiple of the factory value
    # Some values only mean something relative to another setting.  When set, the rule and
    # the bounds act on ``value / norm(values)``, and the stored value follows ``norm`` as
    # that other setting moves -- so stepping one parameter does not silently break another.
    norm: Optional[Callable[[Dict[str, float]], float]] = None
    why_lo: str = ""
    why_hi: str = ""

    @property
    def up_is_relax(self) -> bool:
        """True when a Relax click raises the value."""
        if self.rule == "headroom":
            return True
        return self.step > 1 if self.rule == "mult" else self.step > 0


# --------------------------------------------------------------------------------------
# Step rules
# --------------------------------------------------------------------------------------
def _forward(p: Param, v0: float, x: float) -> float:
    if p.rule == "mult":
        return v0 * (p.step ** x)
    if p.rule == "headroom":
        return p.ceiling - (p.ceiling - v0) * (p.step ** x)
    return v0 + p.step * x


def _inverse(p: Param, v0: float, v: float) -> Optional[float]:
    """Step index of value ``v``; None when the rule cannot represent it."""
    try:
        if p.rule == "mult":
            if v0 <= 0 or v <= 0:
                return None
            return math.log(v / v0) / math.log(p.step)
        if p.rule == "headroom":
            if p.ceiling - v0 <= 0:
                return None
            room = p.ceiling - v
            if room <= 0:
                return _FAR
            return math.log(room / (p.ceiling - v0)) / math.log(p.step)
        return (v - v0) / p.step
    except (ValueError, ZeroDivisionError):
        return None


def _round(v: float) -> float:
    return float(f"{v:.4g}")


def _bounds(p: Param, v0: float) -> Tuple[Optional[float], Optional[float]]:
    """Bounds in the same space as ``v0`` (normalised when ``p.norm`` is set)."""
    lo = p.lo if p.lo is not None else (p.lo_x * v0 if p.lo_x is not None else None)
    hi = p.hi if p.hi is not None else (p.hi_x * v0 if p.hi_x is not None else None)
    return lo, hi


def _tuned(p: Param, values: Dict[str, float], value: Optional[float] = None) -> float:
    """``value`` (default: the parameter's own) expressed in the space the rule acts on."""
    v = float(values[p.path] if value is None else value)
    return v / p.norm(values) if p.norm else v


# --------------------------------------------------------------------------------------
# Parameter tables
# --------------------------------------------------------------------------------------
def _mem_free(values: Dict[str, float]) -> float:
    """Memory left (in % points) when the memory gate reads busy."""
    return max(1.0, 100.0 - float(values.get("memory_busy_threshold", 80.0)))


_SYSTEM: List[Param] = [
    Param("memory_busy_threshold", "Memory busy threshold", "headroom", 0.85,
          ceiling=100.0, lo=60.0, hi=97.0,
          why_lo="below 60% the memory gate would start reacting at normal usage",
          why_hi="above 97% almost no memory is left by the time it reads as busy"),
    Param("cpu_busy_threshold", "CPU busy threshold", "headroom", 0.8,
          ceiling=100.0, lo=70.0, hi=98.0,
          why_lo="below 70% a normally loaded CPU would be flagged busy",
          why_hi="above 98% the CPU would almost never be flagged busy"),
    # The `medium` band is ~0.98 * free / k memory points wide, so the tuned quantity is
    # k / free (the inverse of that width).  0.12..0.65 keeps the band between ~8 and
    # ~1.5 points: narrower and a fast ramp skips `medium`, wider and the gate is too flat
    # to tell busy from idle.  Moving the busy threshold carries k along (see Param.norm).
    Param("mem_gate_steepness", "Memory gate steepness", "mult", 1.1,
          norm=_mem_free, lo=0.12, hi=0.65,
          why_lo="a flatter curve can no longer tell busy from idle memory",
          why_hi="a steeper curve can jump past the medium level on a fast ramp"),
    Param("system_score_ewma_alpha", "System score smoothing", "mult", 0.85,
          lo=0.1, hi=0.6,
          why_lo="slower smoothing would leave the level lagging far behind real load",
          why_hi="faster smoothing would let momentary spikes flip the level"),
]

_DISK: List[Param] = [
    # One factor on every await/queue half-point.  The per-media tables themselves stay in
    # config: they are calibrated per device class, and a single scale is all a click needs.
    Param("disk_pressure_model.half_point_scale", "Disk half-point scale", "mult", 1.25,
          lo=0.5, hi=4.0,
          why_lo="lower half-points would flag normal latency and queues as pressure",
          why_hi="higher half-points would hide genuinely slow or backed-up IO"),
    Param("disk_pressure_model.sub_weights.latency", "Latency weight", "mult", 0.9,
          lo_x=0.6, hi_x=1.0,
          why_lo="lower weights would let a busy disk barely register",
          why_hi="the three sub-weights must sum to at most 1"),
    Param("disk_pressure_model.sub_weights.queue", "Queue weight", "mult", 0.9,
          lo_x=0.6, hi_x=1.0,
          why_lo="lower weights would let a busy disk barely register",
          why_hi="the three sub-weights must sum to at most 1"),
    Param("disk_pressure_model.sub_weights.util", "Utilisation weight", "mult", 0.9,
          lo_x=0.6, hi_x=1.0,
          why_lo="lower weights would let a busy disk barely register",
          why_hi="the three sub-weights must sum to at most 1"),
    Param("disk_pressure_model.sigmoid_k", "Disk curve steepness", "mult", 1.15,
          lo=4.0, hi=16.0,
          why_lo="a flatter curve can no longer separate light from heavy IO",
          why_hi="a steeper curve turns pressure into an on/off switch"),
    Param("disk_pressure_model.max_p_weight", "Worst-disk weight", "add", -0.04,
          lo=0.5, hi=1.0,
          why_lo="below 0.5 one saturated disk among many is averaged away",
          why_hi="1.0 already lets the worst disk decide alone"),
    Param("disk_psi_weights.some", "IO stall weight (some)", "mult", 0.85,
          lo_x=0.5, hi_x=1.5,
          why_lo="a lower weight would mute real IO stalls",
          why_hi="a higher weight would let light stalls dominate"),
    Param("disk_psi_weights.full", "IO stall weight (full)", "mult", 0.85,
          lo_x=0.5, hi_x=1.5,
          why_lo="a lower weight would mute real IO stalls",
          why_hi="a higher weight would let light stalls dominate"),
    Param("disk_score_ewma_alpha", "Disk score smoothing", "mult", 0.85,
          lo=0.1, hi=0.6,
          why_lo="slower smoothing would leave the level lagging far behind real load",
          why_hi="faster smoothing would let momentary spikes flip the level"),
]

# Not tuned, on purpose: the loss detectors (drop / fifo / softnet half-points) and the
# hardware estimates (min_pps_activity, cpu_pps_per_core).  Relaxing them would hide real
# loss rather than change how sensitive the model is, so they stay in code.
_NETWORK: List[Param] = [
    Param("network_pressure_model.rx_drop_weight", "RX drop weight", "mult", 0.8,
          lo=0.1, hi=0.6,
          why_lo="a lower weight would ignore real receive-side loss",
          why_hi="generic rx drops are weak evidence and should not dominate"),
    Param("network_pressure_model.util_sat_half", "Link utilisation half-point", "headroom", 0.8,
          ceiling=1.0, lo=0.90, hi=0.995,
          why_lo="below 0.90 a link with headroom would read as saturated",
          why_hi="above 0.995 a saturated link would hardly register"),
    Param("network_pressure_model.util_sat_k", "Link curve steepness", "mult", 1.2,
          lo=60.0, hi=240.0,
          why_lo="a flatter curve can no longer separate busy from saturated",
          why_hi="a steeper curve turns pressure into an on/off switch"),
    Param("network_pressure_model.ema_alpha", "Utilisation smoothing", "mult", 0.85,
          lo=0.1, hi=0.6,
          why_lo="slower smoothing would leave the score lagging far behind real load",
          why_hi="faster smoothing would let momentary spikes dominate"),
    Param("network_pressure_model.decay_rate", "Distress retention", "add", -0.03,
          lo=0.4, hi=0.9,
          why_lo="faster forgetting would drop real distress between samples",
          why_hi="slower forgetting would keep reading pressure long after it ended"),
]

GROUPS: Dict[str, List[Param]] = {"system": _SYSTEM, "disk": _DISK, "network": _NETWORK}


def tuned_paths(group: str) -> List[str]:
    """Config paths the group's buttons control (used to flag them in the UI)."""
    return [p.path for p in GROUPS[group]]


# --------------------------------------------------------------------------------------
# Stepping
# --------------------------------------------------------------------------------------
def _position(p: Param, factory: Dict[str, float], current: Dict[str, float]) -> Optional[float]:
    if p.path not in factory or p.path not in current:
        return None
    return _inverse(p, _tuned(p, factory), _tuned(p, current))


def _step_one(p: Param, factory: Dict[str, float], before: Dict[str, float],
              working: Dict[str, float], direction: str,
              anchor: Optional[float] = None) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(new_value, note)``; ``new_value`` None means "leave as is".

    ``before`` is the untouched input (the position is read from it); ``working`` already
    carries this click's moves of parameters earlier in the table, which is what a
    normalised value must follow.

    ``anchor`` is the group's own position (see ``_group_x``).  A parameter that was held at
    a bound has fallen behind the others, so when it leaves the bound it rejoins the group
    from there instead of from the bound; otherwise one click back from +7 would leave it a
    full step off and the group would read "Custom".
    """
    v0 = _tuned(p, factory)
    t = _tuned(p, before)
    x = _inverse(p, v0, t)
    if x is None:
        return None, f"{p.label}: current value cannot be stepped; edit it by hand"
    d = 1 if direction == RELAX else -1
    moving_up = (d > 0) == p.up_is_relax
    lo, hi = _bounds(p, v0)
    scale = p.norm(working) if p.norm else 1.0
    pinned = (hi is not None and t >= hi - 1e-12) or (lo is not None and t <= lo + 1e-12)

    if moving_up and hi is not None and t >= hi - 1e-12:
        return None, f"{p.label} is at its upper limit ({_fmt(hi * scale)}): {p.why_hi}"
    if not moving_up and lo is not None and t <= lo + 1e-12:
        return None, f"{p.label} is at its lower limit ({_fmt(lo * scale)}): {p.why_lo}"

    if pinned and anchor is not None:
        x = anchor
    new = _forward(p, v0, min(x, _FAR / 2) + d)
    if moving_up and hi is not None:
        new = min(new, hi)
    if not moving_up and lo is not None:
        new = max(new, lo)
    return _round(new * scale), None


def _fmt(v: float) -> str:
    return f"{v:.4g}"


def _group_x(group: str, factory: Dict[str, float], current: Dict[str, float]) -> Optional[float]:
    """The group's step position (the counter shown), or None when nothing can be read.

    Parameters held at a bound say nothing about it and are left out.  If every parameter is
    held, the one furthest from factory speaks for the group, so stepping back unwinds the
    outermost parameter first instead of leaving the group stranded.  When free parameters
    disagree (hand edits) their mean is used: the counter is only a rough position then.
    """
    free: List[float] = []
    held: List[float] = []
    for p in GROUPS[group]:
        x = _position(p, factory, current)
        if x is None:
            continue
        v0, v = _tuned(p, factory), _tuned(p, current)
        lo, hi = _bounds(p, v0)
        if (hi is not None and v >= hi - 1e-12) or (lo is not None and v <= lo + 1e-12):
            if abs(x) < _FAR / 2:
                held.append(x)
        else:
            free.append(x)
    if free:
        return sum(free) / len(free)
    if held:
        return max(held, key=abs)
    return None


def state(group: str, factory: Dict[str, float], current: Dict[str, float]) -> Dict[str, Any]:
    """Where the group sits (``n``, the click counter) and whether each button can act."""
    params = GROUPS[group]
    can = {RELAX: False, TIGHTEN: False}
    for p in params:
        if p.path not in factory or p.path not in current:
            continue
        x = _position(p, factory, current)
        v0, v = _tuned(p, factory), _tuned(p, current)
        lo, hi = _bounds(p, v0)
        at_hi = hi is not None and v >= hi - 1e-12
        at_lo = lo is not None and v <= lo + 1e-12
        if x is not None:
            for direction in (RELAX, TIGHTEN):
                moving_up = (direction == RELAX) == p.up_is_relax
                if not (at_hi if moving_up else at_lo):
                    can[direction] = True
    gx = _group_x(group, factory, current)
    n = int(round(gx)) if gx is not None else None
    return {"n": n if n is not None else 0, "can_relax": can[RELAX],
            "can_tighten": can[TIGHTEN]}


def offsets(group: str, factory: Dict[str, float], current: Dict[str, float]) -> Dict[str, float]:
    """Per-parameter change versus factory, as a fraction (0.12 = 12% above factory)."""
    out = {}
    for p in GROUPS[group]:
        v0, v = factory.get(p.path), current.get(p.path)
        if v0 in (None, 0) or v is None:
            continue
        out[p.path] = round(float(v) / float(v0) - 1.0, 4)
    return out


def describe(group: str, factory: Dict[str, float], current: Dict[str, float]) -> Dict[str, Any]:
    """Where the group sits, without stepping: state, and per-parameter offsets vs factory."""
    if group not in GROUPS:
        raise ValueError(f"unknown pressure group '{group}'")
    offs = offsets(group, factory, current)
    labels = {p.path: p.label for p in GROUPS[group]}
    return {
        "group": group,
        "state": state(group, factory, current),
        "offsets": offs,
        "details": [{"path": path, "label": labels[path], "offset": off}
                    for path, off in offs.items() if abs(off) >= 0.0005],
        "controls": [{"path": p.path, "label": p.label} for p in GROUPS[group]],
    }


def preview(group: str, factory: Dict[str, float], current: Dict[str, float],
            direction: str) -> Dict[str, Any]:
    """Propose the next step for ``group``.  Nothing is written anywhere.

    ``current`` holds the (possibly unsaved) form values; the result's ``values`` carries
    only the paths that change.
    """
    if group not in GROUPS:
        raise ValueError(f"unknown pressure group '{group}'")
    if direction not in (RELAX, TIGHTEN):
        raise ValueError("direction must be 'relax' or 'tighten'")

    params = GROUPS[group]
    # Step in table order so a parameter that others depend on (memory busy before the
    # steepness bound) is already at its new value when the dependents are bounded.
    working = dict(current)
    notes: List[str] = []
    moved: Dict[str, float] = {}
    anchor = _group_x(group, factory, current)
    for p in params:
        if p.path not in factory or p.path not in working:
            continue
        new, note = _step_one(p, factory, current, working, direction, anchor)
        if note:
            notes.append(note)
        if new is not None and new != working[p.path]:
            working[p.path] = new
            moved[p.path] = new

    result = describe(group, factory, working)
    result.update(direction=direction, values=moved, notes=notes)
    return result


# --------------------------------------------------------------------------------------
# Helpers for callers
# --------------------------------------------------------------------------------------
def flatten(config: Any, paths: List[str]) -> Dict[str, float]:
    """Pick ``paths`` out of a config object or dict; missing / non-numeric ones are skipped."""
    out: Dict[str, float] = {}
    for path in paths:
        node: Any = config
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
            if node is None:
                break
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            out[path] = float(node)
    return out


def factory_config_path() -> str:
    """Where the packaged, read-only copy of the shipped config lives."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "config.factory.yaml")
