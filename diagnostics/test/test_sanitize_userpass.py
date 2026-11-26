# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""-u / --user: a user:password pair is redacted, a numeric uid:gid is not."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from diagnostics.sanitize import _REDACTED, scrub_text  # noqa: E402


class UserPassTest(unittest.TestCase):

    def test_password_is_redacted(self):
        for text in ("curl -u alice:s3cret https://x", "curl --user alice:s3cret https://x",
                     "curl -u 1000:s3cret https://x"):
            with self.subTest(text=text):
                out = scrub_text(text)
                self.assertNotIn("s3cret", out)
                self.assertIn(_REDACTED, out)

    def test_numeric_uid_gid_is_kept(self):
        for text in ("docker run -u 1000:1000 image", "podman run --user 0:0 image"):
            with self.subTest(text=text):
                self.assertEqual(scrub_text(text), text)


if __name__ == "__main__":
    unittest.main()
