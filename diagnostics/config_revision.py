# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Configuration-revision chain: versions the host's hardware/software
# inventory so a BIOS/DIMM/disk/driver/firmware change becomes a diagnostics
# event and a stable id other events (a benchmark run) can point back to.
#
# This module is NOT a collector -- monitor.system_info.collect_static_info()
# already reads BIOS/CPU/memory/disk/GPU-XPU/NIC/GuC-HuC/driver-package
# versions and already degrades a failed read to None (monitor/metrics/utils.
# safe_read/run_cmd never raise). What is missing, and what this module adds,
# is versioning: turning that point-in-time snapshot into a fingerprinted
# chain, distinguishing "this field failed to read just now" from "this field
# is genuinely gone", and emitting one diagnostics event per real change.

import hashlib
import json
import threading
import time
import uuid

from diagnostics.emitter import emit_event
from diagnostics.system_info import read_boot_id
from utils.logger import get_logger

logger = get_logger(__name__)

# A field must read as absent on two consecutive checks before it is called
# "removed" -- a single failed dmidecode/lsblk/debugfs read must not be
# mistaken for a DIMM or disk actually having been pulled.
_REMOVED_AFTER_MISSES = 2

_LOOP_STARTED = False
_LOOP_START_LOCK = threading.Lock()
_RECHECK_INTERVAL_SEC = 86400.0
_INITIAL_DELAY_SEC = 60.0


def _hw_view(inventory: dict) -> dict:
    """Stable hardware fields worth fingerprinting. Deliberately excludes
    live/current readings (per-core current frequency, NIC IP leases, disk
    free space) that collect_static_info() mixes in alongside truly static
    fields -- those churn on every recheck and would make every revision
    look "changed" for no real reason."""
    bios = inventory.get("bios") or {}
    cpu = inventory.get("cpu") or {}
    memory = inventory.get("memory") or {}
    disk = inventory.get("disk") or {}
    network = inventory.get("network") or {}
    gpu = inventory.get("gpu") or {}
    npu = inventory.get("npu") or {}
    return {
        "bios_version": bios.get("version"),
        "cpu_model": cpu.get("model_name"),
        "cpu_core_count": cpu.get("core_count"),
        "memory_total_gb": memory.get("total_gb"),
        "memory_ddr_speeds": memory.get("ddr_speeds"),
        "memory_devices": memory.get("devices"),
        "disk_device_count": disk.get("device_count"),
        "disk_total_size_bytes": disk.get("total_size_bytes"),
        "disk_devices": [{"name": d.get("name"), "size_bytes": d.get("size_bytes")}
                         for d in (disk.get("devices") or [])],
        "network_nic_count": network.get("nic_count"),
        "network_speeds_mbps": network.get("network_speeds_mbps"),
        "gpu_names": gpu.get("names"),
        "gpu_count": gpu.get("count"),
        "gpu_vram": gpu.get("vram"),
        "gpu_eu_count": gpu.get("eu_count"),
        "gpu_pci_addresses": gpu.get("pci_addresses"),
        "gpu_freq_bounds_mhz": gpu.get("freq_bounds_mhz"),
        "gpu_gt_freq_bounds_mhz": gpu.get("gt_freq_bounds_mhz"),
        "npu_names": npu.get("names"),
        "npu_freq_bounds_mhz": npu.get("freq_bounds_mhz"),
    }


def _sw_view(inventory: dict) -> dict:
    """Stable software/driver/firmware fields worth fingerprinting."""
    os_info = inventory.get("os") or {}
    driver = inventory.get("driver") or {}
    gpu = inventory.get("gpu") or {}
    return {
        "os_version": os_info.get("version"),
        "kernel_version": driver.get("kernel_version"),
        "kernel_cmdline": driver.get("kernel_cmdline"),
        "guc_fw": driver.get("guc_fw"),
        "huc_fw": driver.get("huc_fw"),
        "mesa": driver.get("mesa"),
        "opencl": driver.get("opencl"),
        "level_zero": driver.get("level_zero"),
        "media": driver.get("media"),
        "npu_fw": driver.get("npu_fw"),
        "gpu_driver_names": gpu.get("driver_names"),
    }


def collect_inventory() -> dict:
    """Force a fresh read and split it into the hw/sw views this module
    versions. A caller wanting the raw payload uses
    ``monitor.system_info.collect_static_info`` directly."""
    from monitor.system_info import collect_static_info
    inventory = collect_static_info(force_refresh=True) or {}
    return {"hw": _hw_view(inventory), "sw": _sw_view(inventory)}


def _classify_and_diff(current: dict, prev_status: dict):
    """One field-status pass. Returns ``(new_status, changes)``.

    ``prev_status`` maps field name -> {value, state, miss_streak} from the
    last check (whether or not it created a revision). ``state`` is one of
    present / unavailable (never seen a real value) / unknown (a real value
    exists but the latest read failed once) / removed (failed
    ``_REMOVED_AFTER_MISSES`` times in a row).
    """
    new_status = {}
    changes = []
    for field, value in current.items():
        prev = prev_status.get(field) or {"value": None, "state": "unavailable", "miss_streak": 0}
        prev_value = prev.get("value")
        prev_state = prev.get("state", "unavailable")
        miss_streak = int(prev.get("miss_streak", 0))

        if value is None:
            if prev_state == "present":
                miss_streak += 1
                if miss_streak >= _REMOVED_AFTER_MISSES:
                    new_status[field] = {"value": None, "state": "removed", "miss_streak": miss_streak}
                    changes.append({"field": field, "previous": prev_value, "current": None, "change": "removed"})
                else:
                    # One failed read is not proof of removal -- keep the last
                    # known value alive for fingerprinting until the streak
                    # confirms it.
                    new_status[field] = {"value": prev_value, "state": "unknown", "miss_streak": miss_streak}
            elif prev_state == "unknown":
                miss_streak += 1
                if miss_streak >= _REMOVED_AFTER_MISSES:
                    new_status[field] = {"value": None, "state": "removed", "miss_streak": miss_streak}
                    changes.append({"field": field, "previous": prev_value, "current": None, "change": "removed"})
                else:
                    new_status[field] = {"value": prev_value, "state": "unknown", "miss_streak": miss_streak}
            else:
                # already unavailable/removed -- still absent, nothing new
                new_status[field] = {"value": prev_value, "state": prev_state, "miss_streak": miss_streak}
        else:
            if prev_state in ("unavailable", "removed"):
                new_status[field] = {"value": value, "state": "present", "miss_streak": 0}
                changes.append({"field": field, "previous": prev_value, "current": value,
                               "change": "added" if prev_state == "removed" else "discovered"})
            elif value != prev_value:
                new_status[field] = {"value": value, "state": "present", "miss_streak": 0}
                changes.append({"field": field, "previous": prev_value, "current": value, "change": "modified"})
            else:
                new_status[field] = {"value": value, "state": "present", "miss_streak": 0}
    return new_status, changes


def _fingerprint(field_status: dict) -> str:
    """Hash only the fields currently known ``present`` -- an unavailable,
    unknown or removed field must not make the fingerprint flap every check."""
    present = {field: entry["value"] for field, entry in sorted(field_status.items())
              if entry.get("state") == "present"}
    payload = json.dumps(present, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _new_revision_id() -> str:
    return uuid.uuid4().hex[:16]


def ensure_baseline():
    """Create revision 1 if no revision exists yet. Called once at diagnostics
    mount; a no-op on every later start since a revision already exists."""
    from db.DatabaseModel import ConfigRevision, DBStatus

    if ConfigRevision.latest() is not None:
        return
    inventory = collect_inventory()
    hw_status, _ = _classify_and_diff(inventory["hw"], {})
    sw_status, _ = _classify_and_diff(inventory["sw"], {})
    field_status = {"hw": hw_status, "sw": sw_status}
    revision_id = _new_revision_id()
    status = ConfigRevision.insert_revision(
        revision_id=revision_id, previous_revision_id=None, boot_id=read_boot_id(),
        hw_fingerprint=_fingerprint(hw_status), sw_fingerprint=_fingerprint(sw_status),
        full_inventory={"hw": inventory["hw"], "sw": inventory["sw"]},
        change_summary=None, field_status=field_status,
    )
    if status != DBStatus.SUCCESS:
        logger.error("Config revision baseline insert failed: revision=%s status=%s", revision_id, status)
        return
    logger.info("Config revision baseline created: %s", revision_id)


def current_revision_id():
    """The active revision id, or None before ``ensure_baseline`` has run."""
    from db.DatabaseModel import ConfigRevision

    latest = ConfigRevision.latest()
    return latest.revision_id if latest is not None else None


def query_revisions_range(start_time=None, end_time=None, limit=200):
    """Return normalized revisions created within an inclusive epoch range."""
    from db.DatabaseModel import ConfigRevision

    revisions = []
    for revision in ConfigRevision.query_range(start_time=start_time, end_time=end_time, limit=limit):
        try:
            changes = json.loads(revision.change_summary_json) if revision.change_summary_json else None
        except (TypeError, ValueError):
            changes = None
        revisions.append({
            "revision_id": revision.revision_id,
            "previous_revision_id": revision.previous_revision_id,
            "boot_id": revision.boot_id,
            "created_at": revision.create_time,
            "checked_at": revision.checked_at,
            "change_summary": changes,
        })
    return revisions


def check_and_record_if_changed():
    """Recheck the inventory against the latest revision. Creates a new
    revision (and emits a diagnostics event) only when a field genuinely
    changed; an unchanged recheck just reconfirms ``checked_at`` (and persists
    any in-progress miss-streak) on the existing revision. Best-effort: never
    raises into the periodic loop that calls it."""
    from db.DatabaseModel import ConfigRevision, DBStatus

    try:
        latest = ConfigRevision.latest()
        if latest is None:
            ensure_baseline()
            return

        prev_status = json.loads(latest.field_status_json) if latest.field_status_json else {}
        prev_hw_status = prev_status.get("hw") or {}
        prev_sw_status = prev_status.get("sw") or {}

        inventory = collect_inventory()
        hw_status, hw_changes = _classify_and_diff(inventory["hw"], prev_hw_status)
        sw_status, sw_changes = _classify_and_diff(inventory["sw"], prev_sw_status)

        from diagnostics.event_store import now_iso
        ts_utc = now_iso()

        if not hw_changes and not sw_changes:
            if not ConfigRevision.touch_checked_at(
                    latest.revision_id, ts_utc, field_status={"hw": hw_status, "sw": sw_status}):
                logger.warning("Config revision check timestamp update failed: %s", latest.revision_id)
            return

        boot_id = read_boot_id()
        time_accuracy = "observed_after_restart" if (latest.boot_id and boot_id and latest.boot_id != boot_id) else "exact"
        revision_id = _new_revision_id()
        field_status = {"hw": hw_status, "sw": sw_status}
        status = ConfigRevision.insert_revision(
            revision_id=revision_id, previous_revision_id=latest.revision_id, boot_id=boot_id,
            hw_fingerprint=_fingerprint(hw_status), sw_fingerprint=_fingerprint(sw_status),
            full_inventory={"hw": inventory["hw"], "sw": inventory["sw"]},
            change_summary={"hw": hw_changes, "sw": sw_changes}, field_status=field_status,
            ts_utc=ts_utc,
        )
        if status != DBStatus.SUCCESS:
            logger.error(
                "Config revision insert failed: previous=%s revision=%s status=%s",
                latest.revision_id, revision_id, status,
            )
            return
        logger.info("Config revision changed: %s -> %s (hw=%d, sw=%d changes)",
                   latest.revision_id, revision_id, len(hw_changes), len(sw_changes))

        if hw_changes:
            emit_event(
                "SYSTEM_HARDWARE_INVENTORY_CHANGED", severity="warning", category="platform.config",
                summary=f"Hardware inventory changed ({len(hw_changes)} field(s))",
                source="config_revision", attributes={
                    "changes": hw_changes, "previous_revision_id": latest.revision_id,
                    "time_accuracy": time_accuracy,
                },
                config_revision_id=revision_id, identity=f"hw:{revision_id}",
            )
        if sw_changes:
            emit_event(
                "SYSTEM_SOFTWARE_INVENTORY_CHANGED", severity="info", category="platform.config",
                summary=f"Software/driver inventory changed ({len(sw_changes)} field(s))",
                source="config_revision", attributes={
                    "changes": sw_changes, "previous_revision_id": latest.revision_id,
                    "time_accuracy": time_accuracy,
                },
                config_revision_id=revision_id, identity=f"sw:{revision_id}",
            )
    except Exception as exc:
        logger.warning("Config revision recheck failed: %s", exc)


def start_config_revision_loop():
    """Start the background recheck thread. Idempotent, mirrors the detector
    loop's started-flag + daemon Thread pattern. A short settle delay avoids
    contending with other startup-time work for dmidecode/lsblk/debugfs reads."""
    global _LOOP_STARTED
    with _LOOP_START_LOCK:
        if _LOOP_STARTED:
            return
        _LOOP_STARTED = True

    def loop():
        time.sleep(_INITIAL_DELAY_SEC)
        try:
            ensure_baseline()
        except Exception as exc:
            logger.error("Config revision baseline initialization failed: %s", exc, exc_info=True)
        while True:
            time.sleep(_RECHECK_INTERVAL_SEC)
            try:
                check_and_record_if_changed()
            except Exception as exc:
                logger.warning("Config revision loop iteration failed: %s", exc)

    t = threading.Thread(target=loop, daemon=True, name="diag-config-revision")
    t.start()
