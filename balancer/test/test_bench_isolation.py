# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the benchmark run's own cgroup scope (benchmark/service/isolation.py).

Only the decisions are tested here -- where a scope goes, and when it is not
used. That a scope really holds the process is checked on a real systemd.

Run:  python3 balancer/test/test_bench_isolation.py
"""

import logging
import os
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_logger_module = types.ModuleType("utils.logger")
_test_logger = logging.getLogger("test_bench_isolation")
_logger_module.logger = _test_logger
_logger_module.get_logger = lambda name=None: _test_logger
sys.modules["utils.logger"] = _logger_module

from benchmark.service import isolation  # noqa: E402
from utils import self_guard  # noqa: E402

VTE = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Terminal.slice/vte-spawn-x.scope"
TMUX = "/user.slice/user-1000.slice/user@1000.service/app.slice/tmux-spawn-x.scope"
SSH = "/user.slice/user-1000.slice/session-7.scope"
SERVICE = "/system.slice/smartune.service"


class PlacementTest(unittest.TestCase):
    def test_terminal_scope_gets_a_sibling_in_the_same_slice_from_the_user_manager(self):
        placement, _ = isolation.placement_for(VTE, 1000, 1000)
        self.assertEqual(placement, isolation.Placement("user", "app-org.gnome.Terminal.slice", 1000))

    def test_follows_wherever_the_service_runs(self):
        placement, _ = isolation.placement_for(TMUX, 1000, 0)
        self.assertEqual(placement, isolation.Placement("user", "app.slice", 1000))

    def test_user_manager_is_used_even_when_root_started_the_service(self):
        placement, _ = isolation.placement_for(VTE, 1000, 0)
        self.assertEqual(placement.manager, "user")

    def test_other_users_manager_is_refused(self):
        placement, why = isolation.placement_for(VTE, 1001, 0)
        self.assertIsNone(placement)
        self.assertIn("another user's manager", why)

    def test_session_scope_gets_a_scope_in_the_user_slice_from_the_system_manager(self):
        placement, _ = isolation.placement_for(SSH, 1000, 0)
        self.assertEqual(placement, isolation.Placement("system", "user-1000.slice", None))

    def test_system_service_gets_a_scope_in_system_slice(self):
        placement, _ = isolation.placement_for(SERVICE, 1000, 0)
        self.assertEqual(placement, isolation.Placement("system", "system.slice", None))

    def test_system_manager_needs_root(self):
        placement, why = isolation.placement_for(SSH, 1000, 1000)
        self.assertIsNone(placement)
        self.assertIn("not root", why)

    def test_unknown_layouts_are_left_alone(self):
        for path in ("/", "", "/only-one.scope", "/odd.slice/thing.scope"):
            placement, _ = isolation.placement_for(path, 1000, 0)
            self.assertIsNone(placement, path)

    def test_service_directly_under_the_user_manager_uses_its_default_slice(self):
        placement, _ = isolation.placement_for("/user.slice/user-1000.slice/user@1000.service/x.scope", 1000, 1000)
        self.assertEqual(placement, isolation.Placement("user", None, 1000))


class WrapTest(unittest.TestCase):
    def setUp(self):
        isolation._probe_cache.clear()
        self.addCleanup(isolation._probe_cache.clear)
        self.addCleanup(self_guard.set_own_units_for_test, None)
        self_guard.set_own_units_for_test(VTE.strip("/").split("/"))
        patches = [
            mock.patch.object(isolation, "_unavailable", return_value=None),
            mock.patch.object(isolation.privilege, "target", return_value=None),
            mock.patch.object(isolation.privilege, "spawn_kwargs", return_value={}),
            mock.patch.object(isolation.os, "geteuid", return_value=1000),
            mock.patch.object(isolation, "_user_env", return_value={"XDG_RUNTIME_DIR": "/run/user/1000"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_wrap(self, probe=(True, "")):
        with mock.patch.object(isolation, "_probe", return_value=probe):
            return isolation.wrap(["python", "bench.py", "--x"], {"A": "1"}, unit="smartune-bench-abc")

    def test_wraps_in_a_scope_and_keeps_the_command_intact(self):
        argv, env, kwargs, scope = self.run_wrap()
        self.assertEqual(argv[:2], ["systemd-run", "--user"])
        self.assertIn("--scope", argv)
        self.assertIn("--unit=smartune-bench-abc", argv)
        self.assertIn("--slice=app-org.gnome.Terminal.slice", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["python", "bench.py", "--x"])
        self.assertEqual(env, {"A": "1", "XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(scope.unit, "smartune-bench-abc")

    def test_no_limits_are_set_on_the_scope(self):
        argv, *_ = self.run_wrap()
        self.assertFalse([a for a in argv if a.startswith("--property")])

    def test_falls_back_unchanged_when_the_probe_fails(self):
        argv, env, _, scope = self.run_wrap(probe=(False, "no bus"))
        self.assertEqual(argv, ["python", "bench.py", "--x"])
        self.assertEqual(env, {"A": "1"})
        self.assertIsNone(scope)

    def test_falls_back_without_a_user_bus(self):
        with mock.patch.object(isolation, "_user_env", return_value=None):
            argv, _, _, scope = self.run_wrap()
        self.assertEqual(argv, ["python", "bench.py", "--x"])
        self.assertIsNone(scope)

    def test_falls_back_without_systemd(self):
        with mock.patch.object(isolation, "_unavailable", return_value="systemd is not the init system"):
            argv, _, _, scope = self.run_wrap()
        self.assertEqual(argv, ["python", "bench.py", "--x"])
        self.assertIsNone(scope)

    def test_falls_back_for_an_unplaceable_layout(self):
        self_guard.set_own_units_for_test(["odd.slice", "thing.scope"])
        argv, _, _, scope = self.run_wrap()
        self.assertEqual(argv, ["python", "bench.py", "--x"])
        self.assertIsNone(scope)

    def test_root_manager_drops_privileges_inside_the_scope_with_its_groups(self):
        self_guard.set_own_units_for_test(SSH.strip("/").split("/"))
        who = isolation.privilege.Target(uid=1000, gid=1000, name="nas", groups=[4, 992, 1000])
        with mock.patch.object(isolation.os, "geteuid", return_value=0), \
                mock.patch.object(isolation.privilege, "target", return_value=who):
            argv, _, kwargs, scope = self.run_wrap()
        self.assertEqual(argv[0], "systemd-run")
        self.assertNotIn("--user", argv)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[:5], ["setpriv", "--reuid=1000", "--regid=1000", "--groups=4,992,1000", "--"])
        self.assertEqual(tail[5:], ["python", "bench.py", "--x"])
        self.assertNotIn("user", kwargs)          # Popen stays root to create the scope
        self.assertEqual(scope.placement.manager, "system")

    def test_root_manager_with_no_groups_clears_them(self):
        self_guard.set_own_units_for_test(SSH.strip("/").split("/"))
        who = isolation.privilege.Target(uid=1000, gid=1000, name="nas", groups=[])
        with mock.patch.object(isolation.os, "geteuid", return_value=0), \
                mock.patch.object(isolation.privilege, "target", return_value=who):
            argv, *_ = self.run_wrap()
        self.assertIn("--clear-groups", argv)


class KillTest(unittest.TestCase):
    def test_no_scope_is_a_noop(self):
        self.assertFalse(isolation.kill(None))

    def test_user_scope_is_signalled_through_the_user_manager(self):
        scope = isolation.Scope("smartune-bench-abc", isolation.Placement("user", "app.slice", 1000),
                                {"XDG_RUNTIME_DIR": "/run/user/1000"})
        with mock.patch.object(isolation.privilege, "spawn_kwargs", return_value={}), \
                mock.patch.object(isolation.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(isolation.kill(scope, "SIGKILL"))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ["systemctl", "--user", "kill", "--signal=SIGKILL", "smartune-bench-abc.scope"])
        self.assertEqual(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"], "/run/user/1000")


if __name__ == "__main__":
    unittest.main()
