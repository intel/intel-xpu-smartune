# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""CPU subsystem: core detection, classification, frequency, and temperature."""

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import psutil

from monitor.metrics.utils import safe_read, run_cmd
from utils.logger import logger

_CORE_CLASS_CACHE: Dict[str, Any] = {"cpu_count": None, "result": None}
_CORE_TOPOLOGY: Optional[List[Optional[int]]] = None

# Prime psutil's internal CPU-time baseline so the first get_cpu_dynamic()
# call returns a meaningful delta-averaged value instead of 0.0.
psutil.cpu_percent(interval=None, percpu=True)


def _expand_cpu_ranges(spec: Optional[str]) -> Set[int]:
    if not spec:
        return set()
    result: Set[int] = set()
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        match = re.match(r"^(\d+)-(\d+)$", token)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if end >= start:
                result.update(range(start, end + 1))
            continue
        if token.isdigit():
            result.add(int(token))
    return result


def _parse_lscpu_cpu_cache_entries() -> List[Dict[str, Any]]:
    output = run_cmd(["lscpu", "--all", "--extended"])
    if not output:
        return []
    entries: List[Dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not re.match(r"^\d+", stripped):
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        entries.append({"cpu": int(parts[0]), "cache": parts[4]})
    return entries


def _is_multi_socket() -> bool:
    cpuinfo = safe_read("/proc/cpuinfo")
    if not cpuinfo:
        return False
    sockets = set()
    for line in cpuinfo.splitlines():
        match = re.match(r"^physical id\s*:\s*(\d+)", line.strip(), re.IGNORECASE)
        if match:
            sockets.add(match.group(1))
    return len(sockets) > 1


def _normalize_unique_core_ids(core_ids: List[int]) -> List[int]:
    return sorted(set(core_ids))


def detect_core_groups() -> Dict[str, Any]:
    entries = _parse_lscpu_cpu_cache_entries()
    if not entries:
        return {"p_cores": [], "e_cores": [], "lpe_cores": [], "source": "unknown"}

    cpu_cache = {item["cpu"]: item["cache"] for item in entries}
    all_core_ids = [item["cpu"] for item in entries]
    remaining_core_ids = list(all_core_ids)
    p_cores: List[int] = []
    e_cores: List[int] = []
    lpe_cores: List[int] = []
    source_chain: List[str] = []

    def remove_remaining(to_remove: List[int]) -> None:
        remove_set = set(to_remove)
        nonlocal remaining_core_ids
        remaining_core_ids = [cid for cid in remaining_core_ids if cid not in remove_set]

    if _is_multi_socket():
        return {
            "p_cores": _normalize_unique_core_ids(all_core_ids),
            "e_cores": [],
            "lpe_cores": [],
            "source": "multi-socket",
        }

    cpuid_bin = shutil.which("cpuid")
    taskset_bin = shutil.which("taskset")
    if cpuid_bin and taskset_bin and remaining_core_ids:
        assigned: List[int] = []
        for core_id in list(remaining_core_ids):
            cache_pattern = cpu_cache.get(core_id, "")
            if cache_pattern.count(":") == 2:
                lpe_cores.append(core_id)
                assigned.append(core_id)
                continue

            output = run_cmd([taskset_bin, "-c", str(core_id), cpuid_bin, "-1", "-l", "0x1a"], timeout=2)
            if not output:
                continue
            match = re.search(r"core type\s*=\s*([^\n]+)", output, re.IGNORECASE)
            if not match:
                continue
            core_type = match.group(1).strip().lower()
            if "intel core" in core_type:
                p_cores.append(core_id)
                assigned.append(core_id)
            elif "intel atom" in core_type:
                e_cores.append(core_id)
                assigned.append(core_id)

        if assigned:
            remove_remaining(assigned)
            source_chain.append("cpuid")

    if remaining_core_ids:
        def _read_cpu_topology(path: str) -> Set[int]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return _expand_cpu_ranges(f.read().strip())
            except FileNotFoundError:
                return set()
            except Exception as exc:
                logger.debug("Read failed for %s: %s", path, exc)
                return set()

        core_set = _read_cpu_topology("/sys/devices/cpu_core/cpus")
        atom_set = _read_cpu_topology("/sys/devices/cpu_atom/cpus")
        lowpower_set = _read_cpu_topology("/sys/devices/cpu_lowpower/cpus")
        assigned: List[int] = []
        atom_candidates: List[int] = []

        if core_set or atom_set or lowpower_set:
            for core_id in list(remaining_core_ids):
                if core_id in core_set:
                    p_cores.append(core_id)
                    assigned.append(core_id)
                elif core_id in lowpower_set:
                    lpe_cores.append(core_id)
                    assigned.append(core_id)
                elif core_id in atom_set:
                    atom_candidates.append(core_id)

            for core_id in atom_candidates:
                cache_pattern = cpu_cache.get(core_id, "")
                if cache_pattern.count(":") == 2:
                    lpe_cores.append(core_id)
                else:
                    e_cores.append(core_id)
                assigned.append(core_id)

        if assigned:
            remove_remaining(assigned)
            source_chain.append("sysfs")

    if remaining_core_ids:
        assigned: List[int] = []
        non_lpe_cores: List[int] = []

        for core_id in list(remaining_core_ids):
            cache_pattern = cpu_cache.get(core_id, "")
            if cache_pattern.count(":") == 2:
                lpe_cores.append(core_id)
                assigned.append(core_id)
            else:
                non_lpe_cores.append(core_id)

        def parse_l1d(cache_pattern: str) -> Optional[int]:
            if not cache_pattern:
                return None
            head = cache_pattern.split(":", 1)[0]
            return int(head) if head.isdigit() else None

        drop_index: Optional[int] = None
        prev_l1d: Optional[int] = None
        for i, core_id in enumerate(non_lpe_cores):
            l1d = parse_l1d(cpu_cache.get(core_id, ""))
            if l1d is None:
                prev_l1d = l1d
                continue
            if prev_l1d is not None and l1d < prev_l1d:
                drop_index = i
                break
            prev_l1d = l1d

        if drop_index is not None:
            for i, core_id in enumerate(non_lpe_cores):
                if i >= drop_index:
                    e_cores.append(core_id)
                else:
                    p_cores.append(core_id)
                assigned.append(core_id)

        if assigned:
            remove_remaining(assigned)
            source_chain.append("lscpu")

    if remaining_core_ids:
        assigned: List[int] = []
        processed: Set[int] = set()
        remaining_set = set(remaining_core_ids)
        has_smt_pairs = False

        for core_id in list(remaining_core_ids):
            if core_id in processed:
                continue
            pair_id = core_id + 1
            cache_pattern = cpu_cache.get(core_id, "")
            if pair_id in remaining_set and pair_id not in processed and cpu_cache.get(pair_id, "") == cache_pattern:
                p_cores.extend([core_id, pair_id])
                assigned.extend([core_id, pair_id])
                processed.update({core_id, pair_id})
                has_smt_pairs = True
            else:
                if has_smt_pairs:
                    e_cores.append(core_id)
                else:
                    p_cores.append(core_id)
                assigned.append(core_id)
                processed.add(core_id)

        if assigned:
            remove_remaining(assigned)
            source_chain.append("smt")

    if remaining_core_ids:
        p_cores.extend(remaining_core_ids)
        source_chain.append("default")

    source = "+".join(source_chain) if source_chain else "unknown"
    return {
        "p_cores": _normalize_unique_core_ids(p_cores),
        "e_cores": _normalize_unique_core_ids(e_cores),
        "lpe_cores": _normalize_unique_core_ids(lpe_cores),
        "source": source,
    }


def classify_cores(freqs: List[Optional[float]]) -> Dict[str, Any]:
    cpu_count = len(freqs)
    cached_count = _CORE_CLASS_CACHE.get("cpu_count")
    cached_result = _CORE_CLASS_CACHE.get("result")
    if cached_result and cached_count == cpu_count:
        return cached_result

    detected = detect_core_groups()
    if detected["p_cores"] or detected["e_cores"] or detected.get("lpe_cores"):
        result = {
            "p_cores": detected["p_cores"],
            "e_cores": detected["e_cores"],
            "lpe_cores": detected.get("lpe_cores", []),
            "source": detected["source"],
        }
        _CORE_CLASS_CACHE["cpu_count"] = cpu_count
        _CORE_CLASS_CACHE["result"] = result
        return result

    valid_values = [f for f in freqs if f is not None]
    if not valid_values:
        result = {"p_cores": [], "e_cores": [], "lpe_cores": [], "source": "unknown"}
        _CORE_CLASS_CACHE["cpu_count"] = cpu_count
        _CORE_CLASS_CACHE["result"] = result
        return result

    freq_span = max(valid_values) - min(valid_values)
    if freq_span < 150:
        result = {"p_cores": [], "e_cores": [], "lpe_cores": [], "source": "single-cluster"}
        _CORE_CLASS_CACHE["cpu_count"] = cpu_count
        _CORE_CLASS_CACHE["result"] = result
        return result

    max_values = [f or 0 for f in freqs]
    sorted_idx = sorted(range(cpu_count), key=lambda i: max_values[i], reverse=True)
    split = max(1, cpu_count // 2)
    p_cores = sorted_idx[:split]
    e_cores = sorted_idx[split:]
    result = {"p_cores": p_cores, "e_cores": e_cores, "lpe_cores": [], "source": "heuristic"}
    _CORE_CLASS_CACHE["cpu_count"] = cpu_count
    _CORE_CLASS_CACHE["result"] = result
    return result


def _avg(values: List[Optional[float]], indices: List[int]) -> Optional[float]:
    picked = [values[i] for i in indices if i < len(values) and values[i] is not None]
    if not picked:
        return None
    return round(sum(picked) / len(picked), 2)


def _get_cpu_base_freq_mhz() -> Optional[float]:
    """Read the base CPU frequency (kHz → MHz) from sysfs, or return None."""
    try:
        base_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency")
        if base_path.exists():
            val = base_path.read_text().strip()
            return round(int(val) / 1000.0, 1)
    except Exception:
        pass
    return None


def get_cpu_freq_summary() -> Dict[str, Any]:
    freqs = psutil.cpu_freq(percpu=True)
    per_core = []
    min_vals = []
    max_vals = []
    per_core_max: List[Optional[float]] = []
    for freq in freqs or []:
        if freq is None:
            per_core.append(None)
            per_core_max.append(None)
            continue
        per_core.append(round(freq.current, 1))
        per_core_max.append(freq.max if freq.max is not None else None)
        if freq.min is not None:
            min_vals.append(freq.min)
        if freq.max is not None:
            max_vals.append(freq.max)
    min_freq = round(min(min_vals), 1) if min_vals else None
    max_freq = round(max(max_vals), 1) if max_vals else None

    core_class = detect_core_groups()
    p_indices = core_class.get("p_cores", [])
    e_indices = core_class.get("e_cores", [])
    lpe_indices = core_class.get("lpe_cores", [])

    def _core_range(indices: List[int]) -> Dict[str, Optional[float]]:
        mins = [min_vals[i] for i in indices if i < len(min_vals)] if min_vals else []
        maxs = [per_core_max[i] for i in indices if i < len(per_core_max) and per_core_max[i] is not None]
        all_mins = [v for v in mins if v is not None]
        all_maxs = [v for v in maxs if v is not None]
        return {
            "min_mhz": round(min(all_mins), 1) if all_mins else min_freq,
            "max_mhz": round(max(all_maxs), 1) if all_maxs else None,
        }

    return {
        "min_mhz": min_freq,
        "max_mhz": max_freq,
        "base_mhz": _get_cpu_base_freq_mhz(),
        "per_core_mhz": per_core,
        "p_core_freq_mhz": _core_range(p_indices) if p_indices else None,
        "e_core_freq_mhz": _core_range(e_indices) if e_indices else None,
        "lpe_core_freq_mhz": _core_range(lpe_indices) if lpe_indices else None,
    }


def get_cpu_dynamic() -> Dict[str, Any]:
    # interval=None: average over the time since the last call in this
    # process, giving true delta semantics (no blocking, no missed peaks).
    # Primed at module import so the first real call is already valid.
    usage_per_core = psutil.cpu_percent(interval=None, percpu=True)
    total_usage = round(sum(usage_per_core) / len(usage_per_core), 2) if usage_per_core else 0.0
    freqs = psutil.cpu_freq(percpu=True)
    per_core_freq = [round(f.current, 1) if f else None for f in freqs or []]

    core_class = classify_cores([f.max if f else None for f in freqs or []])
    p_cores = core_class["p_cores"]
    e_cores = core_class["e_cores"]
    lpe_cores = core_class.get("lpe_cores", [])

    return {
        "usage_total": round(total_usage, 2),
        "per_core_usage": [round(v, 2) for v in usage_per_core],
        "per_core_freq_mhz": per_core_freq,
        "p_core_usage": _avg(usage_per_core, p_cores),
        "e_core_usage": _avg(usage_per_core, e_cores),
        "lpe_core_usage": _avg(usage_per_core, lpe_cores),
        "p_core_freq_mhz": _avg(per_core_freq, p_cores),
        "e_core_freq_mhz": _avg(per_core_freq, e_cores),
        "lpe_core_freq_mhz": _avg(per_core_freq, lpe_cores),
        "p_core_indices": p_cores,
        "e_core_indices": e_cores,
        "lpe_core_indices": lpe_cores,
        "core_type_source": core_class["source"],
    }


def get_memory_dynamic() -> Dict[str, Any]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "usage_percent": round(mem.percent, 2),
        "total_gb": round(mem.total / (1024 ** 3), 2),
        "available_gb": round(mem.available / (1024 ** 3), 2),
        "swap_total_gb": round(swap.total / (1024 ** 3), 2),
        "swap_used_gb": round(swap.used / (1024 ** 3), 2),
        "swap_usage_percent": round(swap.percent, 2),
    }


def get_core_topology(num_logical: int) -> List[Optional[int]]:
    """Return physical core_id for each logical CPU index.  Cached after first call."""
    global _CORE_TOPOLOGY
    if _CORE_TOPOLOGY is not None and len(_CORE_TOPOLOGY) >= num_logical:
        return _CORE_TOPOLOGY[:num_logical]
    mapping: List[Optional[int]] = []
    for i in range(num_logical):
        raw = safe_read(f"/sys/devices/system/cpu/cpu{i}/topology/core_id")
        try:
            mapping.append(int(raw) if raw is not None else None)
        except ValueError:
            mapping.append(None)
    _CORE_TOPOLOGY = mapping
    return mapping


def get_cpu_temperatures(num_logical: int = 0) -> Dict[str, Any]:
    """Return Intel CPU package + per-core temperatures from coretemp."""
    result: Dict[str, Any] = {"package_c": None, "per_core_c": []}
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return result
        entries = temps.get("coretemp", [])
        if not entries:
            return result
        per_physical: Dict[int, float] = {}
        for entry in entries:
            label = (entry.label or "").lower()
            if "package" in label:
                if entry.current is not None:
                    result["package_c"] = round(entry.current, 1)
            elif label.startswith("core "):
                try:
                    idx = int(label.split()[1])
                    if entry.current is not None:
                        per_physical[idx] = round(entry.current, 1)
                except (ValueError, IndexError):
                    pass
        if not per_physical:
            return result
        if num_logical > 0:
            topology = get_core_topology(num_logical)
            per_logical: List[Optional[float]] = []
            for logical_idx in range(num_logical):
                phys_id = topology[logical_idx] if logical_idx < len(topology) else None
                per_logical.append(per_physical.get(phys_id) if phys_id is not None else None)
            result["per_core_c"] = per_logical
        else:
            max_idx = max(per_physical.keys())
            result["per_core_c"] = [per_physical.get(i) for i in range(max_idx + 1)]
    except Exception:
        pass
    return result
