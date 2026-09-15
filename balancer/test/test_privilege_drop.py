# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for the benchmark privilege drop -- benchmark/service/privilege.py.

The service runs as root, and the benchmark pipeline is the one part of it that
executes code fetched off the internet: the vendored scripts, uv, pip, and
whatever a HuggingFace repo ships.  Those children are dropped to the owner of
the benchmark tree.  Three things have to hold, and each breaks silently:

* **Every spawn point drops.**  A new ``subprocess.run`` added later that
  forgets ``**privilege.spawn_kwargs()`` reintroduces root without failing
  anything, so the check here is structural (AST over the package) rather than
  one test per call site.

* **Nothing drops when there is nobody to drop to.**  Not root to begin with, a
  root-owned tree (the .deb), a uid with no passwd entry: each has to leave the
  spawn arguments completely empty.  ``user=0`` passed by a developer's
  non-root service would fail every job.

* **The child can write where it has to.**  Every directory the root parent
  creates ahead of the child is handed over, the rendered run script is handed
  over (it is 0700), and HOME points somewhere the child owns -- otherwise uv,
  pip and git all fail on /root.

Not covered here: the drop actually taking effect.  That needs a root process
and a real fork, so it is the end-to-end check -- setup env, then
``find benchmark/runtime ! -uid <owner>``.

Run:  /usr/bin/python3 balancer/test/test_privilege_drop.py
      (needs psutil + flask, like test_quiet_mode.py)
"""

import ast
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if path not in sys.path:
        sys.path.insert(0, path)

from benchmark.service import env, jobs, privilege, runner  # noqa: E402

# This process's own account, used wherever a test needs a chown that is allowed
# to succeed (chowning to yourself is permitted; to anyone else is not).
_MY_UID = os.getuid()
_MY_GID = os.getgid()
_ME = privilege.Target(uid=_MY_UID, gid=_MY_GID, name="tester",
                       groups=[_MY_GID, 992])


class _FakeSrcRoot:
    """Stand-in for privilege.SRC_ROOT with a chosen owner.

    A root-owned tree is the .deb's normal layout and cannot be created by a
    test, so the owner is faked rather than the directory. A class rather than a
    SimpleNamespace because the decline messages interpolate this, and __str__
    is only honoured on a type.
    """

    def __init__(self, uid: int, gid: int = 0, error: OSError = None):
        self._uid, self._gid, self._error = uid, gid, error

    def stat(self):
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(st_uid=self._uid, st_gid=self._gid)

    def __str__(self):
        return "/fake/benchmark"


def _fake_src_root(uid: int, gid: int = 0):
    return _FakeSrcRoot(uid, gid)


class TargetResolutionTests(unittest.TestCase):
    """Who to drop to, and -- more importantly -- when not to."""

    def setUp(self):
        privilege.target.cache_clear()
        self.addCleanup(privilege.target.cache_clear)

    def test_no_drop_when_not_root(self):
        with mock.patch.object(os, "geteuid", return_value=1000):
            self.assertIsNone(privilege.target())
            # Not merely "no target": the spawn arguments must be empty, or a
            # developer running the service by hand would have every job try a
            # setuid it is not permitted to do.
            self.assertEqual(privilege.spawn_kwargs(), {})

    def test_no_drop_when_the_tree_is_root_owned(self):
        # The .deb installs root-owned under /opt/intel/smartune. Running as
        # before is the accepted outcome there; refusing to benchmark is not.
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=0)):
            self.assertIsNone(privilege.target())
            self.assertEqual(privilege.spawn_kwargs(), {})

    def test_no_drop_when_the_owner_has_no_passwd_entry(self):
        # A tree copied off another machine. We could setuid to the bare uid,
        # but with no passwd entry there is no group list either -- and a child
        # without `render` cannot open the GPU, which fails a run far more
        # confusingly than not dropping at all.
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=4242)), \
             mock.patch("pwd.getpwuid", side_effect=KeyError(4242)):
            self.assertIsNone(privilege.target())
            self.assertEqual(privilege.spawn_kwargs(), {})

    def test_no_drop_when_the_tree_cannot_be_stat_ed(self):
        broken = _FakeSrcRoot(uid=0, error=OSError("gone"))
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", broken):
            self.assertIsNone(privilege.target())

    def test_target_is_the_owner_of_the_tree(self):
        entry = types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="nas",
                                      pw_dir="/home/nas")
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=1000, gid=1000)), \
             mock.patch("pwd.getpwuid", return_value=entry), \
             mock.patch.object(os, "getgrouplist", return_value=[1000, 992, 27]):
            who = privilege.target()
        self.assertIsNotNone(who)
        self.assertEqual((who.uid, who.gid, who.name), (1000, 1000, "nas"))
        # Sorted, and complete: 992 is `render` on this machine, and a child
        # missing it cannot open the GPU a benchmark exists to measure.
        self.assertEqual(who.groups, [27, 992, 1000])

    def test_spawn_kwargs_carry_every_group(self):
        with mock.patch.object(privilege, "target", return_value=_ME):
            kwargs = privilege.spawn_kwargs()
        self.assertEqual(kwargs["user"], _MY_UID)
        self.assertEqual(kwargs["group"], _MY_GID)
        self.assertEqual(kwargs["extra_groups"], _ME.groups)
        # Group- and world-readable, so the root parent can still read every
        # result the child writes (results.py reads all of them).
        self.assertEqual(kwargs["umask"], 0o022)

    def test_the_resolution_is_cached(self):
        entry = types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="nas",
                                      pw_dir="/home/nas")
        lookup = mock.Mock(return_value=entry)
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=1000, gid=1000)), \
             mock.patch("pwd.getpwuid", lookup), \
             mock.patch.object(os, "getgrouplist", return_value=[1000]):
            privilege.target()
            privilege.target()
            privilege.target()
        self.assertEqual(lookup.call_count, 1)


class SpawnPointCoverageTests(unittest.TestCase):
    """Every subprocess in the package drops. Structural, so additions fail here.

    A spawn point that forgets the drop does not fail anything at runtime -- it
    just runs as root again -- which is exactly the kind of regression a test
    has to catch instead of a reviewer.
    """

    # search_models.py is deliberately absent: it IS one of these children (see
    # models.py), so the `hf` calls it makes already inherit the drop.
    _PACKAGE = Path(_REPO_ROOT) / "benchmark" / "service"
    _EXEMPT = {"search_models.py"}

    def _spawn_calls(self, path: Path):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in ("run", "Popen", "call", "check_output",
                                      "check_call")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"):
                yield node

    @staticmethod
    def _has_spawn_kwargs(call: ast.Call) -> bool:
        for keyword in call.keywords:
            # ** unpacking has arg=None.
            if keyword.arg is not None:
                continue
            value = keyword.value
            if (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "spawn_kwargs"):
                return True
        return False

    def test_every_spawn_point_drops_privileges(self):
        checked = 0
        for path in sorted(self._PACKAGE.glob("*.py")):
            if path.name in self._EXEMPT:
                continue
            for call in self._spawn_calls(path):
                checked += 1
                self.assertTrue(
                    self._has_spawn_kwargs(call),
                    f"{path.name}:{call.lineno} spawns a subprocess without "
                    f"**privilege.spawn_kwargs() -- it would run as root",
                )
        # Positive control: if the walk ever stops finding the calls, the loop
        # above passes vacuously and the guard is gone.
        self.assertGreaterEqual(checked, 4, "expected at least the four known "
                                            "spawn points to be found")


class JobSpawnTests(unittest.TestCase):
    """The single choke point: both setup and run come through JobManager.start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A fresh manager, not the module singleton: this test starts jobs, and
        # the singleton is shared with runner.py's listener.
        self.manager = jobs.JobManager()

    def _start(self):
        proc = mock.Mock()
        proc.pid = 4242
        proc.wait.return_value = 0
        with mock.patch.object(jobs.subprocess, "Popen", return_value=proc) as popen:
            self.manager.start(
                kind="setup", argv=["true"], cwd=self.tmp.name, env={},
                log_path=Path(self.tmp.name) / "nested" / "job.log",
            )
        return popen.call_args.kwargs

    def test_the_child_is_dropped(self):
        with mock.patch.object(privilege, "target", return_value=_ME):
            kwargs = self._start()
        self.assertEqual(kwargs["user"], _MY_UID)
        self.assertEqual(kwargs["group"], _MY_GID)
        self.assertEqual(kwargs["extra_groups"], _ME.groups)
        self.assertEqual(kwargs["umask"], 0o022)
        # The drop must not have cost the process group cancel() relies on.
        self.assertTrue(kwargs["start_new_session"])

    def test_nothing_is_passed_when_there_is_no_target(self):
        with mock.patch.object(privilege, "target", return_value=None):
            kwargs = self._start()
        for name in ("user", "group", "extra_groups", "umask"):
            self.assertNotIn(name, kwargs)

    def test_the_log_directory_is_handed_over(self):
        with mock.patch.object(privilege, "target", return_value=_ME), \
             mock.patch.object(privilege, "chown") as chown:
            self._start()
        # .../nested did not exist, so the child -- which writes nothing else
        # into it -- gets it.
        self.assertIn(Path(self.tmp.name) / "nested",
                      [call.args[0] for call in chown.call_args_list])

    def test_the_log_file_is_handed_over(self):
        # The parent opens the log and passes the descriptor down, so the inode
        # is root's however the child is spawned. Every other artifact of a run
        # is the target user's (the script, the metrics CSV); the log -- the one
        # a user actually opens -- was the only thing left behind as root.
        with mock.patch.object(privilege, "target", return_value=_ME), \
             mock.patch.object(privilege, "chown") as chown:
            self._start()
        self.assertIn(Path(self.tmp.name) / "nested" / "job.log",
                      [call.args[0] for call in chown.call_args_list])


class EnsureDirTests(unittest.TestCase):
    """Only what this call created is handed over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_only_new_levels_are_chowned(self):
        (self.root / "existing").mkdir()
        with mock.patch.object(privilege, "target", return_value=_ME), \
             mock.patch.object(privilege, "chown") as chown:
            privilege.ensure_dir(self.root / "existing" / "a" / "b")
        handed = [call.args[0] for call in chown.call_args_list]
        # An operator who deliberately made part of the tree root-owned (or
        # pointed env_root at a shared volume) must not have that undone.
        self.assertNotIn(self.root / "existing", handed)
        self.assertNotIn(self.root, handed)
        self.assertEqual(handed, [self.root / "existing" / "a",
                                  self.root / "existing" / "a" / "b"])
        self.assertTrue((self.root / "existing" / "a" / "b").is_dir())

    def test_nothing_is_chowned_without_a_target(self):
        with mock.patch.object(privilege, "target", return_value=None), \
             mock.patch.object(privilege, "chown") as chown:
            privilege.ensure_dir(self.root / "a" / "b")
        chown.assert_not_called()
        self.assertTrue((self.root / "a" / "b").is_dir())

    def test_an_existing_directory_is_accepted(self):
        with mock.patch.object(privilege, "target", return_value=_ME):
            privilege.ensure_dir(self.root)          # already there
            privilege.ensure_dir(self.root)          # and again
        self.assertTrue(self.root.is_dir())


class ChildEnvironmentTests(unittest.TestCase):
    """HOME has to be somewhere the child owns, or uv/pip/git fail on /root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"

    def _build(self, target, base_env):
        with mock.patch.object(privilege, "target", return_value=target), \
             mock.patch.object(env, "env_root", return_value=self.root), \
             mock.patch.dict(os.environ, base_env, clear=False):
            return env.build_subprocess_env()

    def test_home_moves_into_the_runtime_tree(self):
        built = self._build(_ME, {"HOME": "/root"})
        home = Path(built["HOME"])
        self.assertEqual(home, self.root / ".gen" / "home")
        # Created here, not left for the child: the child is the one that cannot
        # create it.
        self.assertTrue(home.is_dir())
        self.assertEqual(built["USER"], "tester")
        self.assertEqual(built["LOGNAME"], "tester")

    def test_roots_xdg_directories_are_dropped(self):
        built = self._build(_ME, {
            "HOME": "/root",
            "XDG_CACHE_HOME": "/root/.cache",
            "XDG_DATA_HOME": "/root/.local/share",
            "XDG_CONFIG_HOME": "/root/.config",
            "XDG_STATE_HOME": "/root/.local/state",
            "XDG_RUNTIME_DIR": "/run/user/0",
        })
        # Left in place, these keep pointing at /root even once HOME does not --
        # which is where uv would try to install a managed Python.
        for name in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                     "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            self.assertNotIn(name, built)

    def test_the_environment_is_untouched_without_a_target(self):
        built = self._build(None, {"HOME": "/home/dev",
                                   "XDG_CACHE_HOME": "/home/dev/.cache"})
        self.assertEqual(built["HOME"], "/home/dev")
        self.assertEqual(built["XDG_CACHE_HOME"], "/home/dev/.cache")


class RenderedScriptTests(unittest.TestCase):
    """The 0700 run script has to belong to whoever runs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"

    def test_the_script_is_handed_over_and_stays_private(self):
        entries = [{"id": "org/model", "type": "model_id", "build": "int4"}]
        with mock.patch.object(privilege, "target", return_value=_ME), \
             mock.patch.object(env, "paths", return_value={"runs": self.runs}), \
             mock.patch.object(privilege, "chown") as chown:
            script_path, models_json = runner.render_script(entries, "benchmark", "abc123")

        # 0700 owned by root is a script the child cannot read, and reading it
        # is the entire job -- so the handover is what makes the mode safe.
        chown.assert_any_call(script_path)
        self.assertEqual(stat.S_IMODE(script_path.stat().st_mode), 0o700)
        self.assertIn("org/model", models_json)
        self.assertIn("org/model", script_path.read_text())


class DescribeTests(unittest.TestCase):
    """The diagnostic block, which is what a permission failure is read against."""

    def setUp(self):
        privilege.target.cache_clear()
        self.addCleanup(privilege.target.cache_clear)

    def test_it_names_the_account_and_every_path(self):
        entry = types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="nas",
                                      pw_dir="/home/nas")
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=1000, gid=1000)), \
             mock.patch("pwd.getpwuid", return_value=entry), \
             mock.patch.object(os, "getgrouplist", return_value=[1000, 992]):
            block = "\n".join(privilege.describe())
        self.assertIn("run as", block)
        self.assertIn("nas", block)
        self.assertIn("child HOME", block)
        for name in env.paths():
            self.assertIn(f"path {name}", block)

    def test_it_says_why_when_nothing_was_dropped(self):
        with mock.patch.object(os, "geteuid", return_value=0), \
             mock.patch.object(privilege, "SRC_ROOT", _fake_src_root(uid=0)):
            block = "\n".join(privilege.describe())
        # "not dropped" with no reason sends the reader to the source; the
        # reason is the whole value of printing this into the job log.
        self.assertIn("not dropped", block)
        self.assertIn("owned by root", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
