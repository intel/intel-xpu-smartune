# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Endpoint tests for the pressure relax/tighten preview and the startup/factory restore.

Everything runs against throw-away copies of config.yaml, so nothing touches the real one.

Run:  python3 balancer/test/test_pressure_tuning_api.py
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import config.config as config_module
from config.config import Config
from monitor import monitor_api, pressure_tuning
from monitor.disk_pressure import DiskIOMonitor

_SHIPPED = os.path.join(_REPO_ROOT, "config", "config.yaml")


class _ApiCase(unittest.TestCase):
    with_factory = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tuning-api-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live_path = os.path.join(self.tmp, "config.yaml")
        self.factory_path = os.path.join(self.tmp, "config.factory.yaml")
        shutil.copy(_SHIPPED, self.live_path)
        if self.with_factory:
            shutil.copy(_SHIPPED, self.factory_path)
        self.live = Config.from_file(self.live_path)

        resource_monitor = types.SimpleNamespace(
            get_disk_pressure_model=lambda: DiskIOMonitor._model(types.SimpleNamespace(config=self.live)))
        patches = [
            mock.patch.object(config_module, "b_config", self.live),
            mock.patch.object(pressure_tuning, "factory_config_path", return_value=self.factory_path),
            mock.patch.object(monitor_api, "_get_resource_monitor", return_value=resource_monitor),
            mock.patch.dict(monitor_api._startup_defaults, {}, clear=True),
            mock.patch.object(monitor_api, "_get_config_updated_at", return_value=0),
            mock.patch.object(monitor_api, "_bump_config_updated_at", return_value=1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(monitor_api.monitor_bp)
        self.client = app.test_client()

    def yaml_text(self):
        with open(self.live_path) as f:
            return f.read()

    def post(self, url, body):
        return self.client.post(url, json=body).get_json()


class PreviewTests(_ApiCase):
    def test_preview_proposes_a_step_and_writes_nothing(self):
        before = self.yaml_text()
        current = self.client.get("/monitor/pressure-tuning/system").get_json()["data"]["current"]
        out = self.post("/monitor/pressure-tuning/system", {"direction": "relax", "current": current})
        self.assertEqual(out["retcode"], 0, out)
        values = out["data"]["values"]
        self.assertGreater(values["memory_busy_threshold"], current["memory_busy_threshold"])
        self.assertEqual(self.yaml_text(), before)
        self.assertEqual(self.live.memory_busy_threshold, 80.0)

    def test_preview_uses_the_unsaved_form_values(self):
        out = self.post("/monitor/pressure-tuning/system", {
            "direction": "relax", "current": {"memory_busy_threshold": 90.0}})
        self.assertAlmostEqual(out["data"]["values"]["memory_busy_threshold"],
                               100 - 10 * 0.85, places=2)

    def test_without_a_direction_only_the_position_is_returned(self):
        out = self.post("/monitor/pressure-tuning/system", {
            "current": {"memory_busy_threshold": 90.0, "cpu_busy_threshold": 90.0}})
        self.assertEqual(out["retcode"], 0, out)
        self.assertNotIn("values", out["data"])
        self.assertTrue(any(d["label"] == "Memory busy threshold" for d in out["data"]["details"]))

    def test_state_endpoint_reports_factory_position(self):
        data = self.client.get("/monitor/pressure-tuning/disk").get_json()["data"]
        self.assertEqual(data["reference_source"], "factory")
        self.assertEqual(data["state"]["n"], 0)
        self.assertIn("disk_pressure_model.half_point_scale", data["paths"])

    def test_bad_group_direction_and_body_are_rejected(self):
        self.assertNotEqual(self.post("/monitor/pressure-tuning/cpu",
                                      {"direction": "relax", "current": {}})["retcode"], 0)
        self.assertNotEqual(self.post("/monitor/pressure-tuning/system",
                                      {"direction": "up", "current": {}})["retcode"], 0)
        self.assertNotEqual(self.post("/monitor/pressure-tuning/system",
                                      {"direction": "relax", "current": {"cpu_busy_threshold": "x"}})["retcode"], 0)
        self.assertNotEqual(self.post("/monitor/pressure-tuning/system",
                                      {"direction": "relax"})["retcode"], 0)

    def test_every_group_previews_against_the_shipped_config(self):
        for group in ("system", "disk", "network"):
            data = self.client.get(f"/monitor/pressure-tuning/{group}").get_json()["data"]
            self.assertEqual(sorted(data["current"]), sorted(data["paths"]), group)
            for direction in ("relax", "tighten"):
                out = self.post(f"/monitor/pressure-tuning/{group}",
                                {"direction": direction, "current": data["current"]})
                self.assertEqual(out["retcode"], 0, out)
                self.assertTrue(out["data"]["values"], f"{group} {direction} moved nothing")


class RestoreTests(_ApiCase):
    def _edit(self):
        r = self.post("/monitor/config/system_pressure", {"memory_busy_threshold": 91.5})
        self.assertEqual(r["retcode"], 0, r)
        r = self.post("/monitor/config/disk_pressure", {"disk_pressure_model": {"half_point_scale": 2.5}})
        self.assertEqual(r["retcode"], 0, r)
        self.assertEqual(self.live.memory_busy_threshold, 91.5)

    def test_factory_restore_writes_back_and_needs_no_save(self):
        self._edit()
        out = self.post("/monitor/config-restore", {"kind": "factory"})
        self.assertEqual(out["retcode"], 0, out)
        self.assertEqual(self.live.memory_busy_threshold, 80.0)
        self.assertEqual(self.live.disk_pressure_model["half_point_scale"], 1.0)
        self.assertIn("memory_busy_threshold: 80", self.yaml_text())

    def test_startup_restore_returns_to_the_values_of_this_run(self):
        self._edit()
        out = self.post("/monitor/config-restore", {"kind": "startup", "sections": ["system_pressure"]})
        self.assertEqual(out["retcode"], 0, out)
        self.assertEqual(self.live.memory_busy_threshold, 80.0)
        # Only the requested section is restored.
        self.assertEqual(self.live.disk_pressure_model["half_point_scale"], 2.5)

    def test_factory_restore_covers_every_restorable_section(self):
        out = self.post("/monitor/config-restore", {"kind": "factory"})
        self.assertEqual(out["retcode"], 0, out)
        self.assertEqual(set(out["data"]["results"]), set(monitor_api._restorable_sections()))
        self.assertTrue(all(r["ok"] for r in out["data"]["results"].values()), out)

    def test_bad_requests_are_rejected(self):
        self.assertNotEqual(self.post("/monitor/config-restore", {"kind": "nope"})["retcode"], 0)
        self.assertNotEqual(self.post("/monitor/config-restore",
                                      {"kind": "factory", "sections": ["diagnostic_rules"]})["retcode"], 0)

    def test_hints_say_factory_is_available(self):
        data = self.client.get("/monitor/config-hints").get_json()["data"]
        self.assertTrue(data["factory_available"])


class WithoutFactoryFileTests(_ApiCase):
    with_factory = False

    def test_factory_restore_is_refused(self):
        out = self.post("/monitor/config-restore", {"kind": "factory"})
        self.assertNotEqual(out["retcode"], 0)
        self.assertIn("installed package", out["retmsg"])

    def test_hints_say_factory_is_unavailable(self):
        data = self.client.get("/monitor/config-hints").get_json()["data"]
        self.assertFalse(data["factory_available"])

    def test_tuning_falls_back_to_the_values_this_run_started_with(self):
        data = self.client.get("/monitor/pressure-tuning/system").get_json()["data"]
        self.assertEqual(data["reference_source"], "startup")
        self.assertEqual(data["state"]["n"], 0)


if __name__ == "__main__":
    unittest.main()
