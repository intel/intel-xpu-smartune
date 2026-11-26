# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune` log source: reads back SmartTune's own structured JSON application
# log (logs/multi_tasks_*.log written by utils/logger.py). This is the single
# store for application logs -- not journald. Read-only: it parses the JSON
# lines into uniform LogRecords.
#
# query() serves time-windowed views; scan_forward() reads only what was appended
# since saved per-file positions.

import json
import os
import stat
import zlib
from datetime import datetime

from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSource, level_rank
from utils.logger import LOG_DIR, LOG_PREFIX, get_logger

logger = get_logger(__name__)

# Bytes one forward scan reads across all files; a large new file takes several scans.
_SCAN_MAX_BYTES = 8 * 1024 * 1024
# The logger writes levels with Python's standard names, as `"level": "ERROR"`.
_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_KEY = b'"level": "'
# How many leading bytes fingerprint a file (see _head_crc).
_HEAD_BYTES = 256


def _level_needles(min_level):
    """Byte patterns one of which a line at or above ``min_level`` contains, so
    lines below it are skipped without parsing their JSON. None: parse all."""
    if not min_level:
        return None
    floor = level_rank(min_level)
    return tuple(_LEVEL_KEY + name.encode() + b'"' for name in _LEVEL_NAMES
                 if level_rank(name) >= floor)


class SmartuneScan:
    """Result of one forward read: records oldest first, and the positions to
    save once some or all of them are processed."""

    __slots__ = ("records", "complete", "_files", "_ends")

    def __init__(self, files, items, complete):
        items.sort(key=lambda item: (item[0].ts_epoch, item[1], item[2]))
        self.records = [item[0] for item in items]
        self._ends = [(item[1], item[2]) for item in items]
        # name -> (inode, offset before this read, offset after it, first bytes)
        self._files = files
        self.complete = complete  # False: unread bytes remain

    def positions(self, processed=None):
        """Positions once the first ``processed`` records are done (all by default). A file
        with an unprocessed record resumes right after its last processed one."""
        done, pending = {}, set()
        if processed is not None:
            for index, (name, end_offset) in enumerate(self._ends):
                if index < processed:
                    done[name] = end_offset
                else:
                    pending.add(name)
        out = {}
        for name, (inode, start, end, head) in self._files.items():
            offset = done.get(name, start) if name in pending else end
            out[name] = [inode, offset, _head_crc(head, offset)]
        return out


def _head_crc(head, offset):
    """CRC of a file's first bytes up to ``offset``; a change means the name holds another
    file, even if its inode number was reused."""
    return zlib.crc32(head[:min(offset, _HEAD_BYTES)])


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

    def scan_forward(self, positions=None, *, min_level=None, since_epoch=None,
                     max_records=500, max_bytes=_SCAN_MAX_BYTES):
        """Read the whole lines appended to each run log since ``positions`` (from
        :meth:`SmartuneScan.positions`); a new, replaced or truncated file is read from the
        start. ``since_epoch`` skips unpositioned files last written before it and older
        records. Reads at most ``max_records`` records and ``max_bytes`` bytes. Never raises."""
        positions = positions or {}
        needles = _level_needles(min_level)
        flt = LogQueryFilter(min_level=min_level)
        files, items = {}, []
        budget = max_bytes
        complete = True
        for path in reversed(self._log_files_newest_first()):  # oldest first
            name = os.path.basename(path)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                continue  # gone, or a symlink
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    continue
                head = os.pread(fd, _HEAD_BYTES, 0)
                offset = self._resume_offset(positions.get(name), info, head)
                if offset is None:
                    offset = (info.st_size if since_epoch is not None
                              and info.st_mtime < since_epoch else 0)
                new_offset = offset
                if offset < info.st_size:
                    if budget <= 0 or len(items) >= max_records:
                        complete = False
                    else:
                        new_offset, read_all = self._read_lines(
                            fd, name, offset, budget, needles, flt,
                            since_epoch, items, max_records, line_limit=max_bytes)
                        budget -= new_offset - offset
                        complete = complete and read_all
                        if len(head) < _HEAD_BYTES:
                            head = os.pread(fd, _HEAD_BYTES, 0)
                files[name] = (info.st_ino, offset, new_offset, head)
            except OSError as exc:
                logger.warning("SmartTune log source skipped %s: %s", path, exc)
            finally:
                os.close(fd)
        return SmartuneScan(files, items, complete)

    @staticmethod
    def _resume_offset(saved, info, head):
        """The saved offset when it still belongs to this file, else None."""
        try:
            inode, offset, crc = saved
        except (TypeError, ValueError):
            return None
        if inode != info.st_ino or offset > info.st_size or _head_crc(head, offset) != crc:
            return None
        return offset

    def _read_lines(self, fd, name, offset, budget, needles, flt, since_epoch,
                    items, max_records, *, line_limit=None):
        """Parse whole lines from ``offset`` (at most ``budget`` bytes) into ``items``, up to
        ``max_records``; a line longer than ``line_limit`` (default ``budget``) is skipped. Returns
        ``(offset after the last line consumed, whether the file was read to its end)``."""
        line_limit = budget if line_limit is None else max(line_limit, budget)
        chunk = os.pread(fd, budget + 1, offset)
        over_budget = len(chunk) > budget
        chunk = chunk[:budget]
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            if over_budget and line_limit > budget:
                # The budget left by earlier files is too small: this line waits for the next scan.
                probe = os.pread(fd, line_limit + 1, offset)
                if b"\n" in probe[:line_limit] or len(probe) <= line_limit:
                    return offset, False
                chunk = probe[:line_limit]
            if over_budget:
                # A line longer than a whole scan's budget is no record of ours: step over it.
                logger.warning("SmartTune log source skipped %d bytes without a "
                               "line end in %s", len(chunk), name)
                return offset + len(chunk), False
            return offset, True  # a line still being written
        line_start = offset
        for raw in chunk[:last_newline].split(b"\n"):
            line_end = line_start + len(raw) + 1
            rec = self._parse_line(raw, needles, flt)
            if rec is not None and (since_epoch is None or rec.ts_epoch >= since_epoch):
                rec.fields["file"] = name
                rec.fields["offset"] = line_start
                items.append((rec, name, line_end))
                if len(items) >= max_records:
                    return line_end, False
            line_start = line_end
        # A partial last line is a write in progress, not unread backlog.
        return offset + last_newline + 1, not over_budget

    def _parse_line(self, raw, needles, flt):
        raw = raw.strip()
        if not raw:
            return None
        if needles is not None and _LEVEL_KEY in raw and not any(n in raw for n in needles):
            return None  # below the level floor: skip without parsing
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return None  # a torn line, or not one of ours
        if not isinstance(obj, dict):
            return None
        rec = self._to_record(obj)
        return rec if flt.accepts(rec) else None

    def query(self, flt: LogQueryFilter):
        records = []
        # Each file covers [start stamp, mtime], so skip only files wholly outside
        # the window. Files are written concurrently (the long-running service keeps
        # appending to its file while one-off processes open newer ones), so file
        # order says nothing about where an older file ends: every overlapping file
        # is read, and the limit is applied after the merge.
        for path in self._log_files_newest_first():
            file_start = self._file_start_epoch(path)
            if file_start is not None and flt.end_time is not None and file_start > flt.end_time:
                continue
            if flt.start_time is not None:
                try:
                    if os.path.getmtime(path) < flt.start_time:
                        continue
                except OSError:
                    continue
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
                logger.warning("SmartTune log source skipped %s: %s", path, exc)
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
