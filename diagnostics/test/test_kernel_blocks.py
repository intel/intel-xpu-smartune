# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Grouping the lines of one kernel report into a block."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kernel_samples import (  # noqa: E402
    DRM_WARN, HUNG_TASK_GPU, KERNEL_BUG_AT, MCE_CORRECTED, NULL_DEREF_OOPS, OOM_KILL, SEGFAULT,
    WARN_XE, kernel_records, user_record,
)

from diagnostics.sources import kernel_blocks  # noqa: E402


def _texts(groups):
    return [[rec.message for rec in group] for group in groups]


class GroupingTest(unittest.TestCase):

    def test_report_lines_form_one_block(self):
        for name, text in (("hung task", HUNG_TASK_GPU), ("warning", WARN_XE),
                           ("oops", NULL_DEREF_OOPS), ("oom", OOM_KILL), ("mce", MCE_CORRECTED),
                           ("segfault", SEGFAULT)):
            with self.subTest(name):
                groups = kernel_blocks.group_records(kernel_records(text))
                self.assertEqual(_texts(groups), [text.split("\n")])

    def test_a_gap_ends_the_block(self):
        records = kernel_records(HUNG_TASK_GPU)
        later = kernel_records(" __schedule+0x27c/0x6b0", start=records[-1].fields["monotonic_s"] + 0.6)
        groups = kernel_blocks.group_records(records + later)
        self.assertEqual([len(g) for g in groups], [len(records), 1])

    def test_a_new_boot_ends_the_block(self):
        records = kernel_records(HUNG_TASK_GPU)
        other_boot = kernel_records(" __schedule+0x27c/0x6b0", boot="boot-2",
                                    start=records[-1].fields["monotonic_s"] + 0.001)
        groups = kernel_blocks.group_records(records + other_boot)
        self.assertEqual([len(g) for g in groups], [len(records), 1])

    def test_next_header_starts_a_new_block(self):
        first = kernel_records(HUNG_TASK_GPU)
        second = kernel_records(HUNG_TASK_GPU, start=first[-1].fields["monotonic_s"] + 0.001)
        groups = kernel_blocks.group_records(first + second)
        self.assertEqual([len(g) for g in groups], [len(first), len(second)])

    def test_end_marker_closes_the_block(self):
        warn = kernel_records(WARN_XE)
        after = kernel_records("Code: 48 8b 07 c3", start=warn[-1].fields["monotonic_s"] + 0.001)
        groups = kernel_blocks.group_records(warn + after)
        self.assertEqual([len(g) for g in groups], [len(warn), 1])

    def test_oom_report_ends_at_the_kill_line(self):
        oom = kernel_records(OOM_KILL)
        after = kernel_records(" some trailing line", start=oom[-1].fields["monotonic_s"] + 0.001)
        groups = kernel_blocks.group_records(oom + after)
        self.assertEqual([len(g) for g in groups], [len(oom), 1])

    def test_oops_and_panic_continue_the_bug_report(self):
        text = NULL_DEREF_OOPS.replace("---[ end trace 0000000000000000 ]---",
                                       "Kernel panic - not syncing: Fatal exception")
        groups = kernel_blocks.group_records(kernel_records(text))
        self.assertEqual(len(groups), 1)

    def test_audit_and_userspace_lines_do_not_split_a_block(self):
        records = kernel_records(HUNG_TASK_GPU)
        audit = kernel_records('audit: type=1400 audit(1900000100.004:42): apparmor="DENIED"',
                               start=records[3].fields["monotonic_s"] + 0.0001)
        user = user_record("Started session-12.scope.", records[5].fields["monotonic_s"] + 0.0001)
        groups = kernel_blocks.group_records(records + audit + [user])
        sizes = sorted(len(g) for g in groups)
        self.assertEqual(sizes, [1, 1, len(records)])

    def test_driver_message_keeps_its_stack_dump(self):
        text = ("xe 0000:03:00.0: [drm] *ERROR* GT0: GuC load failed\n"
                "CPU: 1 PID: 777 Comm: kworker/1:2 Not tainted 6.8.0-45-generic #45-Ubuntu\n"
                "Call Trace:\n <TASK>\n dump_stack_lvl+0x48/0x70\n </TASK>")
        groups = kernel_blocks.group_records(kernel_records(text))
        self.assertEqual(len(groups), 1)

    def test_unrelated_driver_lines_stay_apart(self):
        text = "usb 1-1: new high-speed USB device number 2\nusb 1-1: New USB device found"
        groups = kernel_blocks.group_records(kernel_records(text))
        self.assertEqual([len(g) for g in groups], [1, 1])

    def test_long_report_is_cut_at_the_cap(self):
        lines = ["------------[ cut here ]------------",
                 "WARNING: CPU: 0 PID: 1 at kernel/foo.c:1 foo+0x1/0x2"]
        lines += [f" frame_{i}+0x1/0x2" for i in range(kernel_blocks.max_lines("warning"))]
        groups = kernel_blocks.group_records(kernel_records("\n".join(lines), step=0.0001))
        self.assertEqual(len(groups[0]), kernel_blocks.max_lines("warning"))
        merged = kernel_blocks.merge_block(groups[0])
        self.assertTrue(merged.fields["block_truncated"])


class TrackerTest(unittest.TestCase):

    def test_continues_tells_whether_the_open_block_takes_a_line(self):
        records = kernel_records(HUNG_TASK_GPU)
        tracker = kernel_blocks.KernelBlockTracker()
        tracker.push(records[0])
        self.assertTrue(tracker.continues(records[1]))
        self.assertEqual(tracker.open_kind(), "hung_task")
        unrelated = kernel_records("usb 1-1: new device", start=records[0].fields["monotonic_s"] + 0.6)
        self.assertFalse(tracker.continues(unrelated[0]))


class MergeTest(unittest.TestCase):

    def test_merged_record_carries_the_block(self):
        records = kernel_records(HUNG_TASK_GPU, levels={0: "ERROR"})
        merged = kernel_blocks.merge_block(records)
        self.assertEqual(merged.level, "ERROR")
        self.assertEqual(merged.message, HUNG_TASK_GPU)
        self.assertEqual(merged.fields["block"], "hung_task")
        self.assertEqual(merged.fields["line_count"], len(records))
        self.assertEqual(merged.fields["block_title"], records[0].message)
        self.assertFalse(merged.fields["block_truncated"])

    def test_worst_level_wins(self):
        records = kernel_records(WARN_XE, levels={1: "WARNING", 6: "ERROR"})
        self.assertEqual(kernel_blocks.merge_block(records).level, "ERROR")

    def test_cut_here_takes_the_kind_and_title_after_it(self):
        merged = kernel_blocks.merge_block(kernel_records(DRM_WARN))
        self.assertEqual(merged.fields["block"], "warning")
        self.assertTrue(merged.fields["block_title"].startswith("xe 0000:03:00.0: [drm] Assertion"))
        text = "------------[ cut here ]------------\n" + KERNEL_BUG_AT
        self.assertEqual(kernel_blocks.merge_block(kernel_records(text)).fields["block"], "bug")

    def test_single_line_is_returned_unchanged(self):
        record = kernel_records("usb 1-1: new device")[0]
        self.assertIs(kernel_blocks.merge_block([record]), record)

    def test_coalesce_returns_one_record_per_report(self):
        warn = kernel_records(WARN_XE)
        line = kernel_records("usb 1-1: new device", start=warn[-1].fields["monotonic_s"] + 5)
        merged = kernel_blocks.coalesce(line + warn)
        self.assertEqual([r.fields.get("block") for r in merged], ["warning", None])


class ExcerptTest(unittest.TestCase):

    def test_short_text_is_kept(self):
        self.assertEqual(kernel_blocks.block_excerpt("short"), "short")

    def test_long_text_keeps_head_and_tail(self):
        text = "HEAD" + "x" * 10_000 + "TAIL"
        excerpt = kernel_blocks.block_excerpt(text, limit=1000)
        self.assertEqual(len(excerpt), 1000)
        self.assertTrue(excerpt.startswith("HEAD"))
        self.assertTrue(excerpt.endswith("TAIL"))
        self.assertIn("\n...\n", excerpt)


if __name__ == "__main__":
    unittest.main()
