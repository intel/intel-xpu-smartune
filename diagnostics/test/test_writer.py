# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Background event writer: inline until started, ordered on its own thread,
drops (and counts) work when the queue stays full."""

import contextvars
import os
import queue
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics import writer  # noqa: E402

_VAR = contextvars.ContextVar("test_writer_var", default=None)


class WriterTest(unittest.TestCase):

    def _use(self, work, thread):
        """Point the module at a writer owned by this test (never the shared one)."""
        for name, value in (("_queue", work), ("_thread", thread)):
            patcher = mock.patch.object(writer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _background(self, capacity=100):
        work = queue.Queue(maxsize=capacity)
        thread = threading.Thread(target=writer._drain, args=(work,), daemon=True)
        thread.start()
        self._use(work, thread)
        return thread

    def test_runs_inline_while_not_started(self):
        self._use(None, None)
        seen = []
        self.assertTrue(writer.submit(lambda: seen.append(threading.get_ident())))
        self.assertEqual(seen, [threading.get_ident()])
        self.assertEqual(writer.stats()["mode"], "inline")

    def test_background_work_keeps_order_and_context(self):
        thread = self._background()
        seen = []
        _VAR.set("job-1")
        for i in range(20):
            writer.submit(lambda i=i: seen.append((i, _VAR.get(), threading.get_ident())))
        self.assertTrue(writer.flush(5))
        self.assertEqual([i for i, _, _ in seen], list(range(20)))
        self.assertEqual({(var, ident) for _, var, ident in seen}, {("job-1", thread.ident)})

    def test_full_queue_drops_and_counts(self):
        release = threading.Event()
        blocker = threading.Thread(target=release.wait, daemon=True)
        blocker.start()
        self.addCleanup(release.set)
        self._use(queue.Queue(maxsize=1), blocker)  # alive, but never drains
        dropped_before = writer.stats()["dropped"]
        self.assertTrue(writer.submit(lambda: None))
        self.assertFalse(writer.submit(lambda: None))
        stats = writer.stats()
        self.assertEqual((stats["mode"], stats["queued"], stats["dropped"] - dropped_before),
                         ("background", 1, 1))

    def test_a_failing_job_does_not_stop_the_writer(self):
        self._background()
        seen = []
        writer.submit(lambda: 1 / 0)
        writer.submit(lambda: seen.append("after"))
        self.assertTrue(writer.flush(5))
        self.assertEqual(seen, ["after"])


if __name__ == "__main__":
    unittest.main()
