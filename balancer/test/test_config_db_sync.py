# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the config.yaml <-> database reconciliation (utils/app_utils).

A managed app is registered in config.yaml and tracked in the app table.  Deleting
its config entry by hand used to leave the row marked ``controlled``, so the app
stayed monitored, OOM-adjusted and network-classified off the database while every
config-driven lookup came up empty.  These tests drive synthetic config lists and
rows, so they cover that split state without needing a database.

Run:  python3 balancer/test/test_config_db_sync.py
"""

import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils import app_utils
from db.DatabaseModel import DBStatus


def _entry(name, app_id, bpf=None, procs=None, cmdline="/usr/bin/demo"):
    return {
        "name": name,
        "id": app_id,
        "commandline": cmdline,
        "bpf_name": bpf if bpf is not None else [name[:15]],
        "process_names": procs if procs is not None else [name],
    }


class _Row:
    """Just enough of an AIAppPriority row for the reconciliation."""

    def __init__(self, app_id, name, controlled=True, priority="high", meta=None):
        self.id = app_id
        self.app_id = app_id
        self.name = name
        self.controlled = controlled
        self.priority = priority
        self.config_meta_json = json.dumps(meta, sort_keys=True) if meta is not None else None


def _meta_of(entry):
    return app_utils.build_config_meta(entry)


class DiffControlledAppsTests(unittest.TestCase):
    def test_in_sync_yields_nothing(self):
        entry = _entry("demo", "demo.id")
        row = _Row("demo.id", "demo", meta=_meta_of(entry))
        orphans, stale = app_utils.diff_controlled_apps([entry], [row])
        self.assertEqual(orphans, [])
        self.assertEqual(stale, [])

    def test_changed_config_entry_refreshes_snapshot(self):
        """A hand-edited process_names must reach the stored snapshot, or the
        recovery block printed for a later deletion would be out of date."""
        row = _Row("demo.id", "demo", meta=_meta_of(_entry("demo", "demo.id")))
        edited = _entry("demo", "demo.id", procs=["demo", "demo-worker"])
        orphans, stale = app_utils.diff_controlled_apps([edited], [row])
        self.assertEqual(orphans, [])
        self.assertEqual([s["app_id"] for s in stale], ["demo.id"])
        self.assertEqual(stale[0]["meta"]["process_names"], ["demo", "demo-worker"])

    def test_missing_snapshot_is_backfilled(self):
        """Rows predating config_meta_json get one on the next start."""
        entry = _entry("demo", "demo.id")
        orphans, stale = app_utils.diff_controlled_apps([entry], [_Row("demo.id", "demo")])
        self.assertEqual(orphans, [])
        self.assertEqual([s["app_id"] for s in stale], ["demo.id"])

    def test_deleted_config_entry_makes_controlled_row_an_orphan(self):
        """The reported bug: the entry is gone from config.yaml but the row still
        says controlled, so the balancer keeps managing an app it cannot describe."""
        row = _Row("gone.id", "gone", meta=_meta_of(_entry("gone", "gone.id")))
        orphans, stale = app_utils.diff_controlled_apps([_entry("demo", "demo.id")], [row])
        self.assertEqual([o["app_id"] for o in orphans], ["gone.id"])
        self.assertEqual(stale, [])
        # The stored snapshot rides along so the warning can print a pasteable entry.
        self.assertEqual(orphans[0]["meta"]["bpf_name"], ["gone"])

    def test_uncontrolled_row_without_config_entry_is_left_alone(self):
        """Registered but never enabled — nothing keys off it, so do not touch it."""
        row = _Row("idle.id", "idle", controlled=False)
        orphans, stale = app_utils.diff_controlled_apps([], [row])
        self.assertEqual(orphans, [])
        self.assertEqual(stale, [])

    def test_entry_without_id_matches_by_name(self):
        """fetch_all_apps() already warns about id-less entries; treating one as a
        deletion would silently un-control a working app."""
        entry = {"name": "Demo", "commandline": "/usr/bin/demo", "process_names": ["demo"]}
        orphans, _ = app_utils.diff_controlled_apps([entry], [_Row("demo.id", "demo")])
        self.assertEqual(orphans, [])

    def test_no_controlled_apps_section_orphans_every_controlled_row(self):
        """`controlled_apps:` with only comments parses as None — the shipped state."""
        rows = [_Row("a.id", "a"), _Row("b.id", "b", controlled=False)]
        orphans, stale = app_utils.diff_controlled_apps(None, rows)
        self.assertEqual([o["app_id"] for o in orphans], ["a.id"])
        self.assertEqual(stale, [])

    def test_non_dict_config_entries_are_ignored(self):
        orphans, _ = app_utils.diff_controlled_apps(["oops", None], [_Row("a.id", "a")])
        self.assertEqual([o["app_id"] for o in orphans], ["a.id"])


class _Field:
    """Turns ``Model.field == value`` into a row predicate, as peewee would."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return lambda row: getattr(row, self.name, None) == value


class _Select:
    """The subset of a peewee select the reconciliation actually uses."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def where(self, predicate):
        return _Select([r for r in self._rows if predicate(r)])

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeModel:
    """Records update_record() calls instead of touching the database."""

    app_id = _Field("app_id")

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def query(self):
        return _Select(self._rows)

    def update_record(self, id, **data):
        self.updates.append((id, data))
        return DBStatus.SUCCESS


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self._real_model = app_utils.AIAppPriority
        self.addCleanup(setattr, app_utils, "AIAppPriority", self._real_model)
        self._real_cfg = app_utils.b_config.controlled_apps
        self.addCleanup(setattr, app_utils.b_config, "controlled_apps", self._real_cfg)

    def _run(self, config_apps, rows):
        app_utils.b_config.controlled_apps = config_apps
        fake = _FakeModel(rows)
        app_utils.AIAppPriority = fake
        return fake, app_utils.reconcile_controlled_apps()

    def test_orphan_is_only_switched_to_uncontrolled(self):
        """Nothing else is touched: priority, oom_score and any saved limit
        overrides must survive so re-enabling the app restores its settings."""
        row = _Row("gone.id", "gone", meta=_meta_of(_entry("gone", "gone.id")))
        fake, result = self._run([], [row])
        self.assertEqual(result["uncontrolled"], ["gone.id"])
        self.assertEqual(fake.updates, [("gone.id", {"controlled": False})])

    def test_snapshot_refresh_writes_only_config_meta(self):
        entry = _entry("demo", "demo.id")
        fake, result = self._run([entry], [_Row("demo.id", "demo")])
        self.assertEqual(result["uncontrolled"], [])
        self.assertEqual(result["meta_refreshed"], ["demo.id"])
        (row_id, data), = fake.updates
        self.assertEqual(row_id, "demo.id")
        self.assertEqual(json.loads(data["config_meta_json"]), _meta_of(entry))

    def test_in_sync_writes_nothing(self):
        entry = _entry("demo", "demo.id")
        row = _Row("demo.id", "demo", meta=_meta_of(entry))
        fake, result = self._run([entry], [row])
        self.assertEqual(fake.updates, [])
        self.assertEqual(result, {"uncontrolled": [], "meta_refreshed": []})

    def test_database_failure_does_not_propagate(self):
        """Reconciliation runs before the service starts; it must never keep it down."""
        class _Boom:
            @staticmethod
            def query():
                raise RuntimeError("database is locked")

        app_utils.b_config.controlled_apps = []
        app_utils.AIAppPriority = _Boom
        self.assertEqual(
            app_utils.reconcile_controlled_apps(),
            {"uncontrolled": [], "meta_refreshed": []},
        )


class OomPriorityTests(unittest.TestCase):
    def setUp(self):
        self._real_scores = app_utils._original_oom_scores
        app_utils._original_oom_scores = {}
        self.addCleanup(setattr, app_utils, "_original_oom_scores", self._real_scores)

    def test_restore_only_writes_the_app_snapshot_pids(self):
        app_utils._original_oom_scores = {
            "optimum.scope": {"3747712": "125"},
            "other.scope": {"4000000": "42"},
        }
        writes = []
        updates = []

        with (
            mock.patch.object(app_utils.subprocess, "run", side_effect=AssertionError("pgrep must not run")),
            mock.patch.object(app_utils.os.path, "exists", return_value=True),
            mock.patch.object(app_utils, "write_cgroup_file", side_effect=lambda *args, **kwargs: writes.append(args)),
            mock.patch.object(app_utils, "_update_app_oom_score_adj", side_effect=lambda *args: updates.append(args)),
        ):
            app_utils.adjust_oom_priority(
                "optimum.scope", "optimum-cli", "critical", "/venv/bin/python /venv/bin/optimum-cli", restore=True
            )

        self.assertEqual(writes, [("125", "/proc/3747712/oom_score_adj")])
        self.assertEqual(updates, [("optimum.scope", 125)])
        self.assertNotIn("optimum.scope", app_utils._original_oom_scores)
        self.assertEqual(app_utils._original_oom_scores["other.scope"], {"4000000": "42"})


class ReEnableTests(unittest.TestCase):
    """Re-adding an app whose config entry the user deleted (Balancer "Option 2")."""

    def setUp(self):
        self._real_model = app_utils.AIAppPriority
        self.addCleanup(setattr, app_utils, "AIAppPriority", self._real_model)
        self._real_cfg = app_utils.b_config.controlled_apps
        self.addCleanup(setattr, app_utils.b_config, "controlled_apps", self._real_cfg)
        self._real_append = app_utils.b_config.append_to_list_section
        self.addCleanup(setattr, app_utils.b_config, "append_to_list_section", self._real_append)
        self.appended = []
        app_utils.b_config.append_to_list_section = (
            lambda section, entry, path=None: bool(self.appended.append((section, entry)) is None)
        )

    def test_only_apps_missing_from_config_are_offered(self):
        registered = _entry("demo", "demo.id")
        app_utils.b_config.controlled_apps = [registered]
        app_utils.AIAppPriority = _FakeModel([
            _Row("demo.id", "demo", meta=_meta_of(registered)),
            _Row("gone.id", "gone", controlled=False, meta=_meta_of(_entry("gone", "gone.id"))),
        ])
        offered = app_utils.fetch_unregistered_apps()
        self.assertEqual([a["app_id"] for a in offered], ["gone.id"])
        self.assertTrue(offered[0]["previously_managed"])
        self.assertEqual(offered[0]["process_names"], ["gone"])

    def test_uncontrolled_app_keeps_its_saved_priority(self):
        entry = _entry("gone", "gone.id")
        app_utils.b_config.controlled_apps = []
        app_utils.AIAppPriority = _FakeModel([
            _Row("gone.id", "gone", controlled=False, priority="critical", meta=_meta_of(entry)),
        ])

        offered = app_utils.fetch_unregistered_apps()

        self.assertEqual(offered[0]["priority"], "critical")

    def test_re_enabling_writes_the_config_entry_back(self):
        """Without this the app would be controlled in the database but absent from
        config.yaml, and the next startup reconciliation would un-control it again."""
        gone = _entry("gone", "gone.id")
        app_utils.b_config.controlled_apps = []
        app_utils.AIAppPriority = _FakeModel([_Row("gone.id", "gone", controlled=False, meta=_meta_of(gone))])
        self.assertTrue(app_utils.restore_config_entry("gone.id"))
        self.assertEqual(self.appended, [("controlled_apps", gone)])

    def test_restore_is_a_no_op_when_the_entry_is_still_there(self):
        entry = _entry("demo", "demo.id")
        app_utils.b_config.controlled_apps = [entry]
        app_utils.AIAppPriority = _FakeModel([_Row("demo.id", "demo", meta=_meta_of(entry))])
        self.assertFalse(app_utils.restore_config_entry("demo.id"))
        self.assertEqual(self.appended, [])

    def test_row_without_snapshot_still_restores_name_id_and_cmdline(self):
        """Rows predating config_meta_json: the entry comes back without the BPF
        hints, which the user can fill in, rather than not at all."""
        row = _Row("old.id", "old")
        row.cmdline = "/usr/bin/old --serve"
        app_utils.b_config.controlled_apps = []
        app_utils.AIAppPriority = _FakeModel([row])
        self.assertTrue(app_utils.restore_config_entry("old.id"))
        self.assertEqual(self.appended, [("controlled_apps", {
            "name": "old", "id": "old.id", "commandline": "/usr/bin/old --serve",
            "bpf_name": [], "process_names": [],
        })])


class RenderConfigEntryTests(unittest.TestCase):
    def test_rendered_entry_parses_back_to_the_original(self):
        """The warning tells the user to paste this block into config.yaml, so it
        has to survive a round-trip through the YAML parser."""
        import yaml

        entry = _entry("Helicon Search", "hs_agent.service",
                       bpf=["HeliconSearch_a"], procs=["HeliconSearch_agent", "VLMService"])
        block = app_utils.render_config_entry_yaml(
            entry["name"], entry["id"], app_utils.build_config_meta(entry)
        )
        parsed = yaml.safe_load("controlled_apps:\n" + block)["controlled_apps"]
        self.assertEqual(parsed, [entry])

    def test_missing_snapshot_still_renders(self):
        block = app_utils.render_config_entry_yaml("demo", "demo.id", None)
        self.assertIn('- name: "demo"', block)
        self.assertIn("bpf_name: []", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
