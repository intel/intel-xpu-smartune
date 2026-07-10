# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""History snapshot building and persistence for dynamic monitor data."""

import json
import re
import time
import threading
from typing import Any, Dict, List, Optional

from db.DatabaseModel import MonitorSnapshot
from monitor.metrics.utils import safe_read, to_float
from utils.logger import logger

_DYNAMIC_SNAPSHOT_LOCK = threading.Lock()
_DYNAMIC_SNAPSHOT_STATE: Dict[str, Any] = {"last_persist_ts": 0.0}
_DYNAMIC_SNAPSHOT_MIN_INTERVAL_SEC = 5.0


def persist_monitor_snapshot(snapshot_type: str, data: Dict[str, Any]) -> None:
    try:
        collected_at = str(data.get("collected_at") or "")
        result = MonitorSnapshot.insert_snapshot(
            snapshot_type=snapshot_type,
            data=data,
            source="monitor.system_info",
            collected_at=collected_at,
        )
        if result.value != "SUCCESS":
            logger.debug("Persist %s snapshot failed: %s", snapshot_type, result.value)
    except Exception as exc:
        logger.debug("Persist %s snapshot exception: %s", snapshot_type, exc)


def _build_gpu_usage_history(gpu_usage: Dict[str, Any]) -> Dict[str, Any]:
    parsed = gpu_usage.get("parsed") or {}
    devices = parsed.get("devices") or []

    summarized_devices: List[Dict[str, Any]] = []
    for dev in devices:
        freqs = dev.get("freqs") or []
        summarized_freqs = []
        for freq in freqs:
            summarized_freqs.append({
                "name": freq.get("name"),
                "cur_mhz": to_float(freq.get("cur_mhz")),
                "act_mhz": to_float(freq.get("act_mhz")),
                "max_mhz": to_float(freq.get("max_mhz")),
                "rc6_pct": to_float(freq.get("rc6_pct")),
                "throttled": bool(freq.get("throttled")),
                "throttle_reasons": freq.get("throttle_reasons") or [],
            })

        engine_util = dev.get("engine_util") or {}
        summarized_engine_util = {
            key: to_float(val)
            for key, val in engine_util.items()
        }

        # GPU utilization = max of all engine utilization values
        engine_values = [v for v in summarized_engine_util.values() if v is not None]
        gpu_utilization = max(engine_values) if engine_values else None

        summarized_devices.append({
            "pci_dev": dev.get("pci_dev"),
            "dev_type": dev.get("dev_type"),
            "drv_name": dev.get("drv_name"),
            "freqs": summarized_freqs,
            "power_w": {
                "gpu": to_float((dev.get("power_w") or {}).get("gpu")),
                "pkg": to_float((dev.get("power_w") or {}).get("pkg")),
                "card": to_float((dev.get("power_w") or {}).get("card")),
            },
            "engine_util": summarized_engine_util,
            "utilization": round(gpu_utilization, 2) if gpu_utilization is not None else None,
        })

    return {
        "available": bool(gpu_usage.get("available")),
        "error": gpu_usage.get("error"),
        "parsed": {
            "devices": summarized_devices,
        },
    }


def _build_npu_history(npu_smi: Dict[str, Any]) -> Dict[str, Any]:
    raw = npu_smi.get("raw")
    parsed = None
    utilization_percent = None

    def _parse_util_from_raw(raw_text: str) -> Optional[float]:
        lines = raw_text.splitlines()
        for idx, line in enumerate(lines):
            if "NPU Utilization" not in line:
                continue
            for probe in lines[idx + 1: idx + 5]:
                match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:\[%\]|%)", probe)
                if match:
                    value = to_float(match.group(1))
                    if value is None:
                        continue
                    return max(0.0, min(value, 100.0))

        fallback = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:\[%\]|%)", raw_text)
        if fallback:
            value = to_float(fallback.group(1))
            if value is not None:
                return max(0.0, min(value, 100.0))
        return None

    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                parsed = loaded
                utilization_percent = to_float(
                    loaded.get("utilization_percent", loaded.get("utilization"))
                )
        except Exception:
            parsed = None

        if utilization_percent is None:
            utilization_percent = _parse_util_from_raw(raw)

    frequency_mhz: Optional[float] = None
    power_w: Optional[float] = None
    noc_bandwidth_mib_per_s: Optional[float] = None
    temperature_c: Optional[float] = None
    tile_config: Optional[int] = None
    memory_mb: Optional[float] = None
    if isinstance(parsed, dict):
        frequency_mhz = to_float(parsed.get("frequency_mhz"))
        power_w = to_float(parsed.get("power_w"))
        noc_bandwidth_mib_per_s = to_float(parsed.get("noc_bandwidth_mib_per_s"))
        temperature_c = to_float(parsed.get("temperature_c"))
        tc = parsed.get("tile_config")
        if isinstance(tc, (int, float)):
            tile_config = int(tc)
        mb = parsed.get("memory_bytes")
        if isinstance(mb, (int, float)) and mb > 0:
            memory_mb = round(float(mb) / (1024 * 1024), 2)

    return {
        "available": bool(npu_smi.get("available")),
        "error": npu_smi.get("error"),
        "raw_present": bool(raw),
        "utilization_percent": round(utilization_percent, 3) if utilization_percent is not None else None,
        "frequency_mhz": round(frequency_mhz, 1) if frequency_mhz is not None else None,
        "power_w": round(power_w, 2) if power_w is not None else None,
        "noc_bandwidth_mib_per_s": round(noc_bandwidth_mib_per_s, 2) if noc_bandwidth_mib_per_s is not None else None,
        "temperature_c": round(temperature_c, 1) if temperature_c is not None else None,
        "tile_config": tile_config,
        "memory_mb": memory_mb,
        "parsed": parsed,
    }


def _build_disk_history(disk: Dict[str, Any]) -> Dict[str, Optional[float]]:
    disk_io = disk.get("disk_io") if isinstance(disk, dict) else None
    if not isinstance(disk_io, dict):
        return {
            "utilization": None,
            "read_kb_per_sec": None,
            "write_kb_per_sec": None,
            "read_iops": None,
            "write_iops": None,
            "total_iops": None,
        }

    max_util: Optional[float] = None
    total_read_kb = 0.0
    total_write_kb = 0.0
    total_read_iops = 0.0
    total_write_iops = 0.0

    for item in disk_io.values():
        if not isinstance(item, dict):
            continue
        util_val = to_float(item.get("utilization"))
        if util_val is not None:
            if util_val <= 1:
                util_val *= 100
            util_val = max(0.0, min(util_val, 100.0))
            max_util = util_val if max_util is None else max(max_util, util_val)

        read_kb = to_float(item.get("read_kb_per_sec"))
        write_kb = to_float(item.get("write_kb_per_sec"))
        read_iops = to_float(item.get("read_iops"))
        write_iops = to_float(item.get("write_iops"))
        total_read_kb += read_kb if read_kb is not None else 0.0
        total_write_kb += write_kb if write_kb is not None else 0.0
        total_read_iops += read_iops if read_iops is not None else 0.0
        total_write_iops += write_iops if write_iops is not None else 0.0

    per_disk: Dict[str, Dict[str, Optional[float]]] = {}
    for disk_name, item in disk_io.items():
        if not isinstance(item, dict):
            continue
        util = to_float(item.get("utilization"))
        if util is not None:
            if util <= 1:
                util *= 100
            util = max(0.0, min(util, 100.0))
        r_kb = to_float(item.get("read_kb_per_sec"))
        w_kb = to_float(item.get("write_kb_per_sec"))
        max_tp: Optional[float] = None
        base_dev = disk_name.rstrip("0123456789") if not disk_name.startswith("nvme") else disk_name.split("p")[0] if "p" in disk_name else disk_name
        rotational_path = f"/sys/block/{base_dev}/queue/rotational"
        try:
            rot = safe_read(rotational_path)
            if rot is not None:
                rot_val = rot.strip()
                if disk_name.startswith("nvme"):
                    max_tp = 3500.0
                elif rot_val == "0":
                    max_tp = 550.0
                else:
                    max_tp = 200.0
        except Exception:
            pass
        per_disk[disk_name] = {
            "util": round(util, 2) if util is not None else None,
            "read_mb": round(r_kb / 1024.0, 3) if r_kb is not None else None,
            "write_mb": round(w_kb / 1024.0, 3) if w_kb is not None else None,
            "max_throughput_mb": max_tp,
        }

    return {
        "utilization": round(max_util, 2) if max_util is not None else None,
        "read_kb_per_sec": round(total_read_kb, 2),
        "write_kb_per_sec": round(total_write_kb, 2),
        "read_iops": round(total_read_iops, 2),
        "write_iops": round(total_write_iops, 2),
        "total_iops": round(total_read_iops + total_write_iops, 2),
        "per_disk": per_disk,
    }


def _build_network_history(network: Dict[str, Any]) -> Dict[str, Any]:
    # Import here to avoid circular dependency (system_info -> _history -> system_info)
    from monitor.system_info import _get_network_static_info

    total = network.get("total") if isinstance(network, dict) else None
    if not isinstance(total, dict):
        total = {}

    rx_bytes = to_float(total.get("rx_bytes_per_sec"))
    tx_bytes = to_float(total.get("tx_bytes_per_sec"))

    total_mbps = None
    if rx_bytes is not None or tx_bytes is not None:
        total_mbps = ((rx_bytes or 0.0) + (tx_bytes or 0.0)) * 8.0 / 1_000_000.0

    static_info = _get_network_static_info() or {}
    per_nic: Dict[str, Dict[str, Optional[float]]] = {}
    all_nic_utils: list[float] = []
    interfaces = network.get("interfaces") if isinstance(network, dict) else None
    if isinstance(interfaces, dict):
        valid_nics = static_info.get("valid_nics") or []
        nic_speeds = {nic["name"]: nic["speed_mbps"] for nic in valid_nics if isinstance(nic, dict)}
        for nic_name, nic_data in interfaces.items():
            if nic_name not in nic_speeds:
                continue
            speed = nic_speeds[nic_name]
            if not speed or speed <= 0:
                continue
            nic_rx = to_float(nic_data.get("rx_bytes_per_sec")) if isinstance(nic_data, dict) else None
            nic_tx = to_float(nic_data.get("tx_bytes_per_sec")) if isinstance(nic_data, dict) else None
            rx_mbps = (nic_rx or 0.0) * 8.0 / 1_000_000.0 if nic_rx is not None else 0.0
            tx_mbps = (nic_tx or 0.0) * 8.0 / 1_000_000.0 if nic_tx is not None else 0.0
            rx_util = max(0.0, min(rx_mbps / speed * 100.0, 100.0))
            tx_util = max(0.0, min(tx_mbps / speed * 100.0, 100.0))
            nic_util = max(rx_util, tx_util)
            per_nic[nic_name] = {
                "util": round(nic_util, 3),
                "rx_mbps": round(rx_mbps, 3),
                "tx_mbps": round(tx_mbps, 3),
                "speed_mbps": speed,
            }
            all_nic_utils.append(nic_util)

    utilization_percent = max(all_nic_utils) if all_nic_utils else None

    return {
        "total": {
            "rx_bytes_per_sec": rx_bytes,
            "tx_bytes_per_sec": tx_bytes,
        },
        "total_mbps": round(total_mbps, 3) if total_mbps is not None else None,
        "utilization_percent": round(utilization_percent, 3) if utilization_percent is not None else None,
        "per_nic": per_nic,
    }


def _build_dynamic_history_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    cpu = data.get("cpu") or {}
    memory = data.get("memory") or {}
    pressure = data.get("pressure") or {}
    network = data.get("network") or {}
    disk = data.get("disk") or {}
    gpu = data.get("gpu") or {}
    npu = data.get("npu") or {}

    return {
        "collected_at": data.get("collected_at"),
        "cpu": {
            "usage_total": to_float(cpu.get("usage_total")),
            "p_core_usage": to_float(cpu.get("p_core_usage")),
            "e_core_usage": to_float(cpu.get("e_core_usage")),
            "lpe_core_usage": to_float(cpu.get("lpe_core_usage")),
            "p_core_freq_mhz": to_float(cpu.get("p_core_freq_mhz")),
            "e_core_freq_mhz": to_float(cpu.get("e_core_freq_mhz")),
            "lpe_core_freq_mhz": to_float(cpu.get("lpe_core_freq_mhz")),
            "temperature_c": to_float(cpu.get("temperature_c")),
            "per_core_usage": cpu.get("per_core_usage") or [],
            "per_core_freq_mhz": cpu.get("per_core_freq_mhz") or [],
            "per_core_temperature_c": cpu.get("per_core_temperature_c") or [],
            "p_core_indices": cpu.get("p_core_indices") or [],
            "e_core_indices": cpu.get("e_core_indices") or [],
            "lpe_core_indices": cpu.get("lpe_core_indices") or [],
        },
        "memory": {
            "usage_percent": to_float(memory.get("usage_percent")),
        },
        "pressure": {
            "score": to_float(pressure.get("score")),
            "level": pressure.get("level"),
            "cpu": to_float(pressure.get("cpu")),
            "memory": to_float(pressure.get("memory")),
            "io": to_float(pressure.get("io")),
            "network_busy_nics": pressure.get("network_busy_nics") or [],
            "network_total_nics": pressure.get("network_total_nics"),
            "network_busy_ratio": to_float(pressure.get("network_busy_ratio")),
            "network_busy_pct": to_float(pressure.get("network_busy_pct")),
            "network_busy_level": pressure.get("network_busy_level"),
        },
        "disk": {
            **_build_disk_history(disk),
            "busy_disks": disk.get("busy_disks") or [],
            "total_disks": disk.get("total_disks"),
            "busy_ratio": to_float(disk.get("busy_ratio")),
            "busy_pct": to_float(disk.get("busy_pct")),
            "busy_level": disk.get("busy_level"),
        },
        "network": _build_network_history(network),
        "gpu": {
            "vram": gpu.get("vram") or {},
            "gpu_usage": _build_gpu_usage_history(gpu.get("gpu_usage") or {}),
        },
        "npu": {
            "npu_smi": _build_npu_history(npu.get("npu_smi") or {}),
        },
    }


def persist_dynamic_snapshot_if_due(data: Dict[str, Any]) -> None:
    now = time.time()
    with _DYNAMIC_SNAPSHOT_LOCK:
        last_ts = float(_DYNAMIC_SNAPSHOT_STATE.get("last_persist_ts") or 0.0)
        if now - last_ts < _DYNAMIC_SNAPSHOT_MIN_INTERVAL_SEC:
            return
        _DYNAMIC_SNAPSHOT_STATE["last_persist_ts"] = now

    payload = _build_dynamic_history_payload(data)
    persist_monitor_snapshot("dynamic", payload)
