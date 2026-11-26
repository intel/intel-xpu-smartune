# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Every event carries its dashboard domain; SmarTune log events use a fixed category."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import alerts, emit_event, error_signatures, event_catalog, event_store  # noqa: E402
from diagnostics.detectors import rules  # noqa: E402
from diagnostics.sources.base import LogRecord  # noqa: E402


class DomainOfTest(unittest.TestCase):

    def test_every_catalog_domain_is_a_dashboard_domain(self):
        for entry in event_catalog.entries():
            self.assertIn(entry["domain"], event_catalog.DOMAINS, entry["event_type"])

    def test_kernel_faults_are_host_system(self):
        for event_type in ("PLATFORM_KERNEL_PANIC", "PLATFORM_KERNEL_OOPS", "PLATFORM_KERNEL_LOCKUP",
                           "PLATFORM_KERNEL_HUNG_TASK", "PLATFORM_KERNEL_WARNING"):
            self.assertEqual(event_catalog.domain_of(event_type), "hardware", event_type)

    def test_custom_rule_domain_wins(self):
        self.assertEqual(event_catalog.domain_of("CUSTOM_X", {"domain": "compute"}), "compute")

    def test_unknown_type_has_no_domain(self):
        self.assertIsNone(event_catalog.domain_of("NOT_A_TYPE", {"domain": "nonsense"}))

    def test_stored_event_reports_its_domain(self):
        event_store.ensure_tables()
        event = emit_event("DEVICE_STORAGE_IO_ERROR", severity="error", category="device.storage",
                           source="detector", summary="I/O error")
        [stored] = event_store.query_events(event_id=event["event_id"])
        self.assertEqual(stored["domain"], "data-path")


class ServiceLogCategoryTest(unittest.TestCase):

    def test_error_log_event_names_its_service_separately(self):
        rec = LogRecord(ts_epoch=100.0, ts_iso="", level="ERROR", source="smartune",
                        logger="balancer.balancer", message="boom", service="balancer")
        with mock.patch.object(rules, "emit_event", return_value={"event_id": "x"}) as emit:
            rules._emit_for_record(rec)
        kwargs = emit.call_args.kwargs
        self.assertEqual((kwargs["category"], kwargs["service"]), ("platform.service_log", "balancer"))

    def test_new_error_alert_stays_scoped_by_its_logger(self):
        rec = LogRecord(ts_epoch=100.0, ts_iso="", level="ERROR", source="smartune",
                        logger="db.DatabaseModel", message="boom", service="balancer")
        finding = SimpleNamespace(event_type=error_signatures.NEW, fingerprint="f1", source="smartune",
                                  origin="db.DatabaseModel", template="boom", sample="boom",
                                  attributes={}, ts_epoch=100.0)
        with mock.patch.object(rules, "emit_event", return_value={"event_id": "x"}) as emit:
            rules._emit_error_signature(finding, rec, "boom")
        event = {"event_type": emit.call_args.args[0], **emit.call_args.kwargs}
        self.assertEqual(event["category"], "platform.service_log")
        self.assertEqual(alerts.scope_for(event), "smartune:db.DatabaseModel")


if __name__ == "__main__":
    unittest.main()
