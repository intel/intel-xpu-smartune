# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Kernel fault signatures: classify one kernel report (lines grouped by kernel_blocks) into
# signals (hung task, WARN, Oops, lockup, crash, MCE / AER / EDAC, storage I/O error, CPU
# thermal throttling) that rules.py turns into events. Only what decides the event, its
# severity and its subject is parsed; the report text travels as evidence for deeper analysis.
# Patterns are anchored on exact printk formats, so driver chatter containing "error" never
# matches. SignatureGate rate-limits repeats on event time, so a replay decides the same.

import collections
import re
from dataclasses import dataclass, field
from typing import Optional

from diagnostics.sources import kernel_blocks

# --- event types --------------------------------------------------------------------

HUNG_TASK = "PLATFORM_KERNEL_HUNG_TASK"
KERNEL_WARNING = "PLATFORM_KERNEL_WARNING"
KERNEL_OOPS = "PLATFORM_KERNEL_OOPS"
KERNEL_LOCKUP = "PLATFORM_KERNEL_LOCKUP"
PROCESS_CRASHED = "PLATFORM_PROCESS_CRASHED"
HW_ERROR_CORRECTED = "DEVICE_HARDWARE_ERROR_CORRECTED"
HW_ERROR_UNCORRECTED = "DEVICE_HARDWARE_ERROR_UNCORRECTED"
STORAGE_IO_ERROR = "DEVICE_STORAGE_IO_ERROR"
CPU_THERMAL_THROTTLED = "DEVICE_CPU_THERMAL_THROTTLED"

# --- patterns -----------------------------------------------------------------------

_HUNG_RE = re.compile(r"^INFO: task (?P<comm>.+):(?P<pid>\d+) blocked for more than (?P<secs>\d+) seconds")
_WARN_RE = re.compile(
    r"^WARNING: (?:CPU: \d+ PID: \d+ at )?(?P<loc>\S+\.[chS]:\d+).*?(?:\[(?P<module>[\w-]+)\])?$")
_KERNEL_RIP_RE = re.compile(
    r"^RIP: 0010:(?P<func>[\w.]+)\+0x[0-9a-f]+/0x[0-9a-f]+(?:\s+\[(?P<module>[\w-]+)\])?")
_BUG_AT_RE = re.compile(r"^kernel BUG at (?P<loc>\S+?)!?$")
# "BUG:" reports the kernel survives: a WARN in all but name.
_MINOR_BUG_RE = re.compile(
    r"^BUG: (?:sleeping function called from invalid context|scheduling while atomic"
    r"|using smp_processor_id\(\) in preemptible|Bad page state|Bad page map|Bad rss-counter"
    r"|workqueue leaked|key \S+ has not been registered|KASAN|KFENCE|KCSAN|UBSAN"
    r"|spinlock|rwlock|Invalid wait context|MAX_LOCKDEP)")
_WQ_LOCKUP_RE = re.compile(r"^BUG: workqueue lockup - ")
# The stuck CPU of a soft ("CPU#5 stuck for 26s") or hard ("hard LOCKUP on cpu 7") lockup.
_LOCKUP_CPU_RE = re.compile(r"CPU#(\d+) stuck for|hard LOCKUP on cpu (\d+)", re.IGNORECASE)
_SEGFAULT_RE = re.compile(
    r"^(?P<comm>.+?)\[(?P<pid>\d+)\]: (?P<reason>segfault) at .*?(?: in (?P<obj>[^\[\s]+)\[|$)")
_TRAP_RE = re.compile(
    r"^traps: (?P<comm>.+?)\[(?P<pid>\d+)\] (?P<reason>general protection fault|trap [\w ]+?)"
    r" ip:.*?(?: in (?P<obj>[^\[\s]+)\[|$)")
_MCE_BANK_RE = re.compile(
    r"CPU (?P<cpu>\d+): Machine Check(?: Exception)?: [0-9a-fx]+ Bank (?P<bank>\d+)"
    r"(?::\s*(?P<status>[0-9a-f]{1,16})\b)?", re.IGNORECASE)
# Wording of a fatal machine check. Not sufficient alone: a fatal error replayed from the
# MCA banks at boot reads like a corrected one, so MCi_STATUS is checked too.
_MCE_FATAL_RE = re.compile(r"Machine Check Exception|Fatal machine check|\buncorrect", re.IGNORECASE)
# IA32_MCi_STATUS bits (Intel SDM vol. 3B, 16.3.2.2).
_MCI_STATUS_VAL = 1 << 63
_MCI_STATUS_UC = 1 << 61
_MCI_STATUS_PCC = 1 << 57


def _mce_uncorrected(bank):
    """Whether a bank record's valid MCi_STATUS has UC or PCC set; None without one."""
    raw = bank.group("status")
    status = int(raw, 16) if raw else 0
    if not status & _MCI_STATUS_VAL:
        return None
    return bool(status & (_MCI_STATUS_UC | _MCI_STATUS_PCC))


_AER_RE = re.compile(
    r"^(?:\S+ (?P<port>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]): )?AER: (?:Multiple )?"
    r"(?P<sev>Corrected|Correctable|Uncorrected|Uncorrectable)\b[^:]*?error"
    r"(?: message)? received(?:: | from )(?P<src>[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])?",
    re.IGNORECASE)
_EDAC_RE = re.compile(r"^EDAC (?:\S+ )?(?P<mc>MC\d+): (?P<count>\d+) (?P<kind>CE|UE) (?P<what>[^(]+)")
# blk-core: "<error name>, dev sda, sector 123 op 0x0:(READ) ..." (5.x+), and the
# older "blk_update_request: I/O error, dev sda, sector 123".
_BLK_ERROR_RE = re.compile(
    r"(?:^|: )(?P<err>(?:critical |recoverable )?[A-Za-z/ ]*error), dev (?P<dev>[\w.-]+), sector \d+")
_BUFFER_IO_RE = re.compile(r"^Buffer I/O error on dev (?P<dev>[\w.-]+),")
_FS_ERROR_RE = re.compile(
    r"^(?P<fs>EXT[234]|BTRFS)-?(?:fs)? (?:error|critical) \(device (?P<dev>[^)]+)\)"
    r"|^(?P<fs2>XFS) \((?P<dev2>[^)]+)\): (?:.*I/O error|Corruption|.*[Ss]hut ?down)")
_FS_READONLY_RE = re.compile(
    r"^(?P<fs>EXT[234]|BTRFS|XFS)[-\w]* \((?:device )?(?P<dev>[^)]+)\): "
    r"(?:Remounting filesystem read-only|forced shutdown|.*filesystem being remounted read-only)",
    re.IGNORECASE)
_NVME_RE = re.compile(
    r"^nvme (?P<dev>nvme\d+): (?P<what>.*?\btimeout, (?:aborting|reset)\w*|controller is down; will reset"
    r"|Device not ready; aborting \w+|Removing after probe failure.*|Disabling device after reset failure.*)")
_ATA_RE = re.compile(
    r"^(?P<dev>ata\d+(?:\.\d+)?): (?P<what>exception Emask [0-9a-fx]+.*|failed command: .+"
    r"|status: \{ DRDY ERR \}|error: \{ UNC \}|COMRESET failed.*)")
_THERMAL_RE = re.compile(
    r"^(?:mce: )?CPU(?P<cpu>\d+): (?P<scope>Core|Package) temperature (?:is )?above threshold,"
    r" cpu clock (?:is )?throttled")


@dataclass
class Signal:
    event_type: str
    severity: str
    category: str
    summary: str
    subject: str
    record: object                      # the LogRecord the event is anchored on
    attributes: dict = field(default_factory=dict)
    resource_type: Optional[str] = None
    impact: Optional[str] = None
    burst: int = 1                      # events admitted per window and subject
    window: float = 600.0
    service: Optional[str] = None


class SignatureGate:
    """Per-key rate limit over event time: admits ``burst`` per ``window``, counts the rest.
    A ``value`` (such as a restart counter) that did not increase starts a new episode."""

    def __init__(self, max_keys=4096):
        self._state = collections.OrderedDict()
        self._max_keys = max_keys

    def admit(self, key, ts, *, burst=1, window=600.0, value=None):
        st = self._state.pop(key, None)
        new_episode = (st is not None and value is not None and st.get("value") is not None
                       and value <= st["value"])
        if st is None:
            st = {"start": ts, "emitted": 0, "seen": 0, "suppressed": 0, "value": None}
        elif new_episode:
            # The previous episode's tail belongs to it, not to this one.
            st.update(start=ts, emitted=0, seen=0, suppressed=0)
        elif ts - st["start"] >= window or ts < st["start"]:
            st.update(start=ts, emitted=0)
        st["seen"] += 1
        st["value"] = value
        admitted = st["emitted"] < burst
        info = {"occurrences": st["seen"], "suppressed_since_last": st["suppressed"]}
        if admitted:
            st["emitted"] += 1
            st["suppressed"] = 0
        else:
            st["suppressed"] += 1
        self._state[key] = st
        while len(self._state) > self._max_keys:
            self._state.popitem(last=False)
        return admitted, info


# --- block classification -----------------------------------------------------------

def _first(regex, lines):
    for line in lines:
        match = regex.search(line)
        if match:
            return match
    return None


def _block_kind(group):
    """Header kind of a report; "[ cut here ]" defers to the header after it."""
    if len(group) > 1 and kernel_blocks.is_cut_here(group[0].message):
        return kernel_blocks.kind_after_cut_here(group)
    return kernel_blocks.header_kind(group[0].message)


def classify_block(group):
    """Signals carried by one kernel report as a whole (header-driven)."""
    if not group or not kernel_blocks.is_kernel(group[0]):
        return []
    kind = _block_kind(group)
    if kind is None:
        return []
    lines = [(rec.message or "") for rec in group]
    title = kernel_blocks.block_title(group)
    head = group[0]
    common = {"kernel_block": kind, "block_lines": len(group), "headline": title[:300]}

    if kind == "hung_task":
        match = _HUNG_RE.search(title)
        if not match:
            return []
        comm, pid, secs = match.group("comm"), match.group("pid"), int(match.group("secs"))
        return [Signal(
            HUNG_TASK, "error", "platform.availability",
            f"Task {comm} (pid {pid}) blocked for more than {secs}s",
            subject=f"{comm}:{pid}", record=head, impact="degraded",
            attributes={"process": comm, "pid": pid, "blocked_seconds": secs, **common},
            # The kernel re-reports a still-blocked task every timeout period.
            burst=1, window=600)]

    if kind in ("soft_lockup", "hard_lockup", "rcu_stall") or (
            kind == "bug" and _WQ_LOCKUP_RE.search(title)):
        return [_lockup_signal("workqueue_lockup" if kind == "bug" else kind, title, lines, head,
                               common)]

    if kind == "warning":
        match = _first(_WARN_RE, lines)
        loc = match.group("loc") if match else title[:120]
        module = (match.group("module") if match else None) or _rip_module(lines)
        return [Signal(
            KERNEL_WARNING, "warning", "platform.kernel",
            f"Kernel WARNING at {loc}{f' [{module}]' if module else ''}",
            subject=loc, record=head,
            attributes={"location": loc, "module": module, **common},
            burst=1, window=3600)]

    if kind in ("bug", "oops", "gpf"):
        if _MINOR_BUG_RE.search(title):
            return [Signal(
                KERNEL_WARNING, "warning", "platform.kernel", f"Kernel bug report: {title[:160]}",
                subject=re.sub(r"\d+", "#", title)[:120], record=head,
                attributes={"module": _rip_module(lines), **common},
                burst=1, window=3600)]
        rip = _first(_KERNEL_RIP_RE, lines)
        bug_at = _BUG_AT_RE.search(title)
        func = rip.group("func") if rip else None
        module = rip.group("module") if rip else None
        subject = (f"{func} [{module}]" if module else func) if func else (
            bug_at.group("loc") if bug_at else re.sub(r"[0-9a-f]{8,}", "#", title)[:120])
        return [Signal(
            KERNEL_OOPS, "critical", "platform.availability",
            f"Kernel oops: {title[:140]}{f' in {subject}' if func else ''}",
            subject=subject, record=head, impact="failed",
            attributes={"function": func, "module": module,
                        "location": bug_at.group("loc") if bug_at else None, **common},
            burst=1, window=600)]

    if kind in ("segfault", "trap"):
        match = (_SEGFAULT_RE if kind == "segfault" else _TRAP_RE).search(title)
        if not match:
            return []
        comm, pid, obj, reason = (match.group("comm"), match.group("pid"), match.group("obj"),
                                  match.group("reason"))
        return [Signal(
            PROCESS_CRASHED, "error", "platform.process",
            f"Process {comm} (pid {pid}) crashed: {reason}{f' in {obj}' if obj else ''}",
            subject=f"{comm}@{obj or '?'}", record=head, impact="failed",
            attributes={"process": comm, "pid": pid, "reason": reason, "object": obj, **common},
            # Each crash is a real failed run, but a crash loop must not flood.
            burst=5, window=600)]

    if kind == "mce":
        text = "\n".join(lines)
        # A boot-time replay lists every bank holding an error at once: the worst one decides.
        banks = [(bank, _mce_uncorrected(bank)) for bank in _MCE_BANK_RE.finditer(text)]
        worst = max(banks, key=lambda item: bool(item[1]), default=(None, None))[0]
        uncorrected = bool(_MCE_FATAL_RE.search(text)) or any(bad for _, bad in banks)
        subject = f"cpu{worst.group('cpu')}:bank{worst.group('bank')}" if worst else "mce"
        return [_hw_signal("mce", uncorrected, subject, f"Machine check ({subject})", head,
                           {"mci_status": worst.group("status") if worst else None, **common})]

    if kind == "aer":
        aer = _AER_RE.search(title)
        severity = aer.group("sev") if aer else "error"
        device = ((aer.group("src") or aer.group("port")) if aer else None) or (
            head.fields or {}).get("kernel_pci") or "pcie"
        return [_hw_signal("aer", severity.lower().startswith("uncorrect"), device.lower(),
                           f"PCIe AER {severity} error on {device}", head,
                           {"device": device.lower(), **common})]
    return []


def _rip_module(lines):
    rip = _first(_KERNEL_RIP_RE, lines)
    return rip.group("module") if rip else None


_LOCKUP_LABELS = {"soft_lockup": "soft lockup", "hard_lockup": "hard lockup",
                  "rcu_stall": "RCU stall", "workqueue_lockup": "workqueue lockup"}


def _lockup_signal(kind, title, lines, head, common):
    match = _first(_LOCKUP_CPU_RE, lines) if kind in ("soft_lockup", "hard_lockup") else None
    cpu = (match.group(1) or match.group(2)) if match else None
    return Signal(
        KERNEL_LOCKUP, "critical" if kind == "hard_lockup" else "error", "platform.availability",
        f"Kernel {_LOCKUP_LABELS[kind]}{f' on CPU {cpu}' if cpu is not None else ''}: {title[:140]}",
        subject=f"{kind}:cpu{cpu}" if cpu is not None else kind, record=head, impact="degraded",
        attributes={"lockup": kind, "cpu": cpu, "module": _rip_module(lines), **common},
        burst=1, window=600)


def _hw_signal(source_kind, uncorrected, subject, summary, record, attributes):
    return Signal(
        HW_ERROR_UNCORRECTED if uncorrected else HW_ERROR_CORRECTED,
        "critical" if uncorrected else "warning", "device.hardware",
        f"{'Uncorrected' if uncorrected else 'Corrected'} hardware error: {summary}",
        subject=f"{source_kind}:{subject}", record=record,
        impact="failed" if uncorrected else None,
        attributes={"hw_source": source_kind, "corrected": not uncorrected, **attributes},
        # Corrected errors are a rate, not an incident: keep a few per window.
        burst=1 if uncorrected else 3, window=600)


# --- single-line signatures ---------------------------------------------------------

def classify_line(rec):
    """Signals carried by one kernel line on its own (storage, EDAC, thermal)."""
    if not kernel_blocks.is_kernel(rec):
        return []
    message = (rec.message or "").strip()
    if not message:
        return []

    match = _FS_READONLY_RE.search(message)
    if match:
        dev = match.group("dev")
        return [_storage_signal(rec, dev, "filesystem_readonly", "critical",
                                f"{match.group('fs')} filesystem on {dev} went read-only",
                                {"filesystem": match.group("fs"), "read_only": True}, burst=1)]
    match = _BLK_ERROR_RE.search(message)
    if match:
        dev = match.group("dev")
        return [_storage_signal(rec, dev, "block_io", "error",
                                f"Storage {match.group('err')} on {dev}", {})]
    match = _BUFFER_IO_RE.search(message)
    if match:
        dev = match.group("dev")
        return [_storage_signal(rec, dev, "buffer_io", "error", f"Buffer I/O error on {dev}", {})]
    match = _FS_ERROR_RE.search(message)
    if match:
        fs = match.group("fs") or match.group("fs2")
        dev = match.group("dev") or match.group("dev2")
        return [_storage_signal(rec, dev, "filesystem", "error", f"{fs} filesystem error on {dev}",
                                {"filesystem": fs})]
    match = _NVME_RE.search(message)
    if match:
        dev = match.group("dev")
        return [_storage_signal(rec, dev, "nvme", "error", f"NVMe {dev}: {match.group('what')[:120]}", {})]
    match = _ATA_RE.search(message)
    if match:
        dev = match.group("dev")
        return [_storage_signal(rec, dev, "ata", "error", f"ATA {dev}: {match.group('what')[:120]}", {})]

    match = _EDAC_RE.search(message)
    if match:
        mc, kind = match.group("mc"), match.group("kind")
        return [_hw_signal("edac", kind == "UE", mc,
                           f"{match.group('count')} {kind} on {mc}: {match.group('what').strip()[:80]}",
                           rec, {"memory_controller": mc})]

    match = _THERMAL_RE.search(message)
    if match:
        scope = match.group("scope").lower()
        return [Signal(
            CPU_THERMAL_THROTTLED, "warning", "device.thermal",
            f"CPU {scope} temperature above threshold; clock throttled (CPU {match.group('cpu')})",
            subject=f"cpu-{scope}", record=rec, resource_type="cpu", impact="degraded",
            attributes={"scope": scope, "cpu": match.group("cpu")},
            burst=1, window=600)]
    return []


def _storage_signal(rec, dev, kind, severity, summary, attributes, burst=3):
    return Signal(
        STORAGE_IO_ERROR, severity, "device.storage", summary,
        subject=dev, record=rec, resource_type="storage",
        impact="failed" if severity == "critical" else "degraded",
        attributes={"device": dev, "io_error_kind": kind, "read_only": False, **attributes},
        burst=burst, window=600)


# --- user-space crashes -------------------------------------------------------------

def classify_coredump(rec):
    """A systemd-coredump record: the crash, with the unit/cgroup it ran in."""
    fields = rec.fields or {}
    pid = fields.get("coredump_pid")
    if not pid:
        return []
    comm = fields.get("coredump_comm") or "?"
    signal = fields.get("coredump_signal")
    exe = fields.get("coredump_exe")
    return [Signal(
        PROCESS_CRASHED, "error", "platform.process",
        f"Process {comm} (pid {pid}) dumped core{f' ({signal})' if signal else ''}",
        subject=f"{comm}@{exe or '?'}", record=rec, impact="failed",
        service=fields.get("coredump_unit"),
        attributes={"process": comm, "pid": str(pid), "signal": signal, "object": exe,
                    "unit": fields.get("coredump_unit"), "cgroup": fields.get("coredump_cgroup")},
        burst=5, window=600)]


# systemd's line when a service's main process dies: "foo.service: Main process exited,
# code=dumped, status=11/SEGV". Killed (no core) by one of _CRASH_SIGNALS is a crash too.
_UNIT_EXIT_RE = re.compile(r"Main process exited, code=(?P<code>dumped|killed), status=\d+/(?P<signal>[A-Z]+)")
_CRASH_SIGNALS = {"SEGV", "ABRT", "BUS", "FPE", "ILL", "TRAP", "SYS"}


def classify_unit_exit(rec):
    """A systemd service whose main process died of a crash signal."""
    if rec.logger != "systemd" or kernel_blocks.is_kernel(rec):
        return []
    match = _UNIT_EXIT_RE.search(rec.message or "")
    if not match or (match.group("code") == "killed" and match.group("signal") not in _CRASH_SIGNALS):
        return []
    unit = (rec.fields or {}).get("unit") or (rec.message or "").partition(":")[0]
    signal = f"SIG{match.group('signal')}"
    return [Signal(
        PROCESS_CRASHED, "error", "platform.process", f"Service {unit} crashed ({signal})",
        subject=unit, record=rec, impact="failed", service=unit,
        attributes={"process": unit, "unit": unit, "signal": signal,
                    "core_dumped": match.group("code") == "dumped"},
        burst=5, window=600)]
