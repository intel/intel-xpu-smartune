# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `benchmark` log source: reads a benchmark run's own artifact log
# (runs/run_<id>.log) so event context can pull the raw job output. Unlike the
# smartune source these lines are plain stdout/stderr, so ts is coarse (file
# mtime) and level is heuristic -- enough to "view related logs for this job",
# with structured KPIs handled separately by metrics.py.
#
# Guarded/optional: the benchmark toolchain is not in every deployment, so all
# imports are lazy and available() reports False when it is absent.

import collections
import dataclasses
import os

from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSource
from utils.logger import get_logger

logger = get_logger(__name__)

_ERROR_HINTS = ("traceback", "error", "exception", "fatal", "exit code", "failed", "core dumped")
_WARN_HINTS = ("warning", "warn")


def _runs_dir():
    """Locate the benchmark runs directory, or None if benchmark is absent."""
    try:
        from benchmark.service import env
        return env.paths().get("runs")
    except Exception:
        return None


def _heuristic_level(line):
    low = line.lower()
    if any(h in low for h in _ERROR_HINTS):
        return "ERROR"
    if any(h in low for h in _WARN_HINTS):
        return "WARNING"
    return "INFO"


class BenchmarkLogSource(LogSource):
    name = "benchmark"

    def available(self) -> bool:
        runs = _runs_dir()
        return bool(runs and os.path.isdir(runs))

    def _log_files(self, job_id=None):
        """Run log files, newest first. When ``job_id`` is given, prefer the file
        whose name embeds it; else return all recent run logs."""
        runs = _runs_dir()
        if not runs or not os.path.isdir(runs):
            return []
        try:
            entries = [os.path.join(runs, n) for n in os.listdir(runs)
                       if n.startswith("run_") and n.endswith(".log")]
        except OSError:
            return []
        if job_id:
            try:
                from diagnostics.metrics import run_name_for_job
                run_name = run_name_for_job(job_id)
            except Exception:
                run_name = None
            matched = [p for p in entries if run_name and run_name in os.path.basename(p)]
            if matched:
                entries = matched
            else:
                return []
        entries.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                     reverse=True)
        return entries

    def query(self, flt: LogQueryFilter):
        # Without a job/run scope this source stays quiet: benchmark output is
        # only meaningful in the context of a specific run, and dumping every run
        # log into a generic query would drown the smartune stream.
        if not flt.job_id and not flt.allow_unscoped:
            return []
        # Every line carries the file mtime, so a job-scoped query drops the time window.
        line_filter = (dataclasses.replace(flt, start_time=None, end_time=None)
                       if flt.job_id else flt)
        records = []
        for path in self._log_files(job_id=flt.job_id):
            room = flt.limit - len(records)
            if room <= 0:
                break
            # Keep a long log's last lines: they hold the run's outcome.
            tail = collections.deque(maxlen=room)
            try:
                mtime = os.path.getmtime(path)
                run_name = os.path.basename(path)
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for idx, line in enumerate(fh):
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        rec = LogRecord(
                            ts_epoch=mtime,
                            ts_iso="",
                            level=_heuristic_level(line),
                            source="benchmark",
                            logger=run_name,
                            message=line,
                            job_id=flt.job_id,
                            fields={"run_log": run_name, "line": idx},
                        )
                        if line_filter.accepts(rec):
                            tail.append(rec)
            except OSError as exc:
                logger.debug("benchmark source skip %s: %s", path, exc)
            records.extend(tail)
        return records
