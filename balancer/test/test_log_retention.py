# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run-log pruning leaves logs that another process still writes."""

import fcntl
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils import logger as log_module  # noqa: E402


def _touch(directory, stamp):
    path = os.path.join(directory, f"multi_tasks_{stamp}.log")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}\n")
    return path


class PruneOldLogsTest(unittest.TestCase):

    def test_keeps_only_the_newest_files(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [_touch(directory, f"20260101_00000{i}") for i in range(5)]
            log_module.prune_old_logs(directory, keep=2)
            self.assertEqual([os.path.exists(p) for p in paths], [False, False, False, True, True])

    def test_file_locked_by_a_running_writer_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            service_log = _touch(directory, "20260101_000000")
            newer = [_touch(directory, f"20260102_00000{i}") for i in range(3)]
            with open(service_log, "a", encoding="utf-8") as writer:
                fcntl.flock(writer.fileno(), fcntl.LOCK_SH)
                log_module.prune_old_logs(directory, keep=2)
                self.assertTrue(os.path.exists(service_log))
            self.assertFalse(os.path.exists(newer[0]))
            self.assertTrue(os.path.exists(newer[1]) and os.path.exists(newer[2]))

    def test_file_that_cannot_be_opened_is_kept(self):
        if os.geteuid() == 0:
            self.skipTest("root opens any file")
        with tempfile.TemporaryDirectory() as directory:
            foreign = _touch(directory, "20260101_000000")
            os.chmod(foreign, 0)
            for i in range(3):
                _touch(directory, f"20260102_00000{i}")
            log_module.prune_old_logs(directory, keep=1)
            self.assertTrue(os.path.exists(foreign))

    def test_file_handler_holds_a_shared_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "multi_tasks_20260101_000000.log")
            handler = log_module._NoFollowFileHandler(path)
            try:
                log_module._hold_log_lock(handler)
                self.assertTrue(log_module._log_file_in_use(path))
            finally:
                handler.close()
            self.assertFalse(log_module._log_file_in_use(path))


if __name__ == "__main__":
    unittest.main()
