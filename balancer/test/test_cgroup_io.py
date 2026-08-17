# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cgroup v2 io.stat accounting (monitor/cgroup.py).

Uses a fake cgroup tree on disk, so the parsing and the baseline rules are exercised
without depending on the host's real cgroups or on any I/O actually happening.

For an end-to-end check that io.stat is the RIGHT source (i.e. that per-PID syscall
counts under-report device IOPS), run ``balancer/test/probe_cgroup_io.py`` instead --
that one needs a real device and real writes.

Run:  python3 balancer/test/test_cgroup_io.py
  or: python3 -m unittest balancer.test.test_cgroup_io
"""

import os
import shutil
import sys
import tempfile
import unittest

# Allow running the file directly from anywhere.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from monitor.cgroup import (
    as_io_rates,
    io_stat_deltas,
    read_cgroup_io_stat,
    snapshot_cgroup_io,
)


def _dev(rbytes=0, wbytes=0, rios=0, wios=0):
    return {"rbytes": rbytes, "wbytes": wbytes, "rios": rios, "wios": wios}


class ReadIoStatTests(unittest.TestCase):
    """Parsing of the io.stat file format."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="smartune-cgroup-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self, cgroup, content):
        path = os.path.join(self.root, cgroup.lstrip('/'))
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "io.stat"), "w") as f:
            f.write(content)

    def test_parses_all_four_fields_per_device(self):
        self._write("/app.scope",
                    "259:0 rbytes=1024 wbytes=2048 rios=4 wios=8 dbytes=99 dios=1\n"
                    "8:0 rbytes=512 wbytes=0 rios=2 wios=0 dbytes=0 dios=0\n")
        out = read_cgroup_io_stat("/app.scope", self.root)
        self.assertEqual(out["259:0"], _dev(1024, 2048, 4, 8))
        self.assertEqual(out["8:0"], _dev(512, 0, 2, 0))

    def test_discard_fields_are_ignored(self):
        # A TRIM must not read as a write burst: dbytes/dios are deliberately dropped.
        self._write("/app.scope", "259:0 rbytes=0 wbytes=0 rios=0 wios=0 dbytes=999999 dios=42\n")
        out = read_cgroup_io_stat("/app.scope", self.root)
        self.assertEqual(out["259:0"], _dev())
        self.assertNotIn("dbytes", out["259:0"])

    def test_bare_device_line_is_skipped(self):
        # The kernel prints "8:16" with no fields for devices the cgroup never touched.
        self._write("/app.scope", "8:16 \n259:0 rbytes=10 wbytes=20 rios=1 wios=2\n")
        out = read_cgroup_io_stat("/app.scope", self.root)
        self.assertEqual(list(out), ["259:0"])

    def test_missing_file_is_none_not_empty(self):
        # None ("no such cgroup") and {} ("cgroup exists, idle") must stay distinct --
        # io_stat_deltas relies on the difference. See BaselineTests below.
        self.assertIsNone(read_cgroup_io_stat("/does-not-exist.scope", self.root))
        self._write("/idle.scope", "")
        self.assertEqual(read_cgroup_io_stat("/idle.scope", self.root), {})

    def test_unreadable_cgroups_are_omitted_from_snapshot(self):
        self._write("/live.scope", "259:0 rbytes=1 wbytes=1 rios=1 wios=1\n")
        _, snap = snapshot_cgroup_io(["/live.scope", "/gone.scope"], self.root)
        self.assertEqual(list(snap), ["/live.scope"])


class DeltaTests(unittest.TestCase):
    """Differencing two snapshots."""

    def test_totals_sum_across_devices(self):
        snap0 = {"/a": {"259:0": _dev(0, 0, 0, 0), "8:0": _dev(0, 0, 0, 0)}}
        snap1 = {"/a": {"259:0": _dev(100, 200, 1, 2), "8:0": _dev(300, 400, 3, 4)}}
        elapsed, out = io_stat_deltas(0.0, snap0, 2.0, snap1)
        self.assertEqual(elapsed, 2.0)
        self.assertEqual(out["/a"]["rbytes"], 400)
        self.assertEqual(out["/a"]["wbytes"], 600)
        self.assertEqual(out["/a"]["rios"], 4)
        self.assertEqual(out["/a"]["wios"], 6)
        self.assertEqual(set(out["/a"]["per_device"]), {"259:0", "8:0"})

    def test_idle_cgroups_are_absent_from_result(self):
        snap = {"/a": {"259:0": _dev(5, 5, 1, 1)}}
        _, out = io_stat_deltas(0.0, snap, 1.0, snap)  # identical -> no delta
        self.assertEqual(out, {})

    def test_non_monotonic_counters_clamp_to_zero(self):
        # Happens when a cgroup is recreated under the same path between snapshots.
        snap0 = {"/a": {"259:0": _dev(1000, 1000, 10, 10)}}
        snap1 = {"/a": {"259:0": _dev(5, 5, 1, 1)}}
        _, out = io_stat_deltas(0.0, snap0, 1.0, snap1)
        self.assertEqual(out, {})

    def test_zero_or_negative_window_yields_nothing(self):
        snap = {"/a": {"259:0": _dev(1, 1, 1, 1)}}
        self.assertEqual(io_stat_deltas(5.0, {}, 5.0, snap), (0.0, {}))
        self.assertEqual(io_stat_deltas(5.0, {}, 4.0, snap), (0.0, {}))


class BaselineTests(unittest.TestCase):
    """The rule that decides whether a window can be measured at all.

    Getting this wrong made a freshly-launched app invisible for its first sampling
    window -- precisely the app the disk-IO throttle path exists to catch.
    """

    def test_cgroup_absent_at_t0_is_skipped(self):
        # No baseline at all: its counters cover its whole lifetime, not this window.
        _, out = io_stat_deltas(0.0, {}, 1.0, {"/new": {"259:0": _dev(10 ** 9, 10 ** 9, 1, 1)}})
        self.assertEqual(out, {})

    def test_cgroup_present_but_idle_at_t0_gets_a_zero_baseline(self):
        # The cgroup existed and had nothing charged to it, so everything in snap1 was
        # accumulated inside the window and must be reported.
        _, out = io_stat_deltas(0.0, {"/new": {}}, 1.0, {"/new": {"259:0": _dev(0, 4096, 0, 1)}})
        self.assertEqual(out["/new"]["wbytes"], 4096)
        self.assertEqual(out["/new"]["wios"], 1)

    def test_new_device_on_existing_cgroup_gets_a_zero_baseline(self):
        # Same reasoning per device: the app just started touching a second disk.
        snap0 = {"/a": {"259:0": _dev(0, 100, 0, 1)}}
        snap1 = {"/a": {"259:0": _dev(0, 100, 0, 1), "8:0": _dev(0, 2048, 0, 2)}}
        _, out = io_stat_deltas(0.0, snap0, 1.0, snap1)
        self.assertEqual(out["/a"]["per_device"]["8:0"], _dev(0, 2048, 0, 2))


class RateTests(unittest.TestCase):
    """Counts -> MB/s and device IOPS."""

    def test_bytes_become_mb_per_second_and_ios_become_iops(self):
        r = as_io_rates(_dev(rbytes=2 * 1024 ** 2, wbytes=1024 ** 2, rios=50, wios=100), 2.0)
        self.assertAlmostEqual(r["read_mb_s"], 1.0)
        self.assertAlmostEqual(r["write_mb_s"], 0.5)
        self.assertAlmostEqual(r["read_iops"], 25.0)
        self.assertAlmostEqual(r["write_iops"], 50.0)

    def test_zero_window_is_zero_not_a_division_error(self):
        self.assertEqual(as_io_rates(_dev(wbytes=1024), 0.0)["write_mb_s"], 0.0)

    def test_iops_reflects_request_count_not_byte_count(self):
        # The whole point of reading io.stat: 128 KB requests, so 2400 requests for
        # 300 MB -- a syscall-count source would have said ~300 for the same bytes.
        r = as_io_rates(_dev(wbytes=300 * 1024 ** 2, wios=2400), 1.0)
        self.assertAlmostEqual(r["write_mb_s"], 300.0)
        self.assertAlmostEqual(r["write_iops"], 2400.0)
        self.assertAlmostEqual(r["write_mb_s"] * 1024 / r["write_iops"], 128.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
