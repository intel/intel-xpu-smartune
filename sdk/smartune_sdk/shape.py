# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Output shaping for model consumption. API payloads are built for the
# dashboard: per-core arrays, full command lines, thousands of history rows.
# Handed to an LLM verbatim they burn its context on noise, so every CLI
# command passes its result through compact() and fit() before printing.

import json
import re
import time
from datetime import datetime

# Per-element arrays the dashboard plots but an agent never reasons over.
DROP_KEYS = frozenset({
    "per_core_usage", "per_core_freq_mhz", "per_core_temperature_c",
    "p_core_indices", "e_core_indices", "lpe_core_indices",
    "core_type_source", "raw",
})

MAX_STRING = 300
DEFAULT_MAX_BYTES = 16000

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'90s' / '30m' / '1h' / '2d' / '1w' / bare seconds -> int seconds."""
    match = _DURATION_RE.match(str(text))
    if not match:
        raise ValueError(f"invalid duration '{text}' (use e.g. 30m, 1h, 2d)")
    seconds = int(float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()])
    if seconds <= 0:
        raise ValueError(f"duration must be positive, got '{text}'")
    return seconds


def window(since, now=None):
    """(from, to) epoch seconds for 'the last <since>'."""
    to = int(now if now is not None else time.time())
    return to - parse_duration(since), to


def iso(ts):
    """Epoch seconds -> 'YYYY-MM-DD HH:MM:SS +HHMM' in the server's local time.

    The UTC offset is kept so an agent on another machine (or in another time
    zone) does not misread when an event happened. Anything else passes through.
    """
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(ts))
    return ts


# Time fields as the API names them. The server mixes epoch seconds, UTC ISO
# strings (events, alerts, journal lines) and naive local strings (collected_at),
# so the CLI rewrites them all into iso()'s single format before printing.
TIME_KEYS = frozenset({"ts", "ts_utc", "ts_iso", "ts_epoch", "first_ts", "last_ts",
                       "start_time", "end_time", "create_time"})
# "from" / "to" are times only inside these objects; elsewhere (config_changes,
# for instance) they carry old and new values.
WINDOW_KEYS = frozenset({"window", "period"})
_EPOCH_RANGE = (1e9, 1e11)  # 2001 .. 5138: excludes durations, counts and the 0 placeholder
_LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def _is_time_key(key, parent_key):
    if key in TIME_KEYS or key.endswith("_at"):
        return True
    return key in ("from", "to") and parent_key in WINDOW_KEYS


def to_local_time(value):
    """Any server time value -> iso() format; anything unrecognised passes through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return iso(value) if _EPOCH_RANGE[0] <= value < _EPOCH_RANGE[1] else value
    if not isinstance(value, str) or not value:
        return value
    try:
        datetime.strptime(value, _LOCAL_FORMAT)
        return value  # already normalised
    except ValueError:
        pass
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        # Naive strings such as collected_at are the server's local time.
        return iso(time.mktime(parsed.timetuple()))
    return iso(parsed.timestamp())


def localize_times(obj, parent_key=None):
    """Rewrite every time field in obj (recursively) with to_local_time()."""
    if isinstance(obj, dict):
        return {
            k: to_local_time(v) if isinstance(k, str) and _is_time_key(k, parent_key)
            and not isinstance(v, (dict, list)) else localize_times(v, k)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [localize_times(v, parent_key) for v in obj]
    return obj


def compact(obj):
    """Drop plotting-only keys, cap string length, round floats."""
    if isinstance(obj, dict):
        return {k: compact(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 2)
    if isinstance(obj, str) and len(obj) > MAX_STRING:
        return obj[:MAX_STRING] + f"...(+{len(obj) - MAX_STRING} chars)"
    return obj


def pick(row, *keys):
    """Subset of a dict, skipping keys that are absent."""
    return {k: row[k] for k in keys if k in row}


def dumps(obj, pretty=False):
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _size(obj):
    return len(dumps(obj).encode("utf-8"))


def _longest_list(obj, path="$"):
    """(path, parent, key, list) of the longest list with more than one item."""
    best = None
    if isinstance(obj, dict):
        children = obj.items()
    elif isinstance(obj, list):
        children = enumerate(obj)
    else:
        return None
    for key, value in children:
        child_path = f"{path}.{key}" if isinstance(obj, dict) else f"{path}[{key}]"
        if isinstance(value, list) and len(value) > 1:
            if best is None or len(value) > len(best[3]):
                best = (child_path, obj, key, value)
        found = _longest_list(value, child_path)
        if found and (best is None or len(found[3]) > len(best[3])):
            best = found
    return best


def fit(obj, max_bytes=DEFAULT_MAX_BYTES):
    """Shrink obj until it serialises under max_bytes by halving its longest lists.

    Truncation is never silent: a top-level `_truncated` entry records which
    lists were cut and their original length, so the reader knows to narrow the
    query instead of treating a partial answer as complete.
    """
    if _size(obj) <= max_bytes:
        return obj
    if not isinstance(obj, dict):
        obj = {"result": obj}
    budget = max_bytes - 512  # room for the _truncated note itself
    totals = {}
    while _size(obj) > budget:
        found = _longest_list(obj)
        if found is None:
            break
        path, parent, key, items = found
        totals.setdefault(path, len(items))
        parent[key] = items[: len(items) // 2]
    obj["_truncated"] = {
        "lists": {p: {"kept": _len_at(obj, p), "total": n} for p, n in totals.items()},
        "hint": "Output was cut to fit the size limit; narrow the query (time range, -n, filters).",
    }
    return obj


def _len_at(obj, path):
    node = obj
    for part in re.findall(r"\.([^.\[]+)|\[(\d+)\]", path):
        name, index = part
        try:
            node = node[name] if name else node[int(index)]
        except (KeyError, IndexError, TypeError):
            return None
    return len(node) if isinstance(node, list) else None


def numeric_stats(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return {
        "min": round(min(nums), 2),
        "avg": round(sum(nums) / len(nums), 2),
        "max": round(max(nums), 2),
        "last": round(nums[-1], 2),
    }


def downsample(rows, points):
    """Evenly spaced subset of rows, always keeping the last one."""
    if points <= 0 or len(rows) <= points:
        return rows
    step = len(rows) / points
    picked = [rows[int(i * step)] for i in range(points - 1)]
    picked.append(rows[-1])
    return picked
