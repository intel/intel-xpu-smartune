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

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_PREFIX = "multi_tasks"
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


# Test the logger
def test_logger():
    logger.info("This is an info message.")
    logger.debug("This is a debug message.")
    logger.error("This is an error message.")
    logger.critical("This is a critical message.")

if __name__ == "__main__":
    test_logger()
