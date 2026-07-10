# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""NPU subsystem: discovery, device info, frequency bounds, and SMI telemetry."""

import glob
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from monitor.metrics.utils import safe_read, run_cmd
from monitor.npu_monitor import PmtTelemetry, get_npu_processes
from utils.logger import logger


_pmt_init_warned = False


def get_npu_names() -> List[str]:
    output = run_cmd(["lspci", "-nn"])
    if not output:
        return []
    matches = []
    for line in output.splitlines():
        if re.search(r"npu|vpu|accelerator", line, re.IGNORECASE):
            matches.append(line.strip())
    return matches


def _get_intel_vpu_pci_devices() -> List[str]:
    devices: List[str] = []
    for entry in glob.glob("/sys/bus/pci/drivers/intel_vpu/*"):
        name = os.path.basename(entry)
        if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", name):
            devices.append(name)
    return sorted(set(devices))


def get_npu_fw_version() -> str:
    for pci_dev in _get_intel_vpu_pci_devices():
        fw_path = f"/sys/kernel/debug/accel/{pci_dev}/fw_version"
        fw = safe_read(fw_path)
        if fw:
            return fw
    return "NA"


def get_npu_device_info() -> Dict[str, Optional[str]]:
    driver_path = "/sys/bus/pci/drivers/intel_vpu/"
    pciid: Optional[str] = None
    driver_version: Optional[str] = None
    for pci_dev in _get_intel_vpu_pci_devices():
        dev_path = os.path.join("/sys/bus/pci/devices", pci_dev)
        raw_id = safe_read(os.path.join(dev_path, "device"))
        if raw_id:
            pciid = raw_id.strip()
        break
    module_version = safe_read(os.path.join(driver_path, "module", "version"))
    if module_version:
        driver_version = module_version.split(" ")[0]
    return {
        "pciid": pciid,
        "driver_version": driver_version,
    }


def get_npu_freq_bounds() -> Dict[str, Dict[str, Optional[float]]]:
    result: Dict[str, Dict[str, Optional[float]]] = {}
    for pci_dev in _get_intel_vpu_pci_devices():
        max_path = os.path.join("/sys/bus/pci/devices", pci_dev, "npu_max_frequency_mhz")
        try:
            with open(max_path, "r", encoding="utf-8") as f:
                max_val = f.read().strip()
            result[pci_dev] = {
                "max_mhz": float(max_val) if max_val.isdigit() else None,
            }
        except FileNotFoundError:
            result[pci_dev] = {"max_mhz": None}
        except Exception as exc:
            logger.debug("Read failed for %s: %s", max_path, exc)
            result[pci_dev] = {"max_mhz": None}
    return result


def _read_int_file(path: str) -> Optional[int]:
    raw_text = safe_read(path)
    if not raw_text:
        return None
    try:
        return int(raw_text.strip())
    except ValueError:
        return None


def _collect_npu_smi_once(include_processes: bool = False) -> Dict[str, Any]:
    driver_path = "/sys/bus/pci/drivers/intel_vpu/"
    debugfs_root = "/sys/kernel/debug/accel/"
    if not os.path.exists(driver_path):
        return {"available": False, "raw": None, "error": "Intel NPU driver 'intel_vpu' is not loaded"}

    dev_path: Optional[str] = None
    debugfs_path: Optional[str] = None
    dev_file: Optional[str] = None

    for entry in os.listdir(driver_path):
        if not entry.startswith("0000:"):
            continue
        dev_path = os.path.join(driver_path, entry)
        debugfs_path = os.path.join(debugfs_root, entry)
        accel_path = os.path.join(dev_path, "accel")
        if os.path.exists(accel_path):
            accel_entries = os.listdir(accel_path)
            if accel_entries:
                dev_file = os.path.join("/dev/accel", accel_entries[0])
        break

    if dev_path is None:
        return {"available": False, "raw": None, "error": "No Intel NPU PCI device found"}

    global _pmt_init_warned
    telemetry: Optional[PmtTelemetry] = None
    pmt_error: Optional[str] = None
    try:
        telemetry = PmtTelemetry()
    except (SystemExit, RuntimeError) as exc:
        pmt_error = f"PmtTelemetry init failed: {exc}"
    except Exception as exc:
        pmt_error = f"PmtTelemetry init failed: {exc}"
    if pmt_error and not _pmt_init_warned:
        logger.debug(pmt_error + " (this message will be suppressed on subsequent polls)")
        _pmt_init_warned = True

    npu_busy_path = os.path.join(dev_path, "npu_busy_time_us")

    def read_busy_time() -> Optional[int]:
        if not os.path.exists(npu_busy_path):
            return None
        return _read_int_file(npu_busy_path)

    interval_ms = 200.0
    busy_start = read_busy_time()
    t_start = time.monotonic()

    energy_start: Optional[float] = None
    bandwidth_start: Optional[float] = None
    if telemetry is not None:
        telemetry.update_buffer()
        energy_start = telemetry.get_npu_energy()
        bandwidth_start = telemetry.get_noc_bandwidth()

    time.sleep(interval_ms * 1e-3)

    busy_end = read_busy_time()
    t_end = time.monotonic()

    energy_end: Optional[float] = None
    bandwidth_end: Optional[float] = None
    freq_mhz: Optional[float] = None
    tile_config: Optional[int] = None
    temperature_c: Optional[float] = None
    if telemetry is not None:
        telemetry.update_buffer()
        energy_end = telemetry.get_npu_energy()
        bandwidth_end = telemetry.get_noc_bandwidth()
        raw_freq = telemetry.get_freq()
        freq_mhz = raw_freq * 1000 / 2 if raw_freq is not None else None
        tile_config = telemetry.get_tile_config()
        temperature_c = telemetry.get_npu_temperature()

    utilization_percent: Optional[float] = None
    if busy_start is not None and busy_end is not None:
        busy_delta = busy_end - busy_start
        elapsed_us = (t_end - t_start) * 1e6
        if elapsed_us > 0:
            utilization_percent = max(0.0, min(100.0, 100.0 * busy_delta / elapsed_us))

    power_w: Optional[float] = None
    if energy_start is not None and energy_end is not None:
        power_w = (energy_end - energy_start) / (interval_ms * 1e-3)

    noc_bandwidth: Optional[float] = None
    if bandwidth_start is not None and bandwidth_end is not None:
        noc_bandwidth = round(bandwidth_end - bandwidth_start, 6)

    memory_bytes = _read_int_file(os.path.join(dev_path, "npu_memory_utilization"))
    current_freq_mhz = _read_int_file(os.path.join(dev_path, "npu_current_frequency_mhz"))
    max_freq_mhz = _read_int_file(os.path.join(dev_path, "npu_max_frequency_mhz"))

    if freq_mhz is None and current_freq_mhz is not None:
        freq_mhz = float(current_freq_mhz)

    fw_version = safe_read(os.path.join(debugfs_path, "fw_version")) if debugfs_path else None
    pciid = safe_read(os.path.join(dev_path, "device"))
    module_version = safe_read(os.path.join(driver_path, "module", "version"))
    driver_version = module_version.split(" ")[0] if module_version else None

    processes: List[Dict[str, Any]] = []
    if include_processes and dev_file:
        try:
            processes = get_npu_processes(dev_file)
        except Exception as exc:
            logger.debug("get_npu_processes failed: %s", exc)

    payload: Dict[str, Any] = {
        "timestamp": int(time.time()),
        "pciid": pciid,
        "driver_version": driver_version,
        "fw_version": fw_version,
        "utilization_percent": round(utilization_percent, 3) if utilization_percent is not None else None,
        "frequency_mhz": freq_mhz,
        "current_frequency_mhz": current_freq_mhz,
        "max_frequency_mhz": max_freq_mhz,
        "memory_bytes": memory_bytes,
        "pmt_available": telemetry is not None,
        "pmt_error": pmt_error,
        "processes": processes,
    }

    if telemetry is not None:
        payload["power_w"] = round(power_w, 6) if power_w is not None else None
        payload["tile_config"] = tile_config
        payload["temperature_c"] = temperature_c
        payload["noc_bandwidth_mib_per_s"] = noc_bandwidth

    return {
        "available": True,
        "raw": json.dumps(payload, ensure_ascii=False),
        "error": None,
    }


def get_intel_npu_smi_output(include_processes: bool = False) -> Dict[str, Any]:
    try:
        return _collect_npu_smi_once(include_processes=include_processes)
    except Exception as exc:
        return {"available": False, "raw": None, "error": f"Failed to collect NPU metrics: {exc}"}


def get_intel_npu_processes() -> List[Dict[str, Any]]:
    """Standalone helper to probe NPU processes on demand.

    Not called by the default overview collection; invoke explicitly when
    per-process NPU info is needed.
    """
    driver_path = "/sys/bus/pci/drivers/intel_vpu/"
    if not os.path.exists(driver_path):
        return []
    for entry in os.listdir(driver_path):
        if not entry.startswith("0000:"):
            continue
        accel_path = os.path.join(driver_path, entry, "accel")
        if not os.path.exists(accel_path):
            return []
        accel_entries = os.listdir(accel_path)
        if not accel_entries:
            return []
        dev_file = os.path.join("/dev/accel", accel_entries[0])
        try:
            return get_npu_processes(dev_file)
        except Exception as exc:
            logger.debug("get_npu_processes failed: %s", exc)
            return []
    return []
