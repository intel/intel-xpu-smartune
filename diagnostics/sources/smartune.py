# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune` log source: reads back SmartTune's own structured JSON application
# log (logs/multi_tasks_*.log written by utils/logger.py). This is the single
# store for application logs -- not journald. Read-only: it parses the JSON
# lines into uniform LogRecords.

import json
import os
from datetime import datetime

from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSource
from utils.logger import LOG_DIR, LOG_PREFIX, get_logger

logger = get_logger(__name__)


def _parse_epoch(ts_iso):
    """Parse the logger's timezone-aware ISO 8601 ts into epoch seconds."""
    if not ts_iso:
        return 0.0
    try:
        return datetime.fromisoformat(ts_iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


class SmartuneLogSource(LogSource):
    name = "smartune"

    def __init__(self, log_dir=None, prefix=LOG_PREFIX):
        self._log_dir = log_dir or LOG_DIR
        self._prefix = prefix

    def available(self) -> bool:
        return os.path.isdir(self._log_dir)

    def _log_files_newest_first(self):
        """The timestamped run logs, newest first. Skips the `latest` symlink so
        each physical file is read once."""
        try:
            names = os.listdir(self._log_dir)
        except OSError:
            return []
        latest = f"{self._prefix}_latest.log"
        files = [
            os.path.join(self._log_dir, n) for n in names
            if n.startswith(self._prefix + "_") and n.endswith(".log") and n != latest
        ]
        # Fixed-width YYYYMMDD_HHMMSS stamp -> lexical == chronological.
        files.sort(reverse=True)
        return files

    def _file_start_epoch(self, path):
        """Local epoch parsed from the run-log filename stamp, or None. A file only
        ever holds records at or after its start time (the logger appends), so this
        bounds the file's coverage without opening it."""
        base = os.path.basename(path)
        stamp = base[len(self._prefix) + 1:-len(".log")]
        try:
            return datetime.strptime(stamp, "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            return None

    def query(self, flt: LogQueryFilter):
        records = []
        # Files are newest-first and each holds only records >= its start stamp, so
        # a narrow recent window need not read the whole history: skip files that
        # start after the window, and stop once a file starts at/before the window's
        # lower bound (every older file is then entirely before the window too).
        for path in self._log_files_newest_first():
            if len(records) >= flt.limit:
                break
            file_start = self._file_start_epoch(path)
            past_window = False
            if file_start is not None:
                if flt.end_time is not None and file_start > flt.end_time:
                    continue
                past_window = flt.start_time is not None and file_start <= flt.start_time
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue  # tolerate a torn/partial line
                        rec = self._to_record(obj)
                        if flt.accepts(rec):
                            records.append(rec)
            except OSError as exc:
                logger.debug("smartune source skip %s: %s", path, exc)
            if past_window:
                break  # this file straddled the lower bound; older files are before it
        # Newest first, capped.
        records.sort(key=lambda r: r.ts_epoch, reverse=True)
        return records[:flt.limit]

    @staticmethod
    def _to_record(obj):
        ts_iso = obj.get("ts", "")
        msg = obj.get("msg", "")
        exc = obj.get("exc")
        if exc:
            msg = f"{msg}\n{exc}" if msg else exc
        known = {"ts", "level", "service", "logger", "pid", "thread", "msg",
                 "exc", "app_id", "job_id"}
        extras = {k: v for k, v in obj.items() if k not in known}
        return LogRecord(
            ts_epoch=_parse_epoch(ts_iso),
            ts_iso=ts_iso,
            level=obj.get("level", "INFO"),
            source="smartune",
            logger=obj.get("logger", ""),
            message=msg,
            service=obj.get("service"),
            app_id=obj.get("app_id"),
            job_id=obj.get("job_id"),
            fields={"pid": obj.get("pid"), "thread": obj.get("thread"), **extras},
        )
