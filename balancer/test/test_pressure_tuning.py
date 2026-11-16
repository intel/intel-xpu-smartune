# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for the relax / tighten stepping of the pressure models (monitor/pressure_tuning.py).

Run:  python3 balancer/test/test_pressure_tuning.py
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config.config import Config
from monitor import pressure_tuning as pt

_CONFIG_YAML = os.path.join(_REPO_ROOT, "config", "config.yaml")


def _factory_values():
    cfg = Config.from_file(_CONFIG_YAML)
    paths = [p for group in pt.GROUPS for p in pt.tuned_paths(group)]
    return pt.flatten(cfg, paths)


class ShippedConfigTests(unittest.TestCase):
    """The step tables must stay in step with config.yaml."""

    def test_every_tuned_path_exists_in_config_yaml(self):
        # The YAML patcher only rewrites existing lines, so a tuned key that is missing from
        # config.yaml would change in memory and silently never reach the disk.
        values = _factory_values()
        for group in pt.GROUPS:
            for path in pt.tuned_paths(group):
                self.assertIn(path, values, f"{path} missing (or not numeric) in config.yaml")

    def test_factory_values_sit_inside_their_bounds(self):
        values = _factory_values()
        for group, params in pt.GROUPS.items():
            for p in params:
                tuned = pt._tuned(p, values)
                lo, hi = pt._bounds(p, pt._tuned(p, values))
                if lo is not None:
                    self.assertGreaterEqual(tuned, lo - 1e-12, p.path)
                if hi is not None:
                    self.assertLessEqual(tuned, hi + 1e-12, p.path)

    def test_factory_is_position_zero(self):
        values = _factory_values()
        for group in pt.GROUPS:
            st = pt.state(group, values, values)
            self.assertEqual(st["n"], 0, group)

    def test_sub_weights_never_exceed_one(self):
        values = _factory_values()
        current = dict(values)
        for _ in range(30):
            current.update(pt.preview("disk", values, current, pt.TIGHTEN)["values"])
        total = sum(current[f"disk_pressure_model.sub_weights.{k}"]
                    for k in ("latency", "queue", "util"))
        self.assertLessEqual(total, 1.0 + 1e-9)


class SteppingTests(unittest.TestCase):
    def setUp(self):
        self.factory = _factory_values()

    def _apply(self, group, current, direction):
        result = pt.preview(group, self.factory, current, direction)
        current = dict(current)
        current.update(result["values"])
        return current, result

    def test_relax_then_tighten_returns_to_factory(self):
        for group in pt.GROUPS:
            current = {p: self.factory[p] for p in pt.tuned_paths(group)}
            for _ in range(3):
                current, _ = self._apply(group, current, pt.RELAX)
            for _ in range(3):
                current, _ = self._apply(group, current, pt.TIGHTEN)
            for path, v in current.items():
                self.assertAlmostEqual(v, self.factory[path], delta=abs(self.factory[path]) * 2e-3,
                                       msg=f"{group}:{path}")

    def test_counter_walks_back_one_per_click_after_parameters_hit_bounds(self):
        # Regression: after enough relaxing some parameters sit at their bounds; stepping
        # back used to leave them a step behind, so the group read "Custom" instead of n-1.
        for group in pt.GROUPS:
            current = {p: self.factory[p] for p in pt.tuned_paths(group)}
            seen = []
            for _ in range(7):
                current, result = self._apply(group, current, pt.RELAX)
                seen.append(result["state"]["n"])
            self.assertEqual(seen, [1, 2, 3, 4, 5, 6, 7], group)
            for expected in (6, 5, 4, 3, 2, 1, 0, -1):
                current, result = self._apply(group, current, pt.TIGHTEN)
                self.assertEqual(result["state"]["n"], expected, group)

    def test_relax_direction_matches_each_parameter(self):
        for group, params in pt.GROUPS.items():
            current = {p.path: self.factory[p.path] for p in params}
            current, _ = self._apply(group, current, pt.RELAX)
            for p in params:
                before, after = self.factory[p.path], current[p.path]
                if p.path == "mem_gate_steepness":
                    continue
                self.assertEqual(after > before, p.up_is_relax, p.path)

    def test_headroom_never_reaches_ceiling(self):
        current = {"memory_busy_threshold": 80.0}
        factory = {"memory_busy_threshold": 80.0}
        for _ in range(200):
            result = pt.preview("system", factory, current, pt.RELAX)
            current.update(result["values"])
        self.assertLess(current["memory_busy_threshold"], 97.0 + 1e-9)

    def test_bound_stops_the_parameter_and_says_why(self):
        current = {"memory_busy_threshold": 97.0}
        factory = {"memory_busy_threshold": 80.0}
        result = pt.preview("system", factory, current, pt.RELAX)
        self.assertNotIn("memory_busy_threshold", result["values"])
        self.assertTrue(any("upper limit" in n and "Memory busy" in n for n in result["notes"]))

    def test_hand_edit_beyond_bound_is_not_pulled_back(self):
        current = {"memory_busy_threshold": 99.0}
        factory = {"memory_busy_threshold": 80.0}
        result = pt.preview("system", factory, current, pt.RELAX)
        self.assertNotIn("memory_busy_threshold", result["values"])
        result = pt.preview("system", factory, current, pt.TIGHTEN)
        # Moving back is allowed and starts from where the user left it.
        self.assertLess(result["values"]["memory_busy_threshold"], 99.0)

    def test_steps_continue_from_a_hand_edited_value(self):
        factory = {"disk_pressure_model.sigmoid_k": 8.0}
        current = {"disk_pressure_model.sigmoid_k": 10.0}
        result = pt.preview("disk", factory, current, pt.RELAX)
        self.assertAlmostEqual(result["values"]["disk_pressure_model.sigmoid_k"], 11.5, places=2)

    def test_steepness_follows_memory_busy_threshold(self):
        # k / free is the tuned quantity, so moving the busy threshold carries k along and
        # keeps the width of the medium band; relax then tighten must still round-trip.
        factory = {"memory_busy_threshold": 80.0, "mem_gate_steepness": 8.0}
        current = dict(factory)
        for _ in range(3):
            current.update(pt.preview("system", factory, current, pt.RELAX)["values"])
        free = 100 - current["memory_busy_threshold"]
        self.assertLessEqual(current["mem_gate_steepness"], 0.65 * free + 1e-9)
        self.assertGreaterEqual(current["mem_gate_steepness"], 0.12 * free - 1e-9)
        for _ in range(3):
            current.update(pt.preview("system", factory, current, pt.TIGHTEN)["values"])
        self.assertAlmostEqual(current["mem_gate_steepness"], 8.0, delta=0.05)
        self.assertAlmostEqual(current["memory_busy_threshold"], 80.0, delta=0.1)

    def test_steepness_stops_at_the_medium_band_limit(self):
        factory = {"memory_busy_threshold": 80.0, "mem_gate_steepness": 8.0}
        current = {"memory_busy_threshold": 80.0, "mem_gate_steepness": 13.0}  # 0.65 * 20
        result = pt.preview("system", factory, current, pt.RELAX)
        self.assertNotIn("mem_gate_steepness", result["values"])
        self.assertTrue(any("steepness" in n.lower() and "upper limit" in n for n in result["notes"]))

    def test_state_reports_n_even_after_a_hand_edit(self):
        group = "system"
        current = {p: self.factory[p] for p in pt.tuned_paths(group)}
        for _ in range(2):
            current, result = self._apply(group, current, pt.RELAX)
        self.assertEqual(result["state"]["n"], 2)
        edited = dict(current)
        edited["cpu_busy_threshold"] = 75.0
        # No "custom" state: the counter stays an integer position, only approximate now.
        self.assertIsInstance(pt.state(group, self.factory, edited)["n"], int)

    def test_buttons_disable_only_when_every_parameter_is_at_its_limit(self):
        group = "system"
        current = {p: self.factory[p] for p in pt.tuned_paths(group)}
        for _ in range(100):
            current, result = self._apply(group, current, pt.RELAX)
            if not result["state"]["can_relax"]:
                break
        self.assertFalse(result["state"]["can_relax"])
        self.assertTrue(result["state"]["can_tighten"])

    def test_unknown_group_and_direction_are_rejected(self):
        with self.assertRaises(ValueError):
            pt.preview("cpu", {}, {}, pt.RELAX)
        with self.assertRaises(ValueError):
            pt.preview("system", {}, {}, "sideways")


if __name__ == "__main__":
    unittest.main()
