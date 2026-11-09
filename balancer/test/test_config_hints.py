# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Settings field guidance (config/field_hints.py) and the
GET /monitor/config-hints endpoint.

Guards two things: a hint key must still point at a real form field (a renamed
config key would otherwise silently lose its guidance), and the startup defaults
must stay the value the backend started with once settings are edited.

Run:  python3 balancer/test/test_config_hints.py
"""

import copy
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config.field_hints import FIELD_HINTS, SECTION_HINTS
from monitor import monitor_api

# What the Settings forms edit, flattened to the paths the hint keys use.  The network
# pressure form flattens rx_drop_weight into its own field, so mirror that here.
_FORM_VIEWS = {
    "system_pressure": {
        "regular_update_sys_pressure_time": 5,
        "thresholds": {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 1.0},
        "weights": {"cpu": 1, "memory": 7, "io": 2},
        "mem_gate_steepness": 8,
        "memory_busy_threshold": 80,
        "cpu_busy_threshold": 90,
    },
    "disk_pressure": {
        "disk_thresholds": {"low": 0.4, "medium": 0.6, "high": 0.8, "critical": 1.0},
        "sub_weights": {"latency": 0.55, "queue": 0.35, "util": 0.1},
        "sigmoid_k": 8.0,
        "max_p_weight": 0.8,
    },
    "network_pressure": {
        "network_thresholds": {"low": 0.3, "medium": 0.5, "high": 0.7, "critical": 0.9},
        "network_pressure_model_rx_drop_weight": 0.3,
    },
    "limit_policy": {
        "cpu": {"rate": {p: 0.5 for p in ("high", "medium", "low", "undefined")}},
        "memory": {"rate": {p: 0.5 for p in ("high", "medium", "low", "undefined")}},
        "disk_io": {
            "rate": {
                p: {k: 1 for k in ("write", "read", "write_iops", "read_iops")}
                for p in ("high", "medium", "low", "undefined")
            },
            "media_scale": {m: 1.0 for m in ("nvme", "sata_ssd", "mmc", "hdd", "usb", "unknown")},
            "candidate_floor": {
                m: {"mb_s": 1, "iops": 1}
                for m in ("nvme", "sata_ssd", "mmc", "hdd", "usb", "unknown")
            },
        },
    },
    "network_control": {
        "network_pps_policy": {"app_pps_high": 1, "pps_cap_rate": 1, "pps_cap_burst": 1},
        "config_network_bw": {
            p: {"min": 0, "max": 0} for p in ("critical", "high", "low")
        },
    },
}


def _resolves(view, parts):
    """True if the dotted path (with "*" = any one key) reaches a leaf in ``view``."""
    if not parts:
        return not isinstance(view, dict)
    if not isinstance(view, dict):
        return False
    head, rest = parts[0], parts[1:]
    keys = view.keys() if head == "*" else ([head] if head in view else [])
    return any(_resolves(view[k], rest) for k in keys)


class FieldHintsTest(unittest.TestCase):
    def test_every_key_points_at_a_form_field(self):
        for key in FIELD_HINTS:
            section, _, path = key.partition(".")
            with self.subTest(key=key):
                self.assertIn(section, _FORM_VIEWS)
                self.assertTrue(_resolves(_FORM_VIEWS[section], path.split(".")), path)

    def test_numeric_hints_are_well_formed(self):
        for key, hint in FIELD_HINTS.items():
            with self.subTest(key=key):
                self.assertIn(hint["kind"], ("number", "preset"))
                self.assertTrue(hint["up"] and hint["down"])
                self.assertGreater(hint["step"], 0)
                if "min" in hint and "max" in hint:
                    self.assertLess(hint["min"], hint["max"])

    def test_scalar_presets_sit_inside_the_legal_range(self):
        for key, hint in FIELD_HINTS.items():
            if hint["kind"] != "preset":
                continue
            for preset in hint["presets"]:
                with self.subTest(key=key, preset=preset["label"]):
                    self.assertGreaterEqual(preset["value"], hint.get("min", float("-inf")))
                    self.assertLessEqual(preset["value"], hint.get("max", float("inf")))

    def test_pps_cap_step_does_not_overshoot_the_threshold_step(self):
        # The cap may not exceed the threshold, so stepping them differently would make
        # "raise both by one step" fail validation.
        base = "network_control.network_pps_policy."
        self.assertEqual(FIELD_HINTS[base + "app_pps_high"]["step"], FIELD_HINTS[base + "pps_cap_rate"]["step"])

    def test_sections_cover_the_config_specs(self):
        self.assertEqual(set(SECTION_HINTS), set(monitor_api._CONFIG_SPECS))


class StartupDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.live = {"regular_update_sys_pressure_time": 5, "weights": {"cpu": 1}}
        specs = {
            name: {
                "get": (lambda: copy.deepcopy(self.live)) if name == "system_pressure" else (lambda: {}),
                "validate": lambda body: dict(body),
                "write": lambda updates: self.live.update(updates) or True,
            }
            for name in monitor_api._CONFIG_SPECS
        }
        patches = [
            mock.patch.dict(monitor_api._CONFIG_SPECS, specs, clear=True),
            mock.patch.dict(monitor_api._startup_defaults, {}, clear=True),
            # Keep the optimistic-concurrency stamp off the real runtime_state.json.
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

    def test_hints_endpoint_shape(self):
        data = self.client.get("/monitor/config-hints").get_json()["data"]
        self.assertEqual(set(data), {"fields", "sections", "startup_defaults"})
        self.assertEqual(data["startup_defaults"]["system_pressure"]["regular_update_sys_pressure_time"], 5)
        self.assertNotIn("diagnostic_rules", data["startup_defaults"])

    def test_snapshot_survives_a_write(self):
        self.client.get("/monitor/config-hints")
        resp = self.client.post("/monitor/config/system_pressure",
                                json={"regular_update_sys_pressure_time": 30, "expected_updated_at": 0})
        self.assertEqual(self.live["regular_update_sys_pressure_time"], 30, resp.get_json())
        data = self.client.get("/monitor/config-hints").get_json()["data"]
        self.assertEqual(data["startup_defaults"]["system_pressure"]["regular_update_sys_pressure_time"], 5)

    def test_snapshot_is_taken_before_the_first_write_even_if_hints_were_never_fetched(self):
        self.client.post("/monitor/config/system_pressure",
                         json={"regular_update_sys_pressure_time": 30, "expected_updated_at": 0})
        self.assertEqual(self.live["regular_update_sys_pressure_time"], 30)
        data = self.client.get("/monitor/config-hints").get_json()["data"]
        self.assertEqual(data["startup_defaults"]["system_pressure"]["regular_update_sys_pressure_time"], 5)


if __name__ == "__main__":
    unittest.main()
