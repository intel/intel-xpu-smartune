# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Supported built-in diagnostics event types and their user-facing metadata."""

EVENT_CATALOG = (
    ("RESOURCE_MEMORY_OOM_KILL", "Kernel OOM kill", "Kernel terminated a process because memory was exhausted.", "data-path", False),
    ("RESOURCE_SYSTEM_PRESSURE_CHANGED", "System pressure change", "Overall system pressure entered, changed, or recovered from an elevated state.", "hardware", True),
    ("PLATFORM_KERNEL_PANIC", "Kernel panic", "Kernel reported an unrecoverable panic.", "hardware", False),
    ("DEVICE_GPU_HANG", "GPU hang", "System journal reported a GPU hang.", "compute", False),
    ("PLATFORM_SERVICE_CRASHED", "Service crash", "SmarTune service terminated unexpectedly.", "platform", False),
    ("PLATFORM_SYSTEMD_RESTART_LOOP", "Service restart loop", "Systemd reported repeated service restarts.", "platform", False),
    ("LOG_ERROR", "Service error log", "SmarTune emitted an error-level log record.", "platform", True),
    ("LOG_EXCEPTION", "Service exception", "SmarTune emitted an exception or traceback.", "platform", True),
    ("PLATFORM_SERVICE_STARTED", "Service started", "SmarTune service start was recorded.", "platform", False),
    ("PLATFORM_SERVICE_STOPPED", "Service stopped", "SmarTune service shutdown completed.", "platform", False),
    ("CONTROL_CPU_LIMIT_APPLIED", "CPU limit applied", "SmarTune applied a CPU limit to a workload.", "platform", False),
    ("CONTROL_CPU_LIMIT_RECOVERED", "CPU limit recovered", "SmarTune restored a workload's CPU limit.", "platform", False),
    ("CONTROL_CPU_LIMIT_FAILED", "CPU limit failed", "SmarTune could not apply or restore a workload's CPU limit.", "platform", False),
    ("CONTROL_MEMORY_LIMIT_APPLIED", "Memory limit applied", "SmarTune applied a memory limit to a workload.", "platform", False),
    ("CONTROL_MEMORY_LIMIT_RECOVERED", "Memory limit recovered", "SmarTune restored a workload's memory limit.", "platform", False),
    ("CONTROL_MEMORY_LIMIT_FAILED", "Memory limit failed", "SmarTune could not apply or restore a workload's memory limit.", "platform", False),
    ("CONTROL_DISK_IO_LIMIT_APPLIED", "Disk I/O limit applied", "SmarTune applied a disk I/O limit to a workload.", "platform", False),
    ("CONTROL_DISK_IO_LIMIT_RECOVERED", "Disk I/O limit recovered", "SmarTune restored a workload's disk I/O limit.", "platform", False),
    ("CONTROL_DISK_IO_LIMIT_FAILED", "Disk I/O limit failed", "SmarTune could not apply or restore a workload's disk I/O limit.", "platform", False),
    ("CONTROL_LIFECYCLE_REQUIRES_VERIFICATION", "Control requires verification", "SmarTune detected an interrupted control lifecycle that requires verification.", "platform", False),
    ("CONTROL_LIFECYCLE_CLEARED_BY_REBOOT", "Control cleared by reboot", "A prior control lifecycle was cleared because the host rebooted.", "platform", False),
    ("CONTROL_LIFECYCLE_CLEARED_WITHOUT_RUNTIME_STATE", "Control cleared without runtime state", "A recorded control lifecycle has no matching runtime state.", "platform", False),
    ("WORKLOAD_BENCHMARK_STARTED", "Benchmark started", "A benchmark job started.", "services", True),
    ("WORKLOAD_BENCHMARK_DONE", "Benchmark completed", "A benchmark job completed.", "services", True),
    ("WORKLOAD_BENCHMARK_FAILED", "Benchmark failure", "A benchmark run exited with a non-zero status.", "services", False),
    ("WORKLOAD_BENCHMARK_CANCELLED", "Benchmark cancelled", "A benchmark job was cancelled.", "services", True),
    ("WORKLOAD_MODEL_DOWNLOAD_STARTED", "Model download started", "A model download job started.", "services", True),
    ("WORKLOAD_MODEL_DOWNLOAD_DONE", "Model download completed", "A model download job completed.", "services", True),
    ("WORKLOAD_MODEL_DOWNLOAD_FAILED", "Model download failure", "A model download job failed.", "services", False),
    ("WORKLOAD_MODEL_DOWNLOAD_CANCELLED", "Model download cancelled", "A model download job was cancelled.", "services", True),
    ("WORKLOAD_MODEL_WEIGHTS_DELETED", "Model weights deleted", "A model's downloaded weights were deleted.", "services", True),
    ("WORKLOAD_BENCHMARK_RESULTS_DELETED", "Benchmark results deleted", "Stored benchmark results were deleted.", "services", True),
    ("WORKLOAD_BENCHMARK_CASES_DELETED", "Benchmark cases deleted", "Stored benchmark cases were deleted.", "services", True),
    ("SYSTEM_HARDWARE_INVENTORY_CHANGED", "Hardware inventory changed", "SmarTune detected a hardware inventory change.", "hardware", False),
    ("SYSTEM_SOFTWARE_INVENTORY_CHANGED", "Software inventory changed", "SmarTune detected an operating system, driver, or package inventory change.", "hardware", False),
)

_DEFAULTS = {event_type: True for event_type, _, _, _, configurable in EVENT_CATALOG if configurable}


def defaults():
    """Return the complete event configuration with compatibility defaults."""
    return dict(_DEFAULTS)


def entries():
    """Return user-facing metadata for every built-in diagnostic event."""
    return [
        {
            "event_type": event_type,
            "label": label,
            "description": description,
            "domain": domain,
            "configurable": configurable,
            "default_enabled": _DEFAULTS.get(event_type, True),
        }
        for event_type, label, description, domain, configurable in EVENT_CATALOG
    ]


def enabled(event_type: str) -> bool:
    """Return whether this event type should enter the diagnostics pipeline.

    Unknown event types remain enabled so an older configuration never suppresses
    a newly introduced safety-relevant event before the user can review it.
    """
    if event_type not in _DEFAULTS:
        return True
    try:
        from config.config import b_config

        configured = getattr(b_config, "diagnostic_events", None)
        if isinstance(configured, dict) and event_type in configured:
            return bool(configured[event_type])
    except Exception:
        pass
    return True