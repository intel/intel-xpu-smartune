# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for exact app-process identity (utils/app_utils).

Resource limits are written to whatever cgroups the matched PIDs live in, so an
over-matched process name silently limits somebody else. These tests drive a synthetic
process table rather than the live one, so they cover the collisions that matter without
needing those processes to be running.

Run:  python3 balancer/test/test_app_identity.py
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils import app_utils


class _Proc:
    """Just enough of a psutil.Process for process_iter(attrs=...)."""

    def __init__(self, pid, name, exe, cmdline):
        self.info = {'pid': pid, 'name': name, 'exe': exe, 'cmdline': cmdline}


def _no_fuzzy(*_a, **_kw):
    """Stub for the fuzzy fallback: an empty result proves a hit came from exact matching."""
    return []


# The real workload from balancer/test/testing_io.sh: three fio binaries whose names are
# prefixes of one another, each started by a systemd-run launcher that stays behind in the
# terminal's own scope with the target binary in its command line.
_TABLE = [
    _Proc(1, 'fio_lo', '/tmp/fio_lo', ['/tmp/fio_lo', '--name=lo']),
    _Proc(2, 'fio_lo2', '/tmp/fio_lo2', ['/tmp/fio_lo2', '--name=lo2']),
    _Proc(3, 'fio_lo3', '/tmp/fio_lo3', ['/tmp/fio_lo3', '--name=lo3']),
    _Proc(4, 'systemd-run', '/usr/bin/systemd-run',
          ['systemd-run', '--scope', '--unit=lo2-io.scope', '/tmp/fio_lo2', '--name=lo2']),
]


class ExactProcessNameTests(unittest.TestCase):
    def setUp(self):
        self._real_iter = app_utils.psutil.process_iter
        app_utils.psutil.process_iter = lambda attrs=None: iter(_TABLE)
        self.addCleanup(setattr, app_utils.psutil, 'process_iter', self._real_iter)
        # Without this the fuzzy fallback would run against the *live* process table
        # whenever a test expects no match, making results depend on the host.
        self.addCleanup(setattr, app_utils, 'get_app_processes', app_utils.get_app_processes)
        app_utils.get_app_processes = _no_fuzzy

    def test_prefix_names_do_not_collide(self):
        """The measured bug: "fio_lo" claimed fio_lo2 and fio_lo3, so limiting the low-prio
        app also limited the unmanaged one -- at the wrong (higher) rate class, and the
        unmanaged-app test case never actually ran unmanaged."""
        self.assertEqual(app_utils.get_app_processes_by_exact_name('fio_lo'), [1])
        self.assertEqual(app_utils.get_app_processes_by_exact_name('fio_lo2'), [2])

    def test_launcher_is_not_the_app(self):
        """systemd-run carries the target binary in its argv but lives in the launching
        terminal's cgroup, so matching it drags the whole terminal into the limit."""
        self.assertNotIn(4, app_utils.get_app_processes_by_exact_name('fio_lo2'))

    def test_matches_on_comm_when_argv0_was_rewritten(self):
        table = [_Proc(9, 'worker', '/opt/app/worker', ['[renamed]'])]
        app_utils.psutil.process_iter = lambda attrs=None: iter(table)
        self.assertEqual(app_utils.get_app_processes_by_exact_name('worker'), [9])

    def test_matches_on_exe_when_comm_was_truncated(self):
        # The kernel truncates comm to 15 chars; a longer configured name only ever
        # matches via exe / argv[0].
        long_name = 'very-long-daemon-name'
        table = [_Proc(9, long_name[:15], f'/opt/{long_name}', [f'/opt/{long_name}'])]
        app_utils.psutil.process_iter = lambda attrs=None: iter(table)
        self.assertEqual(app_utils.get_app_processes_by_exact_name(long_name), [9])

    def test_empty_query_matches_nothing(self):
        # Must not degrade into "every process", which would limit the whole machine.
        self.assertEqual(app_utils.get_app_processes_by_exact_name(''), [])
        self.assertEqual(app_utils.get_app_processes_by_exact_name(None), [])

    def test_controlled_app_lookup_uses_exact_identity(self):
        self.assertEqual(app_utils.get_app_processes_for_app('fio_lo2'), [2])
        self.assertNotIn(4, app_utils.get_app_processes_for_app('fio_lo2'))


class DerivedIdentityTests(unittest.TestCase):
    """The half of the match that exec-name comparison alone cannot do.

    The wizard writes the *script* into process_names for anything launched through an
    interpreter or a shell, so for those apps the configured name matches none of argv0,
    comm or exe -- all three read "python3" / "bash".
    """

    def setUp(self):
        self._real_iter = app_utils.psutil.process_iter
        self.addCleanup(setattr, app_utils.psutil, 'process_iter', self._real_iter)
        self.addCleanup(setattr, app_utils, 'get_app_processes', app_utils.get_app_processes)
        app_utils.get_app_processes = _no_fuzzy

    def _table(self, table):
        app_utils.psutil.process_iter = lambda attrs=None: iter(table)

    def test_interpreted_app_is_found_by_its_script_name(self):
        self._table([_Proc(9, 'python3', '/usr/bin/python3',
                           ['python3', '/opt/app/server.py', '--port=8080'])])
        self.assertEqual(app_utils.get_app_processes_by_exact_name('server.py'), [9])

    def test_shell_wrapped_app_is_found_by_its_script_name(self):
        self._table([_Proc(9, 'bash', '/usr/bin/bash', ['bash', '/opt/run.sh'])])
        self.assertEqual(app_utils.get_app_processes_by_exact_name('run.sh'), [9])

    def test_launcher_carrying_the_binary_is_still_not_the_app(self):
        """The measured pair: the real fio derives to "fio_lo", while the sudo/systemd-run
        launcher that names it in argv derives to "systemd-run" -- and lives in the
        terminal's own scope, so matching it would limit the whole terminal."""
        self._table([
            _Proc(1, 'fio_lo', '/tmp/fio_lo', ['/tmp/fio_lo', '--name=lo']),
            _Proc(2, 'sudo', '/usr/bin/sudo',
                  ['sudo', 'systemd-run', '--scope', '--unit=lo-io.scope',
                   '/tmp/fio_lo', '--name=lo']),
        ])
        self.assertEqual(app_utils.get_app_processes_by_exact_name('fio_lo'), [1])

    def test_interpreter_name_still_matches_via_exec_fields(self):
        # A hand-edited config may name the interpreter itself. Derivation can never
        # return "python3", so only the exec-field half of the union can find this.
        self._table([_Proc(9, 'python3', '/usr/bin/python3', ['python3', '-c', 'pass'])])
        self.assertEqual(app_utils.get_app_processes_by_exact_name('python3'), [9])


class NoExactMatchTests(unittest.TestCase):
    """What happens when nothing matches: report, do not guess.

    Measured on the live machine after the workload exited: "fio_lo" matched no process
    exactly, and `pgrep -fi fio_lo` returned the diagnostic shell command that merely
    mentioned the name. Returning those would have written io.max into the session scope.
    """

    def setUp(self):
        self._real_iter = app_utils.psutil.process_iter
        app_utils.psutil.process_iter = lambda attrs=None: iter([])
        self.addCleanup(setattr, app_utils.psutil, 'process_iter', self._real_iter)
        self.addCleanup(setattr, app_utils, 'get_app_processes', app_utils.get_app_processes)

    def test_fuzzy_only_hits_are_reported_but_not_returned(self):
        app_utils.get_app_processes = lambda name: [7, 7, 3]
        # Named explicitly: the project logger does not propagate to root.
        with self.assertLogs(app_utils.logger.name, level='WARNING') as captured:
            self.assertEqual(app_utils.get_app_processes_by_exact_name('whatever'), [])
        self.assertIn('not limiting them', "\n".join(captured.output))

    def test_silent_when_nothing_matches_at_all(self):
        # The ordinary "app is not running" case must not spam a warning every tick.
        app_utils.get_app_processes = _no_fuzzy
        self.assertEqual(app_utils.get_app_processes_by_exact_name('whatever'), [])

    def test_fuzzy_is_not_consulted_when_the_exact_match_succeeded(self):
        app_utils.psutil.process_iter = lambda attrs=None: iter(_TABLE)
        app_utils.get_app_processes = lambda name: [999]
        self.assertEqual(app_utils.get_app_processes_by_exact_name('fio_lo'), [1])


class CgroupOwnerTests(unittest.TestCase):
    """The reverse lookup: given a running process, which configured app owns it.

    Separate from the tests above because it is a separate code path -- res_monitor uses it
    to decide which cgroups get merged into an app, and that merged set is what the
    balancer writes io.max to. It used to ask whether any configured name appeared
    anywhere in the command line, which claimed the launcher (and thus the terminal scope
    it lives in) for the app being launched.
    """

    def setUp(self):
        from monitor.res_monitor import ResourceMonitor
        # Bypass __init__: it starts sampling threads and reads the live config, none of
        # which the mapping under test touches.
        self.mon = ResourceMonitor.__new__(ResourceMonitor)
        self.mon._proc_name_to_app = {'fio_lo': 'lo-io.scope', 'server.py': 'web'}

    def _owner(self, proc):
        return self.mon._app_id_for_process(proc.info)

    def test_the_app_itself_is_matched(self):
        self.assertEqual(
            self._owner(_Proc(1, 'fio_lo', '/tmp/fio_lo', ['/tmp/fio_lo', '--name=lo'])),
            'lo-io.scope')

    def test_launcher_is_not_the_app(self):
        self.assertEqual(self._owner(_Proc(2, 'sudo', '/usr/bin/sudo', [
            'sudo', 'systemd-run', '--scope', '--unit=lo-io.scope',
            '/tmp/fio_lo', '--name=lo'])), '')

    def test_prefix_named_app_is_not_claimed(self):
        self.assertEqual(
            self._owner(_Proc(3, 'fio_lo2', '/tmp/fio_lo2', ['/tmp/fio_lo2', '--name=lo2'])), '')

    def test_interpreted_app_is_matched_by_script_name(self):
        self.assertEqual(self._owner(_Proc(4, 'python3', '/usr/bin/python3', [
            'python3', '/opt/app/server.py', '--port=8080'])), 'web')

    def test_unrelated_process_mentioning_the_name_is_not_matched(self):
        self.assertEqual(self._owner(_Proc(5, 'bash', '/usr/bin/bash', [
            'bash', '-c', 'cat /proc/*/cgroup | grep fio_lo'])), '')

    def test_process_without_exe_or_cmdline_is_not_matched(self):
        # process_iter fills unreadable fields with None; must not resolve to the ''
        # dictionary key or every kernel thread would join some app.
        self.assertEqual(self._owner(_Proc(6, 'kworker/0:1', None, [])), '')


class RegisteredAppMatchTests(unittest.TestCase):
    """The forward lookup: given an aggregated cgroup sample, which registered app is it.

    Feeds ResourceMonitor.try_match_app the shape _get_top_processes produces. What this
    returns decides which app a cgroup's usage is reported under -- and therefore which
    cgroup receives that app's limit -- so an over-match here moves a limit onto an
    unrelated app. ``id`` is deliberately the configured app id, which need not name a
    cgroup: the balancer resolves a controlled app to its cgroup set separately.
    """

    def _monitor(self, apps):
        from monitor.res_monitor import ResourceMonitor
        # Bypass __init__: it starts sampling threads and reads the live config, neither of
        # which the index under test touches.
        mon = ResourceMonitor.__new__(ResourceMonitor)
        mon._proc_name_to_app = {}
        mon._multiprocess_apps = {}
        mon.desktop_apps = {a["app_id"]: a for a in apps}
        mon._desktop_name_to_app = {}
        mon._desktop_exe_to_app = {}
        mon._build_desktop_identity_index()
        return mon

    @staticmethod
    def _app(app_id, name, cmdline="", process_names=()):
        return {"app_id": app_id, "name": name, "display_name": name,
                "cmdline": cmdline, "process_names": list(process_names)}

    def _match(self, mon, dominant_name, exe=''):
        return mon.try_match_app(
            {'dominant_name': dominant_name, 'names': {dominant_name}, 'exe': exe})

    def test_app_is_matched_by_its_own_name(self):
        mon = self._monitor([self._app("lo-io.scope", "fio_lo", "/tmp/fio_lo --name=lo")])
        self.assertEqual(self._match(mon, "fio_lo")['id'], "lo-io.scope")

    def test_prefix_named_process_is_not_claimed(self):
        """The measured over-match: a registered app whose name is a prefix of another
        process's name used to claim that process, so the unregistered one's IO was
        reported against the registered app and the cap landed on the wrong cgroup."""
        mon = self._monitor([self._app("lo-io.scope", "fio_lo", "/tmp/fio_lo --name=lo")])
        self.assertIsNone(self._match(mon, "fio_lo2"))

    def test_display_name_shorter_than_the_executable_still_matches(self):
        """What the substring form was really covering: the app is registered under a
        display name while its process shows the executable from `commandline`."""
        mon = self._monitor([
            self._app("hs.id", "heliconSearch",
                      "/usr/local/heliconsearch/HeliconSearch_agent")])
        self.assertEqual(self._match(mon, "HeliconSearch_agent")['id'], "hs.id")

    def test_truncated_comm_still_matches(self):
        # comm is truncated to 15 chars by the kernel, so a longer executable is only
        # ever observed in that form.
        mon = self._monitor([
            self._app("hs.id", "heliconSearch",
                      "/usr/local/heliconsearch/HeliconSearch_agent")])
        self.assertEqual(self._match(mon, "HeliconSearch_a")['id'], "hs.id")

    def test_interpreted_app_matches_the_script_not_the_interpreter(self):
        mon = self._monitor([self._app("web.id", "web", "/usr/bin/python3 /opt/app/server.py")])
        self.assertEqual(self._match(mon, "server.py")['id'], "web.id")

    def test_interpreter_is_not_indexed_as_the_app(self):
        """Indexing the interpreter would hand the app every process sharing it."""
        mon = self._monitor([self._app("web.id", "web", "/usr/bin/python3 /opt/app/server.py")])
        self.assertIsNone(self._match(mon, "python3", exe="/usr/bin/python3"))

    def test_exe_path_matches_the_configured_executable(self):
        mon = self._monitor([self._app("hi-io.scope", "fio_high", "/tmp/fio_hi --name=hi")])
        self.assertEqual(self._match(mon, "whatever", exe="/tmp/fio_hi")['id'], "hi-io.scope")

    def test_first_registration_keeps_a_contested_identity(self):
        mon = self._monitor([
            self._app("first.id", "shared", "/opt/shared"),
            self._app("second.id", "shared", "/opt/shared"),
        ])
        self.assertEqual(self._match(mon, "shared")['id'], "first.id")

    def test_unknown_process_falls_through_to_the_cgroup_fallback(self):
        # No registered app matches, so the caller must still get the cgroup-derived
        # identity rather than a wrong app.
        mon = self._monitor([self._app("lo-io.scope", "fio_lo", "/tmp/fio_lo")])
        result = mon.try_match_app(
            {'dominant_name': 'fio_lo2', 'names': {'fio_lo2'}, 'exe': '/tmp/fio_lo2',
             'cgroup': '/system.slice/lo2-io.scope'})
        self.assertEqual(result['id'], 'lo2-io.scope')

    def test_session_scope_fallback_prefers_script_identity_over_interpreter(self):
        mon = self._monitor([self._app("lo-io.scope", "fio_lo", "/tmp/fio_lo")])
        result = mon.try_match_app(
            {
                'dominant_name': 'python3',
                'dominant_cmdline': 'python3 /opt/workloads/fio_runner.py --name lo',
                'names': {'python3'},
                'exe': '/usr/bin/python3',
                'cgroup': '/user.slice/session-12.scope',
            }
        )
        self.assertEqual(result['id'], 'fio_runner.py')
        self.assertEqual(result['name'], 'fio_runner.py')

    def test_transient_scope_fallback_prefers_the_dominant_workload_identity(self):
        mon = self._monitor([self._app("lo-io.scope", "fio_lo", "/tmp/fio_lo")])
        result = mon.try_match_app(
            {
                'dominant_name': 'python',
                'dominant_cmdline': '/usr/bin/python /opt/bin/optimum-cli export openvino',
                'names': {'python', 'bash'},
                'cgroup': '/user.slice/user-1000.slice/tmux-spawn-123.scope',
            }
        )
        self.assertEqual(result['id'], 'optimum-cli')
        self.assertEqual(result['name'], 'optimum-cli')


class RepresentativeCgroupTests(unittest.TestCase):
    """Which cgroup an app's matched PIDs are taken to live in.

    The limit is written to this cgroup, so picking it from a single PID is a
    correctness question, not a convenience one: the measured failure was a Low-priority
    fio capped on the *terminal's* vte-spawn scope while its workers ran unthrottled,
    because the launcher (lowest PID, so first in the list) never left the terminal.
    """

    def _vote(self, table):
        self.addCleanup(setattr, app_utils, 'get_cgroup_path_by_pid',
                        app_utils.get_cgroup_path_by_pid)
        app_utils.get_cgroup_path_by_pid = lambda pid: table.get(pid)
        return app_utils.dominant_cgroup_by_pids(sorted(table))

    def test_lone_launcher_does_not_decide_the_cgroup(self):
        cg, pid = self._vote({
            525660: '/user.slice/.../vte-spawn-9a573be3.scope',   # sudo systemd-run wrapper
            525663: '/system.slice/lo-io.scope',
            525664: '/system.slice/lo-io.scope',
            525665: '/system.slice/lo-io.scope',
        })
        self.assertEqual(cg, '/system.slice/lo-io.scope')
        self.assertEqual(pid, 525663)

    def test_single_process_app_is_unchanged(self):
        cg, pid = self._vote({4242: '/system.slice/hi-io.scope'})
        self.assertEqual((cg, pid), ('/system.slice/hi-io.scope', 4242))

    def test_tie_breaks_deterministically(self):
        # Same convention as the primary-cgroup pick in _resolve_controlled_target, so the
        # two agree on which cgroup is "the" one when an app genuinely straddles both.
        cg, _ = self._vote({1: '/system.slice/b.scope', 2: '/system.slice/a.scope'})
        self.assertEqual(cg, '/system.slice/a.scope')

    def test_pids_without_a_readable_cgroup_yield_nothing(self):
        self.assertEqual(self._vote({7: None, 8: None}), (None, None))

    def test_unreadable_pids_do_not_outvote_a_real_cgroup(self):
        cg, pid = self._vote({7: None, 8: '/system.slice/lo-io.scope', 9: None})
        self.assertEqual((cg, pid), ('/system.slice/lo-io.scope', 8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
