# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Day and week boundaries of /diag/digest (diagnostics/digest.py).

`period=day&date=YYYY-MM-DD` must cover that calendar day in the server's local
time zone. It used to cover the UTC day, so in UTC+8 "today's digest" ran from
08:00 to 08:00 and left out everything between midnight and 08:00.
"""

import os
import sys
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import digest  # noqa: E402


def _local(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset() to switch time zones")
class DigestWindowTest(unittest.TestCase):

    def setUp(self):
        self._saved_tz = os.environ.get("TZ")

    def tearDown(self):
        if self._saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved_tz
        time.tzset()

    def _use_tz(self, name):
        os.environ["TZ"] = name
        time.tzset()

    def test_day_is_the_local_calendar_day(self):
        self._use_tz("Asia/Shanghai")
        start, end = digest._daily_window("2026-11-12")
        self.assertEqual(_local(start), "2026-11-12 00:00")
        self.assertEqual(_local(end), "2026-11-13 00:00")
        # 2026-11-12 00:00 +0800 is 2026-11-11 16:00 UTC, not the UTC midnight it used to be.
        self.assertEqual(start, 1794412800)

    def test_day_accepts_date_and_datetime(self):
        self._use_tz("Asia/Shanghai")
        expected = digest._daily_window("2026-11-12")
        self.assertEqual(digest._daily_window(datetime(2026, 11, 12, 15, 30).date()), expected)
        self.assertEqual(digest._daily_window(datetime(2026, 11, 12, 15, 30)), expected)
        with self.assertRaises(ValueError):
            digest._daily_window("12/11/2026")
        with self.assertRaises(ValueError):
            digest._daily_window(20261112)

    def test_dst_days_still_end_at_midnight(self):
        self._use_tz("America/New_York")
        for day, next_day, hours in (("2026-03-08", "2026-03-09", 23),
                                     ("2026-11-01", "2026-11-02", 25)):
            start, end = digest._daily_window(day)
            self.assertEqual(_local(start), f"{day} 00:00")
            self.assertEqual(_local(end), f"{next_day} 00:00")
            self.assertEqual(end - start, hours * 3600)

    def test_utc_server_is_unchanged(self):
        self._use_tz("UTC")
        self.assertEqual(digest._daily_window("2026-11-12"), (1794441600, 1794528000))

    def test_week_and_its_comparison_are_whole_local_days(self):
        self._use_tz("Asia/Shanghai")
        start, end = digest._weekly_window("2026-11-12")
        self.assertEqual(_local(start), "2026-11-06 00:00")
        self.assertEqual(_local(end), "2026-11-13 00:00")

        generator = digest.DigestGenerator()
        with mock.patch.object(digest, "_retained_from", return_value=0), \
                mock.patch.object(generator, "_summary_for_window", return_value={}) as previous, \
                mock.patch.object(generator, "_generate", return_value=None) as generate:
            generator.generate_weekly("2026-11-12")
        prev_start, prev_end = previous.call_args[0]
        self.assertEqual(_local(prev_start), "2026-10-30 00:00")
        self.assertEqual(prev_end, start)
        self.assertEqual(generate.call_args[1]["start_time"], start)
        self.assertEqual(generate.call_args[1]["end_time"], end)

    def test_week_spanning_dst_change(self):
        self._use_tz("America/New_York")
        start, end = digest._weekly_window("2026-11-04")
        self.assertEqual(_local(start), "2026-10-29 00:00")
        self.assertEqual(_local(end), "2026-11-05 00:00")
        self.assertEqual(end - start, 7 * 86400 + 3600)


if __name__ == "__main__":
    unittest.main()
