# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Classifying kernel reports and single kernel lines into fault signals."""

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kernel_samples import (  # noqa: E402
    AER_CORRECTED, AER_UNCORRECTED, DRM_WARN, HARD_LOCKUP, HUNG_TASK_GPU, HUNG_TASK_IO,
    KERNEL_BUG_AT, MCE_CORRECTED, MCE_REPLAYED_FATAL, NULL_DEREF_OOPS, OOM_KILL, RCU_STALL,
    SEGFAULT, SLEEPING_IN_ATOMIC, SOFT_LOCKUP, TRAP_GPF, TRAP_INVALID_OPCODE, WARN_XE, WQ_LOCKUP,
    kernel_records, user_record,
)

from diagnostics.detectors import kernel_signatures as ks  # noqa: E402


def _block(text, **kwargs):
    signals = ks.classify_block(kernel_records(text, **kwargs))
    if len(signals) != 1:
        raise AssertionError(f"expected one signal, got {signals}")
    return signals[0]


def _line(message):
    return ks.classify_line(kernel_records(message)[0])


class HungTaskTest(unittest.TestCase):

    def test_blocked_task(self):
        signal = _block(HUNG_TASK_GPU)
        self.assertEqual((signal.event_type, signal.severity, signal.subject),
                         (ks.HUNG_TASK, "error", "python3:4321"))
        attrs = signal.attributes
        self.assertEqual((attrs["process"], attrs["pid"], attrs["blocked_seconds"]),
                         ("python3", "4321", 122))
        self.assertEqual(attrs["block_lines"], len(HUNG_TASK_GPU.split("\n")))

    def test_task_name_with_a_slash(self):
        self.assertEqual(_block(HUNG_TASK_IO).subject, "jbd2/nvme0n1p2-8:512")


class WarningAndOopsTest(unittest.TestCase):

    def test_warn_names_location_and_module(self):
        signal = _block(WARN_XE)
        self.assertEqual((signal.event_type, signal.severity), (ks.KERNEL_WARNING, "warning"))
        self.assertEqual((signal.subject, signal.attributes["module"]),
                         ("drivers/gpu/drm/xe/xe_gt.c:123", "xe"))
        self.assertEqual(signal.summary, "Kernel WARNING at drivers/gpu/drm/xe/xe_gt.c:123 [xe]")

    def test_drm_warn_with_message_before_the_warning_line(self):
        signal = _block(DRM_WARN)
        self.assertEqual((signal.event_type, signal.subject),
                         (ks.KERNEL_WARNING, "drivers/gpu/drm/xe/xe_vm.c:1043"))

    def test_null_dereference_is_an_oops_in_the_faulting_function(self):
        signal = _block(NULL_DEREF_OOPS)
        self.assertEqual((signal.event_type, signal.severity, signal.impact),
                         (ks.KERNEL_OOPS, "critical", "failed"))
        self.assertEqual((signal.subject, signal.attributes["module"]), ("xe_bo_move [xe]", "xe"))

    def test_kernel_bug_at_keeps_its_source_location(self):
        signal = _block(KERNEL_BUG_AT)
        self.assertEqual(signal.event_type, ks.KERNEL_OOPS)
        self.assertEqual((signal.subject, signal.attributes["location"]), ("kfree", "mm/slub.c:432"))

    def test_survivable_bug_report_is_a_warning(self):
        signal = _block(SLEEPING_IN_ATOMIC)
        self.assertEqual((signal.event_type, signal.severity), (ks.KERNEL_WARNING, "warning"))
        self.assertNotIn("580", signal.subject)  # line numbers do not split the subject


class LockupTest(unittest.TestCase):

    def test_soft_lockup(self):
        signal = _block(SOFT_LOCKUP)
        self.assertEqual((signal.event_type, signal.severity, signal.subject),
                         (ks.KERNEL_LOCKUP, "error", "soft_lockup:cpu5"))
        self.assertEqual((signal.attributes["cpu"], signal.attributes["module"]), ("5", "xe"))

    def test_hard_lockup_is_critical(self):
        signal = _block(HARD_LOCKUP)
        self.assertEqual((signal.severity, signal.subject), ("critical", "hard_lockup:cpu7"))

    def test_rcu_stalls(self):
        expedited = ("rcu: INFO: rcu_preempt detected expedited stalls on CPUs/tasks: "
                     "{ 3-.... } 21004 jiffies s: 1213 root: 0x8/.")
        for text in (RCU_STALL, expedited):
            with self.subTest(text[:40]):
                signal = _block(text)
                self.assertEqual((signal.event_type, signal.subject), (ks.KERNEL_LOCKUP, "rcu_stall"))

    def test_workqueue_lockup(self):
        signal = _block(WQ_LOCKUP)
        self.assertEqual((signal.subject, signal.attributes["lockup"]),
                         ("workqueue_lockup", "workqueue_lockup"))


class CrashTest(unittest.TestCase):

    def test_segfault(self):
        signal = _block(SEGFAULT)
        self.assertEqual((signal.event_type, signal.subject), (ks.PROCESS_CRASHED, "python3@libfoo.so.1"))
        attrs = signal.attributes
        self.assertEqual((attrs["pid"], attrs["reason"], attrs["object"]), ("4242", "segfault", "libfoo.so.1"))

    def test_segfault_outside_any_mapping(self):
        signal = _block("python3[4242]: segfault at 0 ip 0000000000000000 sp 00007ffd1e2f3a40 error 14"
                        " likely on CPU 3 (core 3, socket 0)")
        self.assertEqual((signal.subject, signal.attributes["object"]), ("python3@?", None))

    def test_traps(self):
        gpf = _block(TRAP_GPF)
        self.assertEqual((gpf.attributes["reason"], gpf.subject),
                         ("general protection fault", "ollama@libggml.so"))
        opcode = _block(TRAP_INVALID_OPCODE)
        self.assertEqual((opcode.attributes["reason"], opcode.attributes["object"]),
                         ("trap invalid opcode", "app"))

    def test_task_name_with_spaces(self):
        segfault = _block("Web Content[4242]: segfault at 0 ip 00007f3a2b1c4d5e sp 00007ffd1e2f3a40"
                          " error 4 in libxul.so[7f3a2b1a0000+30000]")
        self.assertEqual(segfault.subject, "Web Content@libxul.so")
        trap = _block("traps: Isolated Web Co[99] general protection fault ip:7f00c0ffee00"
                      " sp:7ffd00000000 error:0 in libxul.so[7f00c0f00000+100000]")
        self.assertEqual(trap.attributes["process"], "Isolated Web Co")

    def test_coredump_record(self):
        record = user_record("Process 4242 (python3) of user 1000 dumped core.", 1.0,
                             coredump_pid="4242", coredump_comm="python3",
                             coredump_exe="/usr/bin/python3.12", coredump_signal="SIGSEGV",
                             coredump_unit="app.service")
        [signal] = ks.classify_coredump(record)
        self.assertEqual((signal.event_type, signal.subject, signal.service),
                         (ks.PROCESS_CRASHED, "python3@/usr/bin/python3.12", "app.service"))
        self.assertEqual(signal.attributes["signal"], "SIGSEGV")

    def test_record_without_coredump_fields_is_ignored(self):
        self.assertEqual(ks.classify_coredump(user_record("Started foo.service.", 1.0)), [])

    def test_service_killed_by_a_crash_signal(self):
        cases = {
            "ollama.service: Main process exited, code=dumped, status=11/SEGV": ("SIGSEGV", True),
            "ollama.service: Main process exited, code=killed, status=6/ABRT": ("SIGABRT", False),
        }
        for message, (signal_name, dumped) in cases.items():
            with self.subTest(message):
                [signal] = ks.classify_unit_exit(user_record(message, 1.0, unit="ollama.service"))
                self.assertEqual((signal.event_type, signal.subject, signal.service),
                                 (ks.PROCESS_CRASHED, "ollama.service", "ollama.service"))
                self.assertEqual((signal.attributes["signal"], signal.attributes["core_dumped"]),
                                 (signal_name, dumped))

    def test_service_stopped_or_failed_without_a_crash_is_ignored(self):
        for message in ("ollama.service: Main process exited, code=killed, status=15/TERM",
                        "ollama.service: Main process exited, code=exited, status=1/FAILURE",
                        "ollama.service: Failed with result 'core-dump'."):
            with self.subTest(message):
                self.assertEqual(ks.classify_unit_exit(user_record(message, 1.0)), [])
        other = replace(user_record("x: Main process exited, code=dumped, status=11/SEGV", 1.0),
                        logger="myapp")
        self.assertEqual(ks.classify_unit_exit(other), [])


class HardwareErrorTest(unittest.TestCase):

    def test_corrected_machine_check(self):
        signal = _block(MCE_CORRECTED)
        self.assertEqual((signal.event_type, signal.severity, signal.subject),
                         (ks.HW_ERROR_CORRECTED, "warning", "mce:cpu0:bank5"))
        self.assertEqual(signal.attributes["mci_status"], "8c00004000010090")

    def test_fatal_machine_check_replayed_at_boot_is_uncorrected(self):
        signal = _block(MCE_REPLAYED_FATAL)
        self.assertEqual((signal.event_type, signal.severity, signal.subject),
                         (ks.HW_ERROR_UNCORRECTED, "critical", "mce:cpu2:bank1"))

    def test_worst_bank_of_a_replay_decides(self):
        replay = MCE_CORRECTED + "\n" + "\n".join(MCE_REPLAYED_FATAL.split("\n")[1:])
        signal = _block(replay)
        self.assertEqual((signal.event_type, signal.subject), (ks.HW_ERROR_UNCORRECTED, "mce:cpu2:bank1"))

    def test_machine_check_without_status_falls_back_to_the_text(self):
        corrected = _block("mce: [Hardware Error]: Machine check events logged")
        self.assertEqual((corrected.event_type, corrected.subject), (ks.HW_ERROR_CORRECTED, "mce:mce"))
        fatal = _block("mce: [Hardware Error]: CPU 1: Machine Check Exception: 5 Bank 3")
        self.assertEqual(fatal.event_type, ks.HW_ERROR_UNCORRECTED)

    def test_corrected_pcie_error(self):
        signal = _block(AER_CORRECTED)
        self.assertEqual((signal.event_type, signal.subject), (ks.HW_ERROR_CORRECTED, "aer:0000:01:00.0"))
        newer = _block("pcieport 0000:00:1c.0: AER: Correctable error message received from 0000:01:00.0")
        self.assertEqual(newer.event_type, ks.HW_ERROR_CORRECTED)

    def test_uncorrected_pcie_error(self):
        signal = _block(AER_UNCORRECTED)
        self.assertEqual((signal.event_type, signal.severity), (ks.HW_ERROR_UNCORRECTED, "critical"))
        self.assertEqual(signal.attributes["device"], "0000:02:00.0")

    def test_edac_memory_errors(self):
        [ce] = _line("EDAC MC0: 1 CE memory read error on CPU_SrcID#0_Ha#0_Chan#0_DIMM#0 "
                     "(channel:0 slot:0 page:0x12345 offset:0x0 grain:32 syndrome:0x0)")
        self.assertEqual((ce.event_type, ce.subject), (ks.HW_ERROR_CORRECTED, "edac:MC0"))
        [ue] = _line("EDAC skx MC2: 1 UE memory scrubbing error on CPU_SrcID#1_MC#0_Chan#1_DIMM#0 (x)")
        self.assertEqual((ue.event_type, ue.subject), (ks.HW_ERROR_UNCORRECTED, "edac:MC2"))


class StorageAndThermalTest(unittest.TestCase):

    def test_block_layer_io_error(self):
        [signal] = _line("I/O error, dev nvme0n1, sector 123456 op 0x0:(READ) flags 0x80700 phys_seg 1 prio class 2")
        self.assertEqual((signal.event_type, signal.subject), (ks.STORAGE_IO_ERROR, "nvme0n1"))
        self.assertEqual(signal.attributes["io_error_kind"], "block_io")
        [medium] = _line("blk_update_request: critical medium error, dev sdb, sector 2048 op 0x0:(READ)")
        self.assertEqual(medium.summary, "Storage critical medium error on sdb")

    def test_filesystem_going_read_only_is_critical(self):
        [signal] = _line("EXT4-fs (nvme0n1p2): Remounting filesystem read-only")
        self.assertEqual((signal.severity, signal.subject), ("critical", "nvme0n1p2"))
        self.assertEqual((signal.attributes["io_error_kind"], signal.attributes["read_only"]),
                         ("filesystem_readonly", True))

    def test_other_storage_errors(self):
        cases = {
            "Buffer I/O error on dev sda1, logical block 0, async page read": ("buffer_io", "sda1"),
            "EXT4-fs error (device nvme0n1p2): ext4_find_entry:1455: inode #2: comm ls: reading directory":
                ("filesystem", "nvme0n1p2"),
            "XFS (dm-0): Corruption of in-memory data detected.  Shutting down filesystem": ("filesystem", "dm-0"),
            "nvme nvme0: I/O 123 QID 4 timeout, aborting": ("nvme", "nvme0"),
            "ata1.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x0": ("ata", "ata1.00"),
        }
        for message, (kind, device) in cases.items():
            with self.subTest(message):
                [signal] = _line(message)
                self.assertEqual((signal.attributes["io_error_kind"], signal.subject), (kind, device))

    def test_cpu_thermal_throttling(self):
        for message in ("CPU3: Core temperature above threshold, cpu clock throttled (total events = 1234)",
                        "CPU3: Core temperature is above threshold, cpu clock is throttled (total events = 1234)"):
            with self.subTest(message):
                [signal] = _line(message)
                self.assertEqual((signal.event_type, signal.subject), (ks.CPU_THERMAL_THROTTLED, "cpu-core"))
                self.assertEqual(signal.attributes["cpu"], "3")


class NoiseTest(unittest.TestCase):
    """Ordinary kernel chatter that mentions errors is not a fault signature."""

    def test_driver_chatter_is_not_classified(self):
        for message in ("usb 1-1: device descriptor read/64, error -71",
                        "xe 0000:03:00.0: [drm] *ERROR* GT0: GuC load failed",
                        "iwlwifi 0000:00:14.3: Microcode SW error detected. Restarting 0x0.",
                        "EXT4-fs (nvme0n1p2): mounted filesystem with ordered data mode."):
            with self.subTest(message):
                self.assertEqual(_line(message), [])
                self.assertEqual(ks.classify_block(kernel_records(message)), [])

    def test_oom_report_is_left_to_the_oom_rule(self):
        self.assertEqual(ks.classify_block(kernel_records(OOM_KILL)), [])

    def test_userspace_lines_are_ignored(self):
        record = user_record("I/O error, dev sda, sector 1", 1.0)
        self.assertEqual(ks.classify_line(record), [])
        self.assertEqual(ks.classify_block([record]), [])


if __name__ == "__main__":
    unittest.main()
