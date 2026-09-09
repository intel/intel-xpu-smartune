# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Unified log-query façade. ONE /diag/logs interface that federates the request
# across the registered sources and merges the results by time. This is a
# READ-ONLY translator -- it never stores or indexes anything; SmartTune's own
# JSON files, journald and benchmark artifacts each stay the sole store of their
# data. Sensitive values are redacted before anything is returned.

from diagnostics import sanitize
from diagnostics.sources import LogQueryFilter, get_source, iter_sources, source_names
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
    Returns ``{"records": [...], "sources": [...], "truncated": bool}``.
    """
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

    merged = []
    queried = []
    for src in targets:
        try:
            if not src.available():
                continue
            recs = src.query(flt) or []
            merged.extend(recs)
            queried.append(src.name)
        except Exception as exc:
            logger.warning("log source %r query failed: %s", getattr(src, "name", "?"), exc)

    # Newest first across all sources, then cap the merged set.
    merged.sort(key=lambda r: r.ts_epoch, reverse=True)
    truncated = len(merged) > flt.limit
    merged = merged[:flt.limit]

    records = [_scrub_record(r.to_dict()) for r in merged]
    return {
        "records": records,
        "sources": queried,
        "available_sources": source_names(),
        "truncated": truncated,
        "count": len(records),
    }


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
