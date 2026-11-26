# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Unified log-query façade. ONE /diag/logs interface that federates the request
# across the registered sources and merges the results by time. This is a
# READ-ONLY translator -- it never stores or indexes anything; SmartTune's own
# JSON files, journald and benchmark artifacts each stay the sole store of their
# data. Sensitive values are redacted before anything is returned.

import dataclasses

from diagnostics import sanitize
from diagnostics.sources import LogQueryFilter, get_source, iter_sources, source_names
from diagnostics.sources.base import LogSourceError
from utils.logger import get_logger

logger = get_logger(__name__)


def query(*, sources=None, start_time=None, end_time=None, level=None,
          job_id=None, boot_id=None, keyword=None, kernel_only=True, limit=500):
    """Federate a log query across sources and return a merged, redacted result.

    ``sources`` is an optional list of source names (default: all registered).
    ``boot_id`` scopes boot-aware sources (journal) to one boot session; sources
    without a boot concept ignore it.
    ``kernel_only`` (default True) restricts journald to the kernel ring buffer --
    the host/hardware fault evidence Diagnostics wants; pass False for a full
    journal view. Non-journald sources ignore it.
    Returns ``{"records": [...], "sources": [...], "failed_sources": [...],
    "truncated": bool}``; ``failed_sources`` could not be read. A ``job_id``
    without both time bounds takes the missing ones from the job's events.
    """
    if job_id and (start_time is None or end_time is None):
        from diagnostics.context import job_window

        window = job_window(job_id)
        if window is not None:
            start_time = window[0] if start_time is None else start_time
            end_time = window[1] if end_time is None else end_time
    flt = LogQueryFilter(
        start_time=start_time, end_time=end_time, min_level=level,
        job_id=job_id, boot_id=boot_id, keyword=keyword, kernel_only=kernel_only,
        limit=max(1, min(int(limit or 500), 5000)),
    )

    targets = []
    if sources:
        for name in sources:
            src = get_source(name)
            if src is not None:
                targets.append(src)
    else:
        targets = iter_sources()

    # One record more than needed tells whether a source had more to give.
    probe = dataclasses.replace(flt, limit=flt.limit + 1)
    per_source = []
    queried = []
    failed = []
    truncated = False
    for src in targets:
        name = getattr(src, "name", "?")
        try:
            if not src.available():
                continue
            recs = src.query(probe) or []
        except LogSourceError as exc:
            failed.append(name)
            logger.warning("log source %r could not be read: %s", name, exc)
            continue
        except Exception as exc:
            failed.append(name)
            logger.warning("log source %r query failed: %s", name, exc)
            continue
        truncated = truncated or bool(getattr(recs, "truncated", False)) or len(recs) > flt.limit
        per_source.append(list(recs))
        queried.append(name)

    truncated = truncated or sum(len(recs) for recs in per_source) > flt.limit
    merged = _cap_fairly(per_source, flt.limit)
    # Newest first across all sources.
    merged.sort(key=_recency, reverse=True)

    records = [_scrub_record(r.to_dict()) for r in merged]
    return {
        "records": records,
        "sources": queried,
        "failed_sources": failed,
        "available_sources": source_names(),
        "truncated": truncated,
        "count": len(records),
    }


def _recency(rec):
    """Sort key: time, then line number (benchmark run log lines all carry the file mtime)."""
    line = rec.fields.get("line") if isinstance(rec.fields, dict) else None
    return (rec.ts_epoch, line if isinstance(line, int) else 0)


def _cap_fairly(per_source, limit):
    """The newest ``limit`` records, each source keeping at least an equal share (or all it has).

    A plain newest-first cut would let benchmark lines (all stamped with their
    log's mtime) crowd out the other sources."""
    if not per_source:
        return []
    ranked = [sorted(recs, key=_recency, reverse=True) for recs in per_source]
    if sum(len(recs) for recs in ranked) <= limit:
        return [rec for recs in ranked for rec in recs]
    share = limit // len(ranked)
    kept = [recs[:share] for recs in ranked]
    rest = sorted((rec for recs in ranked for rec in recs[share:]),
                  key=_recency, reverse=True)
    room = limit - sum(len(recs) for recs in kept)
    return [rec for recs in kept for rec in recs] + rest[:room]


def list_boots(limit=15):
    """Boot sessions offered by boot-aware sources. Aggregates every source that
    implements list_boots(); in practice only ``journal`` does today. Returns
    ``{"boots": [...], "sources": [...]}`` (empty when journald absent)."""
    boots = []
    queried = []
    for src in iter_sources():
        try:
            if not src.available():
                continue
            src_boots = src.list_boots(limit=limit) if hasattr(src, "list_boots") else []
            if src_boots:
                for boot in src_boots:
                    boot.setdefault("source", src.name)
                boots.extend(src_boots)
                queried.append(src.name)
        except Exception as exc:
            logger.warning("list_boots on %r failed: %s", getattr(src, "name", "?"), exc)
    return {"boots": boots, "sources": queried}


def _scrub_record(d):
    """Redact the free-text message and any string extras before return."""
    d["message"] = sanitize.scrub_text(d.get("message"))
    if isinstance(d.get("fields"), dict):
        d["fields"] = sanitize.scrub(d["fields"])
    return d
