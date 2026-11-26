# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Built-in journal rules end to end: records in, events out."""

import collections
import os
import sys
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kernel_samples import MCE_CORRECTED, MCE_REPLAYED_FATAL, kernel_records, user_record  # noqa: E402

from diagnostics.detectors import kernel_signatures, rules  # noqa: E402

_OOMD_PRESSURE = ("Killed /user.slice/user-1000.slice/user@1000.service/app.slice/"
                  "app-gnome-firefox-4242.scope due to memory pressure for "
                  "/user.slice/user-1000.slice/user@1000.service being 72.34% > 50.00% for > 20s "
                  "with reclaim activity")
_OOMD_SWAP = ("Killed /system.slice/ollama.service due to memory used (15000000000) / total "
              "(16000000000) and swap used (3900000000) / total (4000000000) being more than 90.00%")
_UNCLEAN = ("File /var/log/journal/{}/system.journal corrupted or uncleanly shut down, "
            "renaming and replacing.")


def _from(logger, message, boot="boot-r", mono=1.0, **fields):
    return replace(user_record(message, mono, boot=boot, **fields), logger=logger)


class JournalRulesTest(unittest.TestCase):

    def setUp(self):
        self.events = []
        for name, value in (("_journal_seen", rules._BoundedSeen(1000)),
                            ("_gate_verdicts", collections.OrderedDict()),
                            ("_journal_gate", kernel_signatures.SignatureGate())):
            patcher = mock.patch.object(rules, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(rules, "emit_event", side_effect=self._emit),
                        mock.patch.object(rules.custom_rules, "configured_rules", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _emit(self, event_type, **kwargs):
        self.events.append((event_type, kwargs))
        return f"event-{len(self.events)}"

    def _run(self, records):
        emitted, complete = rules._process_journal_records(records)
        self.assertTrue(complete)
        return [(event_type, kwargs) for event_type, kwargs in self.events]

    def test_unclean_shutdown_is_one_event_per_boot(self):
        events = self._run([
            _from("systemd-journald", _UNCLEAN.format("m1"), mono=1.0),
            _from("systemd-journald", _UNCLEAN.format("m1").replace("system", "user-1000"), mono=2.0),
        ])
        self.assertEqual([e for e, _ in events], ["PLATFORM_HOST_UNCLEAN_SHUTDOWN"] * 2)
        # The ledger keys events on (source, boot, identity, type): the second is a duplicate.
        self.assertEqual({(kw["identity"], kw["boot_id"]) for _, kw in events},
                         {("unclean-shutdown", "boot-r")})
        self.assertEqual(events[0][1]["severity"], "error")

    def test_other_journald_lines_are_ignored(self):
        self.assertEqual(self._run([_from("systemd-journald", "Journal started")]), [])

    def test_systemd_oomd_kills(self):
        events = self._run([_from("systemd-oomd", _OOMD_PRESSURE, mono=1.0),
                            _from("systemd-oomd", _OOMD_SWAP, mono=2.0)])
        self.assertEqual([e for e, _ in events], ["RESOURCE_MEMORY_OOM_KILL"] * 2)
        pressure, swap = (kw["attributes"] for _, kw in events)
        self.assertEqual((pressure["process"], pressure["killer"], pressure["reason"]),
                         ("app-gnome-firefox-4242.scope", "systemd-oomd", "memory pressure"))
        self.assertEqual((swap["process"], swap["reason"]), ("ollama.service", "swap exhausted"))

    def test_oomd_wording_from_another_program_is_ignored(self):
        self.assertEqual(self._run([_from("myapp", _OOMD_PRESSURE)]), [])

    def test_service_crash(self):
        events = self._run([_from("systemd", "ollama.service: Main process exited, code=dumped, "
                                             "status=6/ABRT", unit="ollama.service")])
        [(event_type, kwargs)] = events
        self.assertEqual((event_type, kwargs["service"], kwargs["attributes"]["signal"]),
                         ("PLATFORM_PROCESS_CRASHED", "ollama.service", "SIGABRT"))

    def test_kernel_report_is_classified_once(self):
        replay = MCE_CORRECTED + "\n" + "\n".join(MCE_REPLAYED_FATAL.split("\n")[1:])
        events = self._run(kernel_records(replay, boot="boot-k"))
        self.assertEqual([e for e, _ in events], ["DEVICE_HARDWARE_ERROR_UNCORRECTED"])

    def test_builtin_scan_reads_journald_and_oomd(self):
        matches = rules._journal_matches()
        for identifier in ("systemd-journald", "systemd-oomd", "systemd", "systemd-coredump"):
            self.assertIn(f"SYSLOG_IDENTIFIER={identifier}", matches)


if __name__ == "__main__":
    unittest.main()
