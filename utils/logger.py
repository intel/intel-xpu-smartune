# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import logging
import os
from datetime import datetime

class Logger:
    def __init__(self, log_file=None, log_level=logging.INFO):
        """
        Initialize the logger.

        :param log_file: Path to the log file. If None, output goes to the console only.
        :param log_level: Logging level; defaults to logging.INFO.
        """
        self.log_file = log_file
        self.log_level = log_level
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level)

        # Prevent duplicate records when the module is imported through multiple
        # paths or when Logger is instantiated more than once: wipe any handlers
        # previously attached to this named logger, and stop propagation to the
        # root logger so external libraries' handlers don't re-emit our records.
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        self.logger.propagate = False

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            file_handler.stream.reconfigure(encoding="utf-8")
            self.logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.stream.reconfigure(encoding="utf-8")
        self.logger.addHandler(console_handler)

    def get_logger(self):
        return self.logger

    def info(self, message):
        self.logger.info(message)

    def debug(self, message):
        self.logger.debug(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)

# Log directory. Defaults to <repo>/logs; SMARTUNE_LOG_DIR overrides it so a
# packaged install (or a test) can redirect logs elsewhere without code changes.
LOG_DIR = os.environ.get("SMARTUNE_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)
LOG_PREFIX = "multi_tasks"
# A fresh timestamped log file is created on every process start (below), i.e.
# one per service start/restart, so LOG_DIR would otherwise grow without bound.
# Keep only the most recent LOG_RETENTION run logs and prune the rest at startup.
LOG_RETENTION = 10


def prune_old_logs(log_dir, prefix=LOG_PREFIX, keep=LOG_RETENTION):
    """Delete all but the ``keep`` newest ``<prefix>_<timestamp>.log`` files.

    Called at import so every process start trims the backlog left by earlier
    runs. Best-effort: it never raises, so a cleanup hiccup cannot stop the
    service from starting. The ``<prefix>_latest.log`` symlink and any
    non-regular files are left untouched. Files are ranked by name which, given
    the fixed ``YYYYMMDD_HHMMSS`` stamp, is chronological — so pruning does not
    depend on mtimes that a copy or ``touch`` could perturb. ``keep < 0`` keeps
    everything (pruning disabled); ``keep == 0`` removes all matching files.
    """
    if keep < 0:
        return
    try:
        names = os.listdir(log_dir)
    except OSError:
        return
    latest = f"{prefix}_latest.log"
    candidates = []
    for name in names:
        if name == latest:
            continue
        if not (name.startswith(prefix + "_") and name.endswith(".log")):
            continue
        path = os.path.join(log_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        candidates.append(path)
    candidates.sort()  # lexical == chronological for the fixed-width timestamp
    for path in (candidates[:-keep] if keep else candidates):
        try:
            os.remove(path)
        except OSError:
            pass


_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file_path = os.path.join(LOG_DIR, f"{LOG_PREFIX}_{_timestamp}.log")
logger = Logger(log_file=log_file_path, log_level=logging.DEBUG).get_logger()

# Also maintain a stable "latest" symlink so log-tailing scripts don't need to
# guess the timestamped filename.  Best-effort: ignore errors on filesystems
# that don't support symlinks.
try:
    latest_link = os.path.join(LOG_DIR, f"{LOG_PREFIX}_latest.log")
    if os.path.islink(latest_link) or os.path.exists(latest_link):
        os.remove(latest_link)
    os.symlink(os.path.basename(log_file_path), latest_link)
except OSError:
    pass

# Trim the backlog now that this run's file exists, so it is counted among the
# retained set and never pruned out from under the handler writing to it.
prune_old_logs(LOG_DIR)


# Test the logger
def test_logger():
    logger.info("This is an info message.")
    logger.debug("This is a debug message.")
    logger.error("This is an error message.")
    logger.critical("This is a critical message.")

if __name__ == "__main__":
    test_logger()
