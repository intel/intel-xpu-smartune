# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Startup reconcile of interrupted limit lifecycles, against a scratch ledger
(conftest points SMARTUNE_DB_PATH at a temporary file)."""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import control_lifecycle, emit_event, event_store  # noqa: E402

BOOT = "a" * 32
OTHER_BOOT = "b" * 32


def _control(event_type, protection_id, resource):
    emit_event(event_type, severity="info", category="platform.control", source="balancer",
               app_id="app1", protection_id=protection_id, resource_type=resource,
               summary=event_type, attributes={"boot_id": BOOT})


def _lifecycle(protection_id):
    return control_lifecycle.summarize(event_store.events_for_protections([protection_id]))[0]


class ReconcileTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        event_store.ensure_tables()

    def _partially_failed(self):
        protection_id = uuid.uuid4().hex
        _control("CONTROL_CPU_LIMIT_APPLIED", protection_id, "cpu")
        _control("CONTROL_MEMORY_LIMIT_APPLIED", protection_id, "memory")
        _control("CONTROL_MEMORY_LIMIT_FAILED", protection_id, "memory")
        lifecycle = _lifecycle(protection_id)
        self.assertEqual((lifecycle["status"], lifecycle["active_resources"]), ("failed", ["cpu"]))
        return protection_id

    def test_partial_failure_needs_verification_after_restart(self):
        protection_id = self._partially_failed()
        marked = control_lifecycle.reconcile_interrupted_lifecycles(current_boot_id=BOOT)
        self.assertIn(protection_id, marked)
        self.assertEqual(_lifecycle(protection_id)["status"], "requires_verification")
        self.assertTrue(control_lifecycle.clear_without_runtime_state(protection_id))
        self.assertNotIn(protection_id, event_store.unresolved_protection_ids())

    def test_partial_failure_is_cleared_by_reboot(self):
        protection_id = self._partially_failed()
        control_lifecycle.reconcile_interrupted_lifecycles(current_boot_id=OTHER_BOOT)
        self.assertEqual(_lifecycle(protection_id)["status"], "cleared_by_reboot")
        self.assertNotIn(protection_id, event_store.unresolved_protection_ids())

    def test_fully_failed_lifecycle_is_left_alone(self):
        protection_id = uuid.uuid4().hex
        _control("CONTROL_CPU_LIMIT_APPLIED", protection_id, "cpu")
        _control("CONTROL_CPU_LIMIT_FAILED", protection_id, "cpu")
        marked = control_lifecycle.reconcile_interrupted_lifecycles(current_boot_id=BOOT)
        self.assertNotIn(protection_id, marked)
        self.assertFalse(control_lifecycle.clear_without_runtime_state(protection_id))


if __name__ == "__main__":
    unittest.main()
