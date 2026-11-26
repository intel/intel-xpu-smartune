# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Supported built-in diagnostics event types and their user-facing metadata."""

from typing import NamedTuple, Optional


class ControlActionEvent(NamedTuple):
    """A control change outside the limit lifecycle: its catalog row and how it is written."""
    event_type: str
    label: str
    description: str
    severity: str
    impact: str
    # str.format template: {app} is the app's name (or its cgroups when the app
    # is unknown); any other {name} is filled from the call's facts, "?" if absent.
    summary: str
    resource_type: Optional[str] = None
    snapshot: bool = False      # attach the processes in the app's cgroups
    configurable: bool = False  # the user may turn the event off in Settings
    domain: str = "platform"


# family -> action -> event. Not lifecycle events: they carry no protection_id.
CONTROL_ACTION_EVENTS = {
    "freeze": {
        "FROZEN": ControlActionEvent(
            "CONTROL_APP_FROZEN", "App frozen",
            "SmarTune froze every process of a manually limited app.",
            "info", "degraded", "{app} frozen", snapshot=True),
        "THAWED": ControlActionEvent(
            "CONTROL_APP_THAWED", "App thawed",
            "SmarTune resumed a frozen app (by the user, a limit restore, app exit, "
            "shutdown or startup recovery).",
            "info", "none", "{app} thawed"),
        "FREEZE_FAILED": ControlActionEvent(
            "CONTROL_APP_FREEZE_FAILED", "Freeze failed",
            "SmarTune could not freeze an app.",
            "error", "failed", "Freeze failed for {app}"),
        "THAW_FAILED": ControlActionEvent(
            "CONTROL_APP_THAW_FAILED", "Thaw failed",
            "SmarTune could not resume a frozen app; it may still be paused.",
            "error", "failed", "Thaw failed for {app}"),
        "FROZEN_LONG": ControlActionEvent(
            "CONTROL_APP_FROZEN_LONG", "App frozen too long",
            "An app has stayed frozen longer than the reminder threshold.",
            "warning", "degraded", "{app} has been frozen for {frozen_seconds}s",
            configurable=True),
    },
    "affinity": {
        "SET": ControlActionEvent(
            "CONTROL_CPU_AFFINITY_SET", "CPU affinity set",
            "SmarTune bound an app to a set of CPU cores.",
            "info", "none", "{app} bound to cpus {cpus}", resource_type="cpu", snapshot=True),
        "RESTORED": ControlActionEvent(
            "CONTROL_CPU_AFFINITY_RESTORED", "CPU affinity restored",
            "SmarTune restored an app's original CPU core set.",
            "info", "none", "CPU affinity restored for {app}", resource_type="cpu"),
        "SET_FAILED": ControlActionEvent(
            "CONTROL_CPU_AFFINITY_SET_FAILED", "CPU affinity failed",
            "SmarTune could not bind an app to the requested CPU cores.",
            "error", "failed", "CPU affinity binding failed for {app}", resource_type="cpu"),
        "RESTORE_FAILED": ControlActionEvent(
            "CONTROL_CPU_AFFINITY_RESTORE_FAILED", "CPU affinity restore failed",
            "SmarTune could not restore an app's original CPU core set.",
            "error", "failed", "CPU affinity restore failed for {app}", resource_type="cpu"),
    },
    # Device frequency ranges (per CPU policy / GPU gt, not per app): no app_id.
    "freq": {
        "LIMITED": ControlActionEvent(
            "CONTROL_DEVICE_FREQ_LIMITED", "Device frequency limited",
            "SmarTune set the min/max frequency of CPU policies or GPU gts at the user's request. "
            "Every app on those devices runs at the capped clock.",
            "info", "degraded", "Frequency of {units} limited to {range}"),
        "RESTORED": ControlActionEvent(
            "CONTROL_DEVICE_FREQ_RESTORED", "Device frequency restored",
            "SmarTune put the frequency range of CPU policies or GPU gts back to what it was "
            "before SmarTune changed it (by the user or at shutdown).",
            "info", "none", "Frequency of {units} restored"),
        "LIMIT_FAILED": ControlActionEvent(
            "CONTROL_DEVICE_FREQ_LIMIT_FAILED", "Device frequency limit failed",
            "SmarTune could not set the frequency range of some CPU policies or GPU gts.",
            "error", "failed", "Frequency limit failed for {units}"),
        "RESTORE_FAILED": ControlActionEvent(
            "CONTROL_DEVICE_FREQ_RESTORE_FAILED", "Device frequency restore failed",
            "SmarTune could not put back the frequency range of some CPU policies or GPU gts; "
            "they may still be capped.",
            "error", "failed", "Frequency restore failed for {units}"),
    },
}


def control_action(family, action):
    """The event defined for ``family`` / ``action``, or None."""
    return CONTROL_ACTION_EVENTS.get(family, {}).get(action)


class LifecycleEvent(NamedTuple):
    """One event of a resource-limit lifecycle (the events of one protection_id).

    ``action`` is applied / recovered / failed on ``resource``, or, for a marker on the
    whole lifecycle (``resource`` None), the status it gives the lifecycle.
    """
    event_type: str
    label: str
    description: str
    action: str
    resource: Optional[str] = None


# Lifecycle readers classify events by this table, never by name (a freeze _FAILED is not
# a limit action). Always recorded: the read model is rebuilt from these events.
LIFECYCLE_EVENTS = (
    LifecycleEvent("CONTROL_CPU_LIMIT_APPLIED", "CPU limit applied", "SmarTune applied a CPU limit to a workload.", "applied", "cpu"),
    LifecycleEvent("CONTROL_CPU_LIMIT_RECOVERED", "CPU limit recovered", "SmarTune restored a workload's CPU limit.", "recovered", "cpu"),
    LifecycleEvent("CONTROL_CPU_LIMIT_FAILED", "CPU limit failed", "SmarTune could not apply or restore a workload's CPU limit.", "failed", "cpu"),
    LifecycleEvent("CONTROL_MEMORY_LIMIT_APPLIED", "Memory limit applied", "SmarTune applied a memory limit to a workload.", "applied", "memory"),
    LifecycleEvent("CONTROL_MEMORY_LIMIT_RECOVERED", "Memory limit recovered", "SmarTune restored a workload's memory limit.", "recovered", "memory"),
    LifecycleEvent("CONTROL_MEMORY_LIMIT_FAILED", "Memory limit failed", "SmarTune could not apply or restore a workload's memory limit.", "failed", "memory"),
    LifecycleEvent("CONTROL_DISK_IO_LIMIT_APPLIED", "Disk I/O limit applied", "SmarTune applied a disk I/O limit to a workload.", "applied", "disk_io"),
    LifecycleEvent("CONTROL_DISK_IO_LIMIT_RECOVERED", "Disk I/O limit recovered", "SmarTune restored a workload's disk I/O limit.", "recovered", "disk_io"),
    LifecycleEvent("CONTROL_DISK_IO_LIMIT_FAILED", "Disk I/O limit failed", "SmarTune could not apply or restore a workload's disk I/O limit.", "failed", "disk_io"),
    LifecycleEvent("CONTROL_LIFECYCLE_REQUIRES_VERIFICATION", "Control requires verification", "SmarTune detected an interrupted control lifecycle that requires verification.", "requires_verification"),
    LifecycleEvent("CONTROL_LIFECYCLE_CLEARED_BY_REBOOT", "Control cleared by reboot", "A prior control lifecycle was cleared because the host rebooted.", "cleared_by_reboot"),
    LifecycleEvent("CONTROL_LIFECYCLE_CLEARED_WITHOUT_RUNTIME_STATE", "Control cleared without runtime state", "A recorded control lifecycle has no matching runtime state.", "cleared_without_runtime_state"),
)

# What a per-resource lifecycle event can do, in the order a cap goes through.
RESOURCE_ACTIONS = ("applied", "recovered", "failed")
# Markers that end a whole lifecycle, whatever its resources last reported.
LIFECYCLE_CLEARED_ACTIONS = ("cleared_by_reboot", "cleared_without_runtime_state")

_LIFECYCLE_BY_TYPE = {event.event_type: event for event in LIFECYCLE_EVENTS}
_LIFECYCLE_BY_ACTION = {(event.action, event.resource): event.event_type for event in LIFECYCLE_EVENTS}


def lifecycle_event(event_type):
    """The lifecycle definition of ``event_type``, or None when it is not part of one."""
    return _LIFECYCLE_BY_TYPE.get(event_type or "")


def lifecycle_action(event_type):
    """What an event of ``event_type`` does to its lifecycle, or None."""
    event = lifecycle_event(event_type)
    return event.action if event else None


def lifecycle_event_type(action, resource=None):
    """Event type for ``action`` on ``resource`` (any case; None: whole lifecycle), or None."""
    return _LIFECYCLE_BY_ACTION.get(((action or "").lower(), resource.lower() if resource else None))


_BUILTIN_EVENTS = (
    ("RESOURCE_MEMORY_OOM_KILL", "OOM kill", "The kernel OOM killer or systemd-oomd terminated a process or scope because memory ran out.", "data-path", False),
    ("RESOURCE_MEMORY_RECLAIM_APPLIED", "Memory reclaimed", "SmarTune reclaimed memory from a memory-limited app.", "data-path", True),
    ("RESOURCE_MEMORY_RECLAIM_FAILED", "Memory reclaim failed", "Reclaiming memory from a memory-limited app failed.", "data-path", True),
    ("RESOURCE_MEMORY_RECLAIM_EXHAUSTED", "Memory reclaim found no swap", "A memory reclaim found no swap headroom; SmarTune backs off and asks for more swap.", "data-path", True),
    ("RESOURCE_SYSTEM_PRESSURE_CHANGED", "System pressure change", "Overall system pressure entered, changed, or recovered from an elevated state.", "hardware", True),
    ("PLATFORM_KERNEL_PANIC", "Kernel panic", "Kernel reported an unrecoverable panic.", "hardware", False),
    ("PLATFORM_HOST_UNCLEAN_SHUTDOWN", "Unclean shutdown", "The host stopped without a clean shutdown before this boot (crash, hang, reset or power loss).", "hardware", False),
    ("DEVICE_GPU_HANG", "GPU hang", "System journal reported a GPU hang.", "compute", False),
    ("DEVICE_GPU_THROTTLED", "GPU throttled", "GPU frequency was held down by a power, thermal, or current limit reported by the driver.", "compute", True),
    ("DEVICE_GPU_THROTTLE_CLEARED", "GPU throttling cleared", "GPU throttling ended.", "compute", False),
    ("PLATFORM_KERNEL_HUNG_TASK", "Kernel hung task", "A task was blocked in the kernel (D state) for longer than the hung-task timeout.", "hardware", False),
    ("PLATFORM_KERNEL_OOPS", "Kernel oops", "The kernel hit a BUG, Oops or general protection fault; the system may be unstable.", "hardware", False),
    ("PLATFORM_KERNEL_LOCKUP", "Kernel lockup", "A CPU or workqueue stopped making progress (soft/hard lockup, RCU stall).", "hardware", False),
    ("PLATFORM_KERNEL_WARNING", "Kernel warning", "The kernel or a driver reported a WARN or a non-fatal BUG with a stack trace.", "hardware", True),
    ("PLATFORM_PROCESS_CRASHED", "Process crash", "A process or systemd service was killed by a fatal signal (segfault, trap, abort or core dump).", "services", True),
    ("DEVICE_HARDWARE_ERROR_UNCORRECTED", "Uncorrected hardware error", "Machine check, PCIe AER or memory controller reported an uncorrected error.", "hardware", False),
    ("DEVICE_HARDWARE_ERROR_CORRECTED", "Corrected hardware error", "Machine check, PCIe AER or memory controller reported a corrected error.", "hardware", True),
    ("DEVICE_STORAGE_IO_ERROR", "Storage I/O error", "A block device, NVMe/ATA controller or filesystem reported an I/O error.", "data-path", False),
    ("DEVICE_CPU_THERMAL_THROTTLED", "CPU thermal throttling", "The kernel reported CPU core or package temperature above threshold with clock throttling.", "hardware", True),
    ("PLATFORM_SERVICE_CRASHED", "Service crash", "SmarTune service terminated unexpectedly.", "platform", False),
    ("PLATFORM_SYSTEMD_RESTART_LOOP", "Service restart loop", "Systemd reported repeated service restarts.", "services", False),
    ("LOG_ERROR", "Service error log", "SmarTune emitted an error-level log record.", "platform", True),
    ("LOG_EXCEPTION", "Service exception", "SmarTune emitted an exception or traceback.", "platform", True),
    ("LOG_ERROR_SIGNATURE_NEW", "New error", "An error message never seen before on this host appeared in SmarTune logs or the system journal.", "platform", True),
    ("LOG_ERROR_SIGNATURE_BURST", "Error burst", "An error message is repeating far more often than its own history predicts.", "platform", True),
    ("PLATFORM_SERVICE_STARTED", "Service started", "SmarTune service start was recorded.", "platform", False),
    ("PLATFORM_SERVICE_STOPPED", "Service stopped", "SmarTune service shutdown completed.", "platform", False),
    ("AUTH_ACCESS_DENIED", "API access denied", "An unauthenticated API request was rejected.", "platform", False),
    ("AUTH_LOGIN_RATE_LIMITED", "Login rate limited", "An address was locked out of the login endpoint after repeated invalid attempts.", "platform", False),
    ("APP_OOM_SCORE_REFUSED", "OOM priority request refused", "An API request to set the OOM priority of an unregistered app was refused.", "platform", False),
    ("APP_RESOURCE_LIMIT_REFUSED", "Resource limit request refused", "An API request to limit an unregistered or uncontrolled app was refused.", "platform", False),
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

EVENT_CATALOG = _BUILTIN_EVENTS + tuple(
    (event.event_type, event.label, event.description, "platform", False)
    for event in LIFECYCLE_EVENTS
) + tuple(
    (event.event_type, event.label, event.description, event.domain, event.configurable)
    for actions in CONTROL_ACTION_EVENTS.values()
    for event in actions.values()
)

_DEFAULTS = {event_type: True for event_type, _, _, _, configurable in EVENT_CATALOG if configurable}
_TEXTS = {event_type: (label, description) for event_type, label, description, _, _ in EVENT_CATALOG}
DOMAINS = ("compute", "data-path", "services", "hardware", "platform")
_DOMAIN_BY_TYPE = {event_type: domain for event_type, _, _, domain, _ in EVENT_CATALOG}


def domain_of(event_type, attributes=None):
    """Dashboard domain of an event: a custom rule's choice, else the catalog's, else None."""
    chosen = attributes.get("domain") if isinstance(attributes, dict) else None
    if chosen in DOMAINS:
        return chosen
    return _DOMAIN_BY_TYPE.get(event_type)


def label(event_type):
    """The human name of a catalog event type (the type itself when unknown)."""
    return _TEXTS.get(event_type, (event_type, None))[0]


def description(event_type):
    """The catalog description of an event type, or None."""
    return _TEXTS.get(event_type, (None, None))[1]


def defaults():
    """Return the complete event configuration with compatibility defaults."""
    return dict(_DEFAULTS)


def entries():
    """Return user-facing metadata for every built-in diagnostic event and
    what it does to a limit lifecycle."""
    return [
        {
            "event_type": event_type,
            "label": label,
            "description": description,
            "domain": domain,
            "configurable": configurable,
            "default_enabled": _DEFAULTS.get(event_type, True),
            "lifecycle_action": lifecycle_action(event_type),
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