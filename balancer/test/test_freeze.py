# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the manual Freeze action (cgroup.freeze).

* ``controller/freeze.py``: write + confirm via cgroup.events, timeout, and the
  crash-safe record that lets the next start thaw only what smartune froze.
* ``DynamicBalancer.set_freeze`` / ``set_restore_resource``: freeze needs a manual
  limit, a half-frozen multi-cgroup app is rolled back, and Restore lifts the
  limits *before* it thaws.

Run:  python3 balancer/test/test_freeze.py
"""

import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if path not in sys.path:
        sys.path.insert(0, path)

from controller import freeze  # noqa: E402
from balancer.balancer import DynamicBalancer, LimitedApp, LimitRegistry  # noqa: E402
import balancer.balancer as balancer_mod  # noqa: E402


def _make_cgroup(root, name, frozen=False):
    path = os.path.join(root, name)
    os.makedirs(path)
    with open(os.path.join(path, "cgroup.freeze"), "w") as f:
        f.write("1\n" if frozen else "0\n")
    _write_events(path, frozen)
    return path


def _write_events(path, frozen):
    with open(os.path.join(path, "cgroup.events"), "w") as f:
        f.write(f"populated 1\nfrozen {1 if frozen else 0}\n")


def _kernel_like_write(content, target_file, allowed_roots=None):
    """Stand-in for write_cgroup_file: the kernel also flips cgroup.events."""
    path = os.path.dirname(target_file)
    with open(target_file, "w") as f:
        f.write(content + "\n")
    _write_events(path, content.strip() == "1")


class SetFrozenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cg = _make_cgroup(self.tmp.name, "app.scope")

    def test_freeze_and_thaw_confirmed_by_events(self):
        with mock.patch.object(freeze, "write_cgroup_file", _kernel_like_write):
            self.assertEqual(freeze.set_frozen(self.cg, True, timeout=1), (True, "ok"))
            self.assertTrue(freeze.is_frozen(self.cg))
            self.assertEqual(freeze.set_frozen(self.cg, False, timeout=1), (True, "ok"))
            self.assertFalse(freeze.is_frozen(self.cg))

    def test_times_out_when_the_cgroup_never_freezes(self):
        # A task stuck in an uninterruptible call keeps frozen at 0.
        with mock.patch.object(freeze, "write_cgroup_file", lambda *a, **k: None):
            ok, msg = freeze.set_frozen(self.cg, True, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("did not reach frozen=1", msg)

    def test_write_failure_is_reported_not_raised(self):
        with mock.patch.object(freeze, "write_cgroup_file", side_effect=PermissionError("no")):
            ok, msg = freeze.set_frozen(self.cg, True, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("cannot write", msg)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = os.path.join(self.tmp.name, "state", "frozen.json")
        patcher = mock.patch.object(freeze, "STATE_FILE", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = os.path.join(self.tmp.name, "cg")
        os.makedirs(self.root)

    def test_startup_thaws_only_recorded_cgroups(self):
        mine = _make_cgroup(self.root, "mine.scope", frozen=True)
        # e.g. `docker pause`: frozen, but not ours -- must stay frozen.
        other = _make_cgroup(self.root, "docker.scope", frozen=True)
        freeze.record("mine.scope", True)
        freeze.record("gone.scope", True)   # cgroup vanished: record is dropped

        with mock.patch.object(freeze, "write_cgroup_file", _kernel_like_write):
            thawed = freeze.thaw_recorded(lambda cg: os.path.join(self.root, cg))

        self.assertEqual(thawed, ["mine.scope"])
        self.assertFalse(freeze.is_frozen(mine))
        self.assertTrue(freeze.is_frozen(other))
        self.assertEqual(freeze._read_state(), [])

    def test_failed_thaw_keeps_the_record_for_the_next_start(self):
        _make_cgroup(self.root, "stuck.scope", frozen=True)
        freeze.record("stuck.scope", True)
        with mock.patch.object(freeze, "write_cgroup_file", lambda *a, **k: None), \
                mock.patch.object(freeze, "FREEZE_TIMEOUT_S", 0.1), \
                mock.patch.object(freeze.time, "sleep", lambda s: None):
            freeze.thaw_recorded(lambda cg: os.path.join(self.root, cg))
        self.assertEqual(freeze._read_state(), ["stuck.scope"])


class _ControlManager:
    """Records every adjust/freeze call in one list so ordering can be asserted."""

    def __init__(self, calls, fail_freeze_on=None):
        self.calls = calls
        self.fail_freeze_on = fail_freeze_on

    def adjust_resources(self, app_id, policy, **kwargs):
        self.calls.append(("restore_limit", app_id))
        return True

    def freeze_cgroup(self, cgroup, frozen):
        if frozen and cgroup == self.fail_freeze_on:
            return False, "did not reach frozen=1"
        self.calls.append(("freeze" if frozen else "thaw", cgroup))
        return True, "ok"

    def set_limited_app_dominant(self, *a, **k):
        pass


class _IoCtl:
    def __init__(self, calls):
        self.calls = calls

    def restore_disk_io_throttle(self, cgroup, disk_filter=None):
        self.calls.append(("restore_io", cgroup))
        return True


def _entry(source="manual", cgroups=("a.scope",)):
    return LimitedApp(
        public_app_id="app-1", app_name="app", source=source,
        limit_rates={}, limit_parts={"cpu_mem_limited": True, "io_limited": True},
        cgroups=list(cgroups), pids={1},
    )


def _balancer(calls, entry=None, fail_freeze_on=None):
    b = DynamicBalancer.__new__(DynamicBalancer)
    b.config = types.SimpleNamespace(regular_update_sys_pressure_time=0)
    b.all_limits = LimitRegistry()
    b.control_manager = _ControlManager(calls, fail_freeze_on)
    b.io_ctl = _IoCtl(calls)
    if entry is not None:
        b.all_limits.apps[entry.cgroups[0]] = entry
    return b


class SetFreezeTests(unittest.TestCase):
    def test_requires_a_manual_limit(self):
        calls = []
        b = _balancer(calls, _entry(source="auto"))
        ok, _ = b.set_freeze("app-1", True)
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_freeze_marks_entry_and_is_idempotent(self):
        calls = []
        entry = _entry()
        b = _balancer(calls, entry)
        self.assertTrue(b.set_freeze("app-1", True)[0])
        self.assertTrue(entry.frozen)
        self.assertGreater(entry.frozen_at, 0)
        self.assertTrue(b.set_freeze("app-1", True)[0])
        self.assertEqual(calls, [("freeze", "a.scope")])

    def test_unfreeze_keeps_the_limits(self):
        calls = []
        entry = _entry()
        b = _balancer(calls, entry)
        b.set_freeze("app-1", True)
        self.assertTrue(b.set_freeze("app-1", False)[0])
        self.assertFalse(entry.frozen)
        self.assertNotIn(("restore_limit", "a.scope"), calls)
        self.assertIn("a.scope", b.all_limits.apps)

    def test_half_frozen_app_is_rolled_back(self):
        calls = []
        entry = _entry(cgroups=("a.scope", "b.scope"))
        b = _balancer(calls, entry, fail_freeze_on="b.scope")
        ok, _ = b.set_freeze("app-1", True)
        self.assertFalse(ok)
        self.assertFalse(entry.frozen)
        self.assertEqual(calls, [("freeze", "a.scope"), ("thaw", "a.scope")])


class RestoreOrderTests(unittest.TestCase):
    def _restore(self, entry, calls):
        b = _balancer(calls, entry)
        with mock.patch.object(balancer_mod.app_utils, "update_app_status"), \
                mock.patch.object(balancer_mod.app_utils, "callback_manager"), \
                mock.patch.object(balancer_mod, "_emit_control_events"):
            return b.set_restore_resource("app-1")

    def test_restore_lifts_limits_before_thawing(self):
        calls = []
        entry = _entry()
        entry.frozen = True
        self.assertTrue(self._restore(entry, calls))
        self.assertEqual(calls, [
            ("restore_limit", "a.scope"), ("restore_io", "a.scope"), ("thaw", "a.scope"),
        ])

    def test_restore_of_an_unfrozen_app_never_touches_the_freezer(self):
        calls = []
        self.assertTrue(self._restore(_entry(), calls))
        self.assertFalse([c for c in calls if c[0] in ("freeze", "thaw")])

    def test_failed_thaw_fails_the_restore(self):
        calls = []
        entry = _entry()
        entry.frozen = True
        b = _balancer(calls, entry)
        b.control_manager.freeze_cgroup = lambda cg, frozen: (False, "stuck")
        with mock.patch.object(balancer_mod.app_utils, "update_app_status"), \
                mock.patch.object(balancer_mod.app_utils, "callback_manager"), \
                mock.patch.object(balancer_mod, "_emit_control_events"):
            self.assertFalse(b.set_restore_resource("app-1"))


class MultiCgroupTests(unittest.TestCase):
    """An app added with several PIDs owns several cgroups; freeze mirrors the limit."""

    CGS = ("a.scope", "b.scope", "c.scope")

    def test_freeze_and_unfreeze_cover_every_cgroup(self):
        calls = []
        b = _balancer(calls, _entry(cgroups=self.CGS))
        self.assertTrue(b.set_freeze("app-1", True)[0])
        self.assertTrue(b.set_freeze("app-1", False)[0])
        self.assertEqual(calls, [("freeze", c) for c in self.CGS] + [("thaw", c) for c in self.CGS])

    def test_restore_thaws_every_cgroup_the_limit_covered(self):
        calls = []
        entry = _entry(cgroups=self.CGS)
        entry.frozen = True
        self.assertTrue(RestoreOrderTests()._restore(entry, calls))
        self.assertEqual([c for c in calls if c[0] == "thaw"], [("thaw", c) for c in self.CGS])

    def test_closed_frozen_app_is_thawed_and_its_records_dropped(self):
        calls = []
        entry = _entry(cgroups=self.CGS)
        entry.frozen = True
        b = _balancer(calls)
        with mock.patch.object(b, "_cgroup_exists", return_value=False), \
                mock.patch.object(balancer_mod.app_utils, "update_app_status"), \
                mock.patch.object(balancer_mod.app_utils, "callback_manager"), \
                mock.patch.object(balancer_mod, "_emit_control_events"):
            b._restore_entry(entry, notify=True)
        # Gone cgroups need no limit restore, but freeze_cgroup is still asked to
        # thaw so it can drop the crash-safety record.
        self.assertFalse([c for c in calls if c[0].startswith("restore")])
        self.assertEqual([c for c in calls if c[0] == "thaw"], [("thaw", c) for c in self.CGS])


class FrozenTooLongTests(unittest.TestCase):
    def _run(self, entry, after=600):
        b = _balancer([], entry)
        b.config.freeze_notify_after = after
        with mock.patch.object(balancer_mod.app_utils, "callback_manager") as cb:
            b._notify_long_frozen()
            b._notify_long_frozen()
        return b, cb.send_callback_notification.call_args_list

    def test_reminds_once_after_the_threshold(self):
        entry = _entry()
        entry.frozen, entry.frozen_at = True, time.time() - 601
        _, sent = self._run(entry)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].args[0]["status"], "app_frozen_long")
        self.assertEqual(sent[0].args[0]["purpose"], "notify")

    def test_no_reminder_before_the_threshold_or_when_unfrozen_or_disabled(self):
        young = _entry()
        young.frozen, young.frozen_at = True, time.time() - 10
        self.assertEqual(self._run(young)[1], [])
        unfrozen = _entry()
        self.assertEqual(self._run(unfrozen)[1], [])
        old = _entry()
        old.frozen, old.frozen_at = True, time.time() - 9999
        self.assertEqual(self._run(old, after=0)[1], [])

    def test_a_new_freeze_can_remind_again(self):
        entry = _entry()
        b = _balancer([], entry)
        b.set_freeze("app-1", True)
        entry.freeze_notified = True
        b.set_freeze("app-1", False)
        b.set_freeze("app-1", True)
        self.assertFalse(entry.freeze_notified)


if __name__ == "__main__":
    unittest.main()
