# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Keep test runs away from the live service's state. utils.logger creates its
# run log and db.DatabaseModel opens its SQLite file when first imported, so
# both locations are redirected here, before any test module is collected.

import atexit
import os
import shutil
import tempfile

_scratch = tempfile.mkdtemp(prefix="smartune-test-")
os.environ["SMARTUNE_LOG_DIR"] = os.path.join(_scratch, "logs")
os.environ["SMARTUNE_DB_PATH"] = os.path.join(_scratch, "test.db")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
