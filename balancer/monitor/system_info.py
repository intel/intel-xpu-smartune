# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Public API for system information collection.

Exposes:
    collect_static_info   -- cached hardware / driver inventory
    collect_dynamic_info  -- real-time metrics snapshot
    preload_static_info   -- convenience wrapper for startup
    shutdown_gpu_usage    -- re-exported for BalanceService
"""

import json
import os
import re
import socket
import subprocess
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import psutil

from config.config import b_config
from monitor.metrics.utils import safe_read, run_cmd
from monitor.metrics.cpu import (
    get_cpu_freq_summary,
    get_cpu_dynamic,
    get_memory_dynamic,
    get_cpu_temperatures,
    detect_core_groups,
)
from monitor.metrics.gpu_info import (
    get_gpu_cards,
    get_gpu_names,
    get_gpu_engines,
    get_gpu_driver_name,
    sort_gpu_engine_instances,
    get_gpu_freq_bounds,
    get_gpu_gt_freq_bounds_sysfs,
    get_gpu_vram,
    get_igpu_eu_count,
    get_gpu_pcie,
    get_gpu_pci_addresses,
    card_to_gpu_label,
)
from monitor.metrics.npu import (
    get_npu_names,
    get_npu_fw_version,
    get_npu_device_info,
    get_npu_freq_bounds,
    get_intel_npu_smi_output,
)
from monitor.metrics.gpu_perf import (
    shutdown_gpu_usage,
    get_gpu_usage_output,
)
from monitor.metrics.history import (
    persist_monitor_snapshot,
    persist_dynamic_snapshot_if_due,
)
from utils.logger import logger

_STATIC_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_STATIC_CACHE_LOCK = threading.Lock()

_NET_RUNTIME_STATE: Dict[str, Any] = {"ts": None, "bytes": {}}


# ---------------------------------------------------------------------------
#  Firmware / version parsing  (small, kept inline)
# ---------------------------------------------------------------------------

def _parse_os_version() -> Optional[str]:
    content = safe_read("/etc/os-release")
    if not content:
        return None
    for line in content.splitlines():
        if line.startswith("VERSION="):
            return line.split("=", 1)[1].strip().strip('"')
    for line in content.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _parse_bios_version() -> Optional[str]:
    output = run_cmd(["dmidecode", "-t", "bios"])
    if not output:
        return None
    for line in output.splitlines():
        if "Version:" in line:
            return line.split("Version:", 1)[1].strip()
    return None


def _get_dmidecode_memory_output() -> Optional[str]:
    """Cache-friendly single call to dmidecode -t memory."""
    return run_cmd(["dmidecode", "-t", "memory"])


def _parse_ddr_speeds(output: Optional[str] = None) -> List[str]:
    if output is None:
        output = _get_dmidecode_memory_output()
    if not output:
        return []
    speeds: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.lower().startswith("speed:"):
            value = line.split(":", 1)[1].strip()
            if value and value not in speeds:
                speeds.append(value)
    return speeds


def _parse_memory_devices(output: Optional[str] = None) -> Dict[str, Any]:
    """Parse per-slot memory device info from dmidecode -t memory.

    Returns dict with:
      total_slots  - total number of memory slots/banks
      populated    - number of slots with a module installed
      devices      - list of populated device dicts
    """
    if output is None:
        output = _get_dmidecode_memory_output()
    if not output:
        return {"total_slots": None, "populated": 0, "devices": []}

    devices: List[Dict[str, Any]] = []
    total_slots = 0

    # Split into sections by "Memory Device" header
    sections = re.split(r"(?=^Memory Device\s*$)", output, flags=re.MULTILINE)

    for section in sections:
        if not section.strip().startswith("Memory Device"):
            continue
        total_slots += 1

        # Parse key-value pairs
        kv: Dict[str, str] = {}
        for line in section.splitlines():
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                kv[key.strip()] = val.strip()

        size_raw = kv.get("Size", "")
        if not size_raw or "No Module Installed" in size_raw:
            continue

        # Parse size to GB
        size_gb: Optional[float] = None
        size_match = re.match(r"(\d+)\s*(GB|MB|TB)", size_raw, re.IGNORECASE)
        if size_match:
            val = int(size_match.group(1))
            unit = size_match.group(2).upper()
            if unit == "GB":
                size_gb = float(val)
            elif unit == "MB":
                size_gb = round(val / 1024, 2)
            elif unit == "TB":
                size_gb = float(val * 1024)

        device = {
            "locator": kv.get("Locator") or None,
            "bank_locator": kv.get("Bank Locator") or None,
            "size_gb": size_gb,
            "type": kv.get("Type") or None,
            "speed": kv.get("Speed") or None,
            "configured_speed": kv.get("Configured Memory Speed") or None,
            "form_factor": kv.get("Form Factor") or None,
            "manufacturer": kv.get("Manufacturer") or None,
            "part_number": (kv.get("Part Number") or "").strip() or None,
        }
        devices.append(device)

    # Extract channel count from Bank Locator values (e.g. "Channel 0 Slot 0")
    channels: set = set()
    for dev in devices:
        bl = dev.get("bank_locator") or ""
        ch_match = re.search(r"Channel\s+(\d+)", bl, re.IGNORECASE)
        if ch_match:
            channels.add(int(ch_match.group(1)))

    return {
        "total_slots": total_slots,
        "populated": len(devices),
        "channels": len(channels) if channels else None,
        "devices": devices,
    }


def _parse_cpu_model() -> Optional[str]:
    content = safe_read("/proc/cpuinfo")
    if not content:
        return None
    for line in content.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_debugfs_uc_info(kind: str) -> Optional[List[Dict[str, Any]]]:
    """Parse GuC/HuC info from debugfs.

    Per driver, only one card is read. For GT selection:
    - single GT → read from it
    - multiple GTs → guc from gt0, huc from gt1
    - legacy i915 (no numbered gt) → fallback to gt/uc/
    """
    key = kind.strip().lower()
    if key not in {"guc", "huc"}:
        return None

    debugfs_base = "/sys/kernel/debug/dri"
    info_file = f"{key}_info"

    # One card per driver is enough
    driver_to_dri: Dict[str, str] = {}
    for card in get_gpu_cards():
        driver = get_gpu_driver_name(card)
        if driver and driver not in driver_to_dri:
            driver_to_dri[driver] = os.path.basename(card).replace("card", "")

    results: List[Dict[str, Any]] = []
    for driver, dri_num in driver_to_dri.items():
        dri_path = os.path.join(debugfs_base, dri_num)

        # Pick GT directory
        try:
            gt_dirs = sorted(e for e in os.listdir(dri_path) if re.match(r"gt\d+$", e))
        except OSError:
            gt_dirs = []

        if len(gt_dirs) > 1 and key == "huc":
            gt_sub = gt_dirs[1]
        elif gt_dirs:
            gt_sub = gt_dirs[0]
        else:
            gt_sub = "gt"  # legacy i915

        content = safe_read(os.path.join(dri_path, gt_sub, "uc", info_file))
        if not content:
            continue

        fw_path = version = status = None
        for line in content.splitlines():
            s = line.strip()
            m = re.match(rf"{key}\s+firmware:\s*(\S+)", s, re.IGNORECASE)
            if m and m.group(1) != "(null)":
                fw_path = m.group(1)
            elif s.startswith("status:"):
                status = s.split(":", 1)[1].strip()
            elif "found" in s and "version" in s:
                m = re.search(r"(\d+(?:\.\d+)+)", s)
                if m:
                    version = m.group(1)

        if fw_path and version:
            results.append({
                "driver": driver,
                "firmware": fw_path,
                "version": version,
                "status": status,
            })

    return results or None


def _get_uc_fw_info(kind: str) -> List[Dict[str, Any]]:
    """Get GuC/HuC firmware info from debugfs."""
    return _parse_debugfs_uc_info(kind) or []


def _get_dpkg_version(pkg_names) -> Dict[str, Any]:
    if isinstance(pkg_names, str):
        pkg_names = [pkg_names]
    for pkg_name in pkg_names:
        cmd = ["dpkg", "-l", pkg_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception:
            continue
        if res.returncode != 0 or not res.stdout.strip():
            continue
        for line in res.stdout.strip().splitlines():
            if line.startswith("ii") and pkg_name in line:
                parts = line.split()
                version = parts[2] if len(parts) > 2 else "NA"
                return {"installed": True, "version": version, "raw": line.strip()}
    return {"installed": False, "version": "NA", "raw": "NA"}


# ---------------------------------------------------------------------------
#  Network
# ---------------------------------------------------------------------------

def _get_network_runtime_bw() -> Dict[str, Any]:
    stats = psutil.net_io_counters(pernic=True)
    now = time.time()
    prev_ts = _NET_RUNTIME_STATE.get("ts")
    prev_bytes = _NET_RUNTIME_STATE.get("bytes", {})

    interfaces: Dict[str, Dict[str, float]] = {}
    total_rx = 0.0
    total_tx = 0.0

    for name, counters in stats.items():
        last = prev_bytes.get(name)
        if prev_ts and last:
            delta_time = max(now - prev_ts, 0.0001)
            rx_rate = (counters.bytes_recv - last[0]) / delta_time
            tx_rate = (counters.bytes_sent - last[1]) / delta_time
        else:
            rx_rate = 0.0
            tx_rate = 0.0
        interfaces[name] = {
            "rx_bytes_per_sec": round(rx_rate, 2),
            "tx_bytes_per_sec": round(tx_rate, 2),
        }
        total_rx += rx_rate
        total_tx += tx_rate

    _NET_RUNTIME_STATE["ts"] = now
    _NET_RUNTIME_STATE["bytes"] = {k: (v.bytes_recv, v.bytes_sent) for k, v in stats.items()}

    return {
        "interfaces": interfaces,
        "total": {
            "rx_bytes_per_sec": round(total_rx, 2),
            "tx_bytes_per_sec": round(total_tx, 2),
        },
    }


def _compute_disk_pressure(disk_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregate busy ratio from per-disk is_busy flags.

    Returns busy_disks, total_disks, busy_ratio, busy_pct, busy_level
    matching the network pressure pattern.
    """
    disk_io = disk_stats.get('disk_io')
    if not isinstance(disk_io, dict) or not disk_io:
        return {
            "busy_disks": [],
            "total_disks": 0,
            "busy_ratio": None,
            "busy_pct": None,
            "busy_level": "NO DATA",
        }

    busy_disks: List[str] = []
    total_disks = 0
    for disk_name, detail in disk_io.items():
        if not isinstance(detail, dict):
            continue
        total_disks += 1
        if detail.get("is_busy"):
            busy_disks.append(disk_name)

    busy_count = len(busy_disks)
    busy_ratio = busy_count / total_disks if total_disks > 0 else None
    busy_pct = busy_ratio * 100.0 if busy_ratio is not None else None

    _th = b_config.thresholds or {}
    _th_low = _th.get("low", 0.4)
    _th_medium = _th.get("medium", 0.6)
    _th_high = _th.get("high", 0.8)
    if total_disks == 0 or busy_ratio is None:
        busy_level = "NO DATA"
    elif busy_ratio < _th_low:
        busy_level = "LOW"
    elif busy_ratio < _th_medium:
        busy_level = "MEDIUM"
    elif busy_ratio < _th_high:
        busy_level = "HIGH"
    else:
        busy_level = "CRITICAL"

    return {
        "busy_disks": busy_disks,
        "total_disks": total_disks,
        "busy_ratio": round(busy_ratio, 4) if busy_ratio is not None else None,
        "busy_pct": round(busy_pct, 2) if busy_pct is not None else None,
        "busy_level": busy_level,
    }


_NETWORK_BUSY_THRESHOLD_PCT = 80  # NIC is "busy" when max(rxUtil, txUtil) >= 80%


def _compute_network_pressure(network_bw: Dict[str, Any], net_static: Dict[str, Any]) -> Dict[str, Any]:
    """Compute per-NIC busy ratio based on actual link speed.

    Returns a dict with busy_nics, total_nics, busy_ratio, busy_pct, busy_level
    matching the disk IO pressure pattern.
    """
    interfaces = network_bw.get("interfaces") or {}
    valid_nics = net_static.get("valid_nics") or []
    nic_speeds = {nic["name"]: nic["speed_mbps"] for nic in valid_nics if isinstance(nic, dict) and nic.get("speed_mbps", 0) > 0}

    busy_nics: List[str] = []
    total_nics = 0

    for nic_name, speed_mbps in nic_speeds.items():
        nic_data = interfaces.get(nic_name)
        if not isinstance(nic_data, dict):
            continue
        total_nics += 1
        rx_bytes = nic_data.get("rx_bytes_per_sec", 0.0)
        tx_bytes = nic_data.get("tx_bytes_per_sec", 0.0)
        rx_mbps = rx_bytes * 8.0 / 1_000_000.0
        tx_mbps = tx_bytes * 8.0 / 1_000_000.0
        rx_util = min(rx_mbps / speed_mbps * 100.0, 100.0) if speed_mbps > 0 else 0.0
        tx_util = min(tx_mbps / speed_mbps * 100.0, 100.0) if speed_mbps > 0 else 0.0
        if max(rx_util, tx_util) >= _NETWORK_BUSY_THRESHOLD_PCT:
            busy_nics.append(nic_name)

    busy_count = len(busy_nics)
    busy_ratio = busy_count / total_nics if total_nics > 0 else None
    busy_pct = busy_ratio * 100.0 if busy_ratio is not None else None

    _nth = b_config.network_thresholds or {}
    _nth_low = _nth.get("low", 0.4)
    _nth_medium = _nth.get("medium", 0.6)
    _nth_high = _nth.get("high", 0.8)
    if total_nics == 0 or busy_ratio is None:
        busy_level = "NO DATA"
    elif busy_ratio < _nth_low:
        busy_level = "LOW"
    elif busy_ratio < _nth_medium:
        busy_level = "MEDIUM"
    elif busy_ratio < _nth_high:
        busy_level = "HIGH"
    else:
        busy_level = "CRITICAL"

    return {
        "busy_nics": busy_nics,
        "total_nics": total_nics,
        "busy_ratio": round(busy_ratio, 4) if busy_ratio is not None else None,
        "busy_pct": round(busy_pct, 2) if busy_pct is not None else None,
        "busy_level": busy_level,
    }


def _get_network_static_info() -> Dict[str, Any]:
    stats = psutil.net_if_stats()
    io_stats = psutil.net_io_counters(pernic=True)
    network_speeds_mbps: Dict[str, int] = {}
    peak_speed: Optional[int] = None

    def _is_candidate_interface(name: str) -> bool:
        lower = name.lower()
        if lower == "lo" or lower.startswith("docker") or lower.startswith("veth"):
            return False
        if lower.startswith("br-") or lower.startswith("virbr"):
            return False
        return True

    primary_interface: Optional[str] = None
    primary_score: Optional[Tuple[int, int, int]] = None

    for name, nic in stats.items():
        raw_speed = getattr(nic, "speed", 0)
        try:
            speed = int(raw_speed)
        except (TypeError, ValueError):
            speed = 0
        network_speeds_mbps[name] = speed
        if speed > 0:
            peak_speed = speed if peak_speed is None else max(peak_speed, speed)

        if not _is_candidate_interface(name):
            continue

        is_up = bool(getattr(nic, "isup", False))
        counters = io_stats.get(name)
        total_bytes = int((getattr(counters, "bytes_recv", 0) or 0) + (getattr(counters, "bytes_sent", 0) or 0))
        score = (
            1 if is_up else 0,
            speed if speed > 0 else 0,
            total_bytes,
        )
        if primary_score is None or score > primary_score:
            primary_score = score
            primary_interface = name

    # Collect IP addresses per interface
    if_addrs = psutil.net_if_addrs()

    valid_nics: List[Dict[str, Any]] = []
    for name, nic in stats.items():
        if not _is_candidate_interface(name):
            continue
        is_up = bool(getattr(nic, "isup", False))
        speed = network_speeds_mbps.get(name, 0)
        if is_up and speed > 0:
            ipv4_addrs: List[str] = []
            ipv6_addrs: List[str] = []
            for addr in if_addrs.get(name, []):
                if addr.family == socket.AF_INET:
                    ipv4_addrs.append(addr.address)
                elif addr.family == socket.AF_INET6:
                    # Strip zone-id suffix (e.g. %eth0)
                    ipv6_addrs.append(addr.address.split("%")[0])
            valid_nics.append({
                "name": name,
                "speed_mbps": speed,
                "ipv4": ipv4_addrs,
                "ipv6": ipv6_addrs,
            })

    return {
        "nic_count": len(stats),
        "network_speeds_mbps": network_speeds_mbps,
        "network_peak_mbps": peak_speed,
        "primary_interface": primary_interface,
        "valid_nics": valid_nics,
    }


# ---------------------------------------------------------------------------
#  Disk
# ---------------------------------------------------------------------------

def _get_disk_static_info() -> Dict[str, Any]:
    devices: List[Dict[str, Any]] = []

    output = run_cmd(["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE"])
    if output:
        try:
            payload = json.loads(output)
            for dev in payload.get("blockdevices", []):
                if dev.get("type") != "disk":
                    continue
                size_raw = dev.get("size")
                try:
                    size_bytes = int(size_raw)
                except (TypeError, ValueError):
                    size_bytes = None
                devices.append({
                    "name": dev.get("name") or "unknown",
                    "size_bytes": size_bytes,
                    "size_gb": round(size_bytes / (1024 ** 3), 2) if size_bytes else None,
                })
        except Exception as exc:
            logger.debug("Failed to parse lsblk output: %s", exc)

    if not devices:
        fallback: Dict[str, int] = {}
        for partition in psutil.disk_partitions(all=False):
            if partition.device in fallback:
                continue
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                fallback[partition.device] = usage.total
            except Exception:
                continue

        for device, size_bytes in fallback.items():
            devices.append({
                "name": os.path.basename(device) or device,
                "size_bytes": size_bytes,
                "size_gb": round(size_bytes / (1024 ** 3), 2) if size_bytes else None,
            })

    total_size_bytes = sum(d["size_bytes"] for d in devices if isinstance(d.get("size_bytes"), int))

    return {
        "device_count": len(devices),
        "total_size_bytes": total_size_bytes if total_size_bytes > 0 else None,
        "total_size_gb": round(total_size_bytes / (1024 ** 3), 2) if total_size_bytes > 0 else None,
        "devices": devices,
    }


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def collect_static_info(force_refresh: bool = False) -> Dict[str, Any]:
    cached = _STATIC_CACHE.get("data")
    if cached is not None and not force_refresh:
        return cached

    with _STATIC_CACHE_LOCK:
        cached = _STATIC_CACHE.get("data")
        if cached is not None and not force_refresh:
            return cached

        now = time.time()

        cpu_freqs = get_cpu_freq_summary()
        mem = psutil.virtual_memory()
        dmi_mem_output = _get_dmidecode_memory_output()
        cards = get_gpu_cards()
        network_static = _get_network_static_info()
        disk_static = _get_disk_static_info()
        gpu_pci_addresses = get_gpu_pci_addresses(cards)
        gpu_gt_freq_bounds = get_gpu_gt_freq_bounds_sysfs(cards)
        raw_gpu_engines = get_gpu_engines(cards)
        gpu_engine_instances: Dict[str, List[str]] = {
            card_key: sorted_instances
            for card_key, engines in raw_gpu_engines.items()
            if (sorted_instances := sort_gpu_engine_instances(engines))
        }

        data = {
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bios": {"version": _parse_bios_version()},
            "os": {"version": _parse_os_version()},
            "driver": {
                "kernel_version": safe_read("/proc/sys/kernel/osrelease"),
                "kernel_cmdline": safe_read("/proc/cmdline"),
                "guc_fw": _get_uc_fw_info("guc"),
                "huc_fw": _get_uc_fw_info("huc"),
                "mesa": _get_dpkg_version("mesa-common-dev"),
                "opencl": _get_dpkg_version("intel-opencl-icd"),
                "level_zero": _get_dpkg_version(["libze-intel-gpu1", "intel-level-zero-gpu"]),
                "media": _get_dpkg_version("intel-media-va-driver-non-free"),
                "npu_fw": get_npu_fw_version(),
            },
            "cpu": {
                "model_name": _parse_cpu_model(),
                "core_count": {
                    "logical": psutil.cpu_count(logical=True),
                    "physical": psutil.cpu_count(logical=False),
                },
                "freq_mhz": cpu_freqs,
            },
            "memory": {
                "ddr_speeds": _parse_ddr_speeds(dmi_mem_output),
                "total_gb": round(mem.total / (1024 ** 3), 2),
                "swap_total_gb": round(psutil.swap_memory().total / (1024 ** 3), 2),
                "devices": _parse_memory_devices(dmi_mem_output),
            },
            "io": network_static,
            "disk": disk_static,
            "gpu": {
                "names": get_gpu_names(),
                "count": len(cards),
                "engines": gpu_engine_instances,
                "freq_bounds_mhz": get_gpu_freq_bounds(cards),
                "gt_freq_bounds_mhz": gpu_gt_freq_bounds,
                "vram": get_gpu_vram(cards),
                "pcie": get_gpu_pcie(cards),
                "eu_count": get_igpu_eu_count(cards),
                "pci_addresses": gpu_pci_addresses,
                "driver_names": {card_to_gpu_label(c): get_gpu_driver_name(c) for c in cards},
            },
            "npu": {
                "names": get_npu_names(),
                "freq_bounds_mhz": get_npu_freq_bounds(),
                **get_npu_device_info(),
            },
        }

        persist_monitor_snapshot("static", data)

        _STATIC_CACHE["data"] = data
        _STATIC_CACHE["ts"] = now
        return data

def preload_static_info() -> Dict[str, Any]:
    data = collect_static_info()
    logger.info("Static system info preloaded at startup: %s", data.get("collected_at"))
    return data


def collect_dynamic_info(resource_monitor=None, system_pressure_monitor=None) -> Dict[str, Any]:
    pressure_extra: Dict[str, Any] = {}
    disk_stats: Dict[str, Any] = {}

    if system_pressure_monitor is not None:
        try:
            level, score, is_disk_io_stressed = system_pressure_monitor.get_current_pressure_level()
            pressure_extra['score'] = score
            pressure_extra['level'] = level
            pressure_extra['is_disk_io_stressed'] = is_disk_io_stressed
        except Exception as e:
            logger.warning(f"SystemPressureMonitor unavailable: {e}")
        try:
            disk_stress = system_pressure_monitor.get_disk_io_stress()
            if disk_stress:
                disk_stats = {
                    'disk_io': disk_stress.get('details', {}),
                    'is_stressed': disk_stress.get('is_stressed', False),
                    'stressed_disks': disk_stress.get('stressed_disks', []),
                    'iowait': disk_stress.get('iowait', 0.0),
                }
        except Exception as e:
            logger.warning(f"Disk stress check unavailable via SPM: {e}")

    try:
        from monitor.psi import PSIMonitor
        psi = PSIMonitor().get_current_pressure()
        pressure_extra['cpu'] = psi.get('cpu', 0.0)
        pressure_extra['memory'] = psi.get('memory', 0.0)
        pressure_extra['io'] = psi.get('io', 0.0)
    except Exception as e:
        logger.debug(f"PSIMonitor unavailable for raw pressure: {e}")

    if not disk_stats and resource_monitor is not None:
        try:
            disk_stress = resource_monitor.is_disk_io_stressed()
            disk_stats = {
                'disk_io': disk_stress.get('details', {}),
                'is_stressed': disk_stress.get('is_stressed', False),
                'stressed_disks': disk_stress.get('stressed_disks', []),
                'iowait': disk_stress.get('iowait', 0.0),
            }
        except Exception as e:
            logger.warning(f"Disk stress check unavailable: {e}")

    # Disk IO pressure: aggregate busy ratio from per-disk is_busy flags
    disk_pressure = _compute_disk_pressure(disk_stats)
    disk_stats['busy_disks'] = disk_pressure['busy_disks']
    disk_stats['total_disks'] = disk_pressure['total_disks']
    disk_stats['busy_ratio'] = disk_pressure['busy_ratio']
    disk_stats['busy_pct'] = disk_pressure['busy_pct']
    disk_stats['busy_level'] = disk_pressure['busy_level']

    gpu_cards = get_gpu_cards()

    network_bw = _get_network_runtime_bw()

    # Network pressure: per-NIC busy ratio based on actual link speed
    try:
        net_static = _get_network_static_info()
        net_pressure_result = _compute_network_pressure(network_bw, net_static)
        pressure_extra['network_busy_nics'] = net_pressure_result['busy_nics']
        pressure_extra['network_total_nics'] = net_pressure_result['total_nics']
        pressure_extra['network_busy_ratio'] = net_pressure_result['busy_ratio']
        pressure_extra['network_busy_pct'] = net_pressure_result['busy_pct']
        pressure_extra['network_busy_level'] = net_pressure_result['busy_level']
    except Exception as e:
        logger.debug(f"Network pressure calculation unavailable: {e}")

    data = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": get_cpu_dynamic(),
        "memory": get_memory_dynamic(),
        "pressure": pressure_extra,
        "network": network_bw,
        "disk": disk_stats,
        "gpu": {
            "vram": get_gpu_vram(gpu_cards),
            "gpu_usage": get_gpu_usage_output(),
        },
        "npu": {
            "npu_smi": get_intel_npu_smi_output(),
        },
    }

    num_logical = len(data["cpu"].get("per_core_usage") or [])
    cpu_temps = get_cpu_temperatures(num_logical=num_logical)
    data["cpu"]["temperature_c"] = cpu_temps["package_c"]
    data["cpu"]["per_core_temperature_c"] = cpu_temps["per_core_c"]

    persist_dynamic_snapshot_if_due(data)

    return data
