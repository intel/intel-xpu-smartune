# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Model-list cache for the Benchmark tab.
#
# The list of benchmarkable models comes from HuggingFace, via search_models.py
# next to this file: it enumerates the OpenVINO org's converted repos and maps
# each back to its source model. That is minutes of network work
# across hundreds of repos, far too slow to do per request, so it runs in the
# background and the API serves the cached JSON it leaves behind. Because minutes
# is also far too slow to discover on first use, a stale cache is refreshed once
# at service startup (see maybe_prefetch) and the dashboard is told over SSE when
# the new list has landed.
#
# Unlike the run/setup jobs this is cheap and read-only, so it does NOT go through
# the single-slot job manager -- refreshing the model list while a benchmark runs
# is harmless. It has its own non-blocking lock purely to stop concurrent
# refreshes from stacking up.

import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from utils.logger import get_logger
logger = get_logger(__name__)

from benchmark.service import env, privilege

# A full search is long; this bounds a wedged search rather than reflecting an
# expected duration.
_SEARCH_TIMEOUT_SEC = 900

# On-demand enrichment of a single model is a few hub calls (config, params, one
# file-tree per downloadable variant); this bounds a stuck one, not a normal one.
_ENRICH_TIMEOUT_SEC = 120

# How old a cache may be before startup refreshes it, and whether to do so at all.
# Overridable per deployment (config.yaml `benchmark:`), because the right answer
# depends on whether the machine has internet at boot.
_DEFAULT_TTL_DAYS = 7

# Weight formats a cached entry can report as downloaded. Mirrors
# runner.VALID_PRECISIONS; kept as a tuple here to fix the display order.
_PRECISIONS = ("fp16", "int8", "int4")

_lock = threading.Lock()
_state = {
    "running": False,
    "last_error": None,      # stderr tail of the last failed refresh
    "last_finished": None,   # epoch seconds
}

# Set once a refresh has been attempted in this process, successfully or not.
# maybe_prefetch checks it so a failing network does not mean a retry on every
# call -- an explicit POST /bench/models/refresh is always still honoured.
_prefetch_attempted = False

# Serialises on-demand memory enrichment. search_models --enrich read-modify-writes
# the whole cache file, so two at once would let the later writer drop the earlier
# one's memory block; one at a time also bounds the outbound hub traffic a burst of
# opened model pages can kick off. Separate from _lock so a lazy enrich and a full
# refresh do not block each other -- their writes are both atomic swaps, and a
# refresh legitimately supersedes a just-enriched entry.
_enrich_lock = threading.Lock()


def cache_file():
    return env.paths()["cache"] / "models_cache.json"


def _run_search() -> bool:
    """Run search_models.py once. Returns False if a refresh was already in flight."""
    if not _lock.acquire(blocking=False):
        return False
    _state["running"] = True
    try:
        env.ensure_dirs()
        logger.info("Refreshing benchmark model list ...")
        result = subprocess.run(
            # --no-memory: list and source-map the models only. The per-model
            # weight/KV-cache footprint is the expensive part (a handful of hub
            # calls for each of ~180 models, minutes in total) and most of it is
            # never looked at, so it is deferred to ensure_memory() -- computed the
            # first time a given model's page is opened.
            [sys.executable, str(env.SEARCH_MODELS_SCRIPT), "--no-memory"],
            cwd=str(env.SRC_ROOT),
            env=env.build_subprocess_env(),
            capture_output=True, text=True, timeout=_SEARCH_TIMEOUT_SEC,
            # Unprivileged, like every other benchmark subprocess: it writes the
            # cache JSON into the runtime tree, and it reaches the network. The
            # `hf` calls it makes in turn inherit the drop, so search_models.py
            # itself needs nothing.
            **privilege.spawn_kwargs(),
        )
        if result.returncode == 0:
            _state["last_error"] = None
            logger.info("Benchmark model list refreshed.")
        else:
            # search_models.py logs progress to stderr, so keep the tail: the last
            # lines are where the actual failure is.
            _state["last_error"] = (result.stderr or "").strip()[-500:]
            logger.warning(f"Benchmark model search failed (exit {result.returncode}): "
                           f"{_state['last_error']}")
    except Exception as exc:
        _state["last_error"] = str(exc)
        logger.warning(f"Benchmark model search failed: {exc}")
    finally:
        _state["last_finished"] = time.time()
        # Released before the flag is cleared, so that "not running" always
        # implies the lock is free. The other order leaves a window where a
        # caller passes refresh_async's running check and starts a thread that
        # then fails to take the lock -- and the flag it set on the way in would
        # never be cleared, leaving the tab syncing forever.
        _lock.release()
        _state["running"] = False

    # Whether it worked or not, the tab is waiting on this: success means a new
    # list to show, failure means an error to report instead of a spinner that
    # never stops.
    _announce()
    return True


# Why a refresh cannot run right now. Reported to the dashboard verbatim, so it
# has to read as a sentence to a user rather than as a status code. There is only
# one such reason left; see refresh_async.
_BUSY_REASON = "A model list refresh is already running."


def refresh_async() -> Optional[str]:
    """Kick off a refresh in the background.

    Returns None when one was started, or the reason it was not. Being busy is
    now the only reason: the search no longer needs the benchmark environment
    (search_models.find_hf falls back to the hub's HTTP API), so browsing models
    works on a machine where nothing has been installed yet -- which is when
    someone is choosing what to install.
    """
    if _state["running"]:
        return _BUSY_REASON
    global _prefetch_attempted
    _prefetch_attempted = True
    # Marked running here rather than in the thread so that the announcement
    # below cannot race it: a refresh started by the service itself (startup
    # prefetch, or the one that follows an environment setup) is otherwise
    # invisible for minutes, and the tab goes on offering a Refresh button for
    # a search that is already underway.
    _state["running"] = True
    threading.Thread(target=_run_search, daemon=True,
                     name="bench-model-search").start()
    _announce()
    return None


def _announce() -> None:
    """Tell the dashboard the refresh state changed. Never raises.

    Imported here because events.py imports this module.
    """
    try:
        from benchmark.service import events
        events.publish_models()
    except Exception:
        logger.exception("Failed to announce the benchmark model list state")


# --- startup prefetch -----------------------------------------------------

def _cache_age_days() -> Optional[float]:
    """Age of the cached list in days, or None if there is no usable cache."""
    payload = _read_cache()
    if payload is None:
        return None
    stamp = payload.get("updated_at")
    if not stamp:
        # A cache with no timestamp cannot be aged out; treat it as fresh rather
        # than re-searching on every start.
        return 0.0
    try:
        written = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return 0.0
    if written.tzinfo is None:
        written = written.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - written).total_seconds() / 86400.0)


def freshness() -> Tuple[bool, Optional[float]]:
    """``(is_stale, age_in_days)``. No cache, or one older than
    ``models_cache_ttl_days``, is stale; a ttl of 0 disables ageing."""
    age = _cache_age_days()
    if age is None:
        return True, None
    ttl = float(env.setting("models_cache_ttl_days", _DEFAULT_TTL_DAYS) or 0)
    return (ttl > 0 and age >= ttl), age


def refresh_if_stale() -> Optional[str]:
    """Refresh only if the list is missing or aged out, returning the reason it
    was not. What the post-install hooks call: a full search is minutes of hub
    queries, which is the wrong thing to spend on a list built ten minutes ago.
    """
    stale, age = freshness()
    if not stale:
        return (f"the cached model list is {age:.1f} days old, within the "
                "configured lifetime")
    return refresh_async()


def maybe_prefetch() -> bool:
    """Refresh the model list at startup if it is missing or stale.

    Returns whether a refresh was started. Every reason not to is a normal
    outcome, not an error: this runs on every service start, including on
    machines with no benchmark environment and no route to huggingface.co.
    """
    global _prefetch_attempted
    if _prefetch_attempted or _state["running"]:
        return False
    if not env.setting("prefetch_models", True):
        logger.debug("Benchmark model prefetch disabled by configuration.")
        return False

    stale, age = freshness()
    if not stale:
        logger.debug(f"Benchmark model cache is {age:.1f} days old; not refreshing.")
        _prefetch_attempted = True
        return False

    logger.info("Refreshing the benchmark model list in the background "
                f"({'no cache yet' if age is None else f'{age:.1f} days old'}); "
                "this takes a few minutes.")
    return refresh_async() is None


# --- reading --------------------------------------------------------------

def _read_cache() -> Optional[dict]:
    path = cache_file()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Benchmark model cache unreadable ({path}): {exc}")
        return None
    return payload if isinstance(payload, dict) else None


def _normalize(entry) -> Optional[dict]:
    """One cache entry in the current shape, whatever shape it was written in.

    Version 1 stored bare model ids. Reading it here rather than forcing a
    refresh is what keeps the tab working on a deployment that upgrades with a
    cache already on disk: the entries simply have no metadata until the next
    search replaces them.
    """
    if isinstance(entry, str):
        return {"id": entry, "task": None, "downloads": 0, "likes": 0,
                "last_modified": None, "variants": []}
    if isinstance(entry, dict) and entry.get("id"):
        return {
            "id": entry["id"],
            "task": entry.get("task"),
            "downloads": entry.get("downloads") or 0,
            "likes": entry.get("likes") or 0,
            "last_modified": entry.get("last_modified"),
            "variants": [v for v in (entry.get("variants") or []) if isinstance(v, dict)],
            # Weight/KV-cache footprint enrichment (search_models.py build_memory).
            # Optional -- a v1 cache or a model whose config could not be read has
            # none -- so the dashboard treats its absence as "unknown", not zero.
            "memory": entry.get("memory"),
        }
    return None


def _model_safe_name(model_id: str) -> str:
    """Directory name the pipeline stores a model's artifacts under.

    Same rule as benchmark/scripts/generators/task_maps.py get_model_safe_name.
    Duplicated (one expression) rather than imported: that module lives in the
    vendor drop and pulls in the pipeline's global_vars, which resolves paths
    from a shell config this process has no reason to load.
    """
    return model_id.replace("/", "_").replace(".", "_")


def _dir_size(path: Path) -> int:
    """Bytes held by everything under ``path``.

    Per-file lstat rather than du: no subprocess, and a symlink is counted as the
    link it is instead of the file it points at, so nothing is counted twice. An
    entry that disappears mid-walk (a download being cleaned up) is skipped --
    this figure exists to tell a 400 MB directory from a 2 GB one, and being off
    by a file it could not read does not change that answer.
    """
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _downloaded_index() -> Dict[str, Dict[str, int]]:
    """Weight formats already on disk with their size, keyed by safe model name.

    build_download (benchmark/scripts/generators/gen_wrapper.py) downloads each
    conversion to <models>/<safe_name>/<format>_ov, so the directory layout is the
    record of what has been fetched. Read once per request and intersected with
    the model list, rather than stat-ing a path per model per precision.

    The size is taken on the same pass. It is what makes the list usable for the
    thing it is now also for -- deciding what to delete -- and one conversion is
    hundreds of megabytes to a couple of gigabytes, so which one to remove is not
    a question a count can answer. Walking every file of a full tree measures in
    tens of milliseconds (800-odd files for 24 GB), which is well inside what a
    list request can carry.
    """
    index: Dict[str, Dict[str, int]] = {}
    models_dir = env.paths()["models"]
    try:
        model_dirs = list(models_dir.iterdir())
    except OSError:
        return index
    for model_dir in model_dirs:
        if not model_dir.is_dir():
            continue
        formats: Dict[str, int] = {}
        try:
            entries = list(model_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith("_ov") or not entry.is_dir():
                continue
            # An interrupted download leaves the directory behind; an empty one
            # would otherwise read as "already have it" and the model could never
            # be re-fetched from the UI.
            try:
                if next(entry.iterdir(), None) is None:
                    continue
            except OSError:
                continue
            formats[entry.name[:-len("_ov")]] = _dir_size(entry)
        if formats:
            index[model_dir.name] = formats
    return index


def _decorate(model: dict, downloaded: Dict[str, Dict[str, int]]) -> dict:
    """Attach what this deployment knows about a model that HuggingFace does not."""
    have = downloaded.get(_model_safe_name(model["id"]), {})
    offered = {v.get("precision") for v in model["variants"] if v.get("precision")}
    model["local"] = {p: (p in have) for p in _PRECISIONS if p in have or p in offered}
    # Only what is actually there: a precision that is offered but not downloaded
    # has no size, and reporting it as 0 would read as "already here, costs
    # nothing" in a list whose point is what is taking up the disk.
    model["local_bytes"] = {p: have[p] for p in _PRECISIONS if p in have}
    model["precisions"] = [p for p in _PRECISIONS if p in offered]
    model["downloaded"] = bool(have)
    return model


def load_state() -> dict:
    """Cache status without the list itself -- small enough to push over SSE.

    The dashboard holds the whole list in memory and filters it locally, so an
    event only has to say that the list changed and how old it now is.
    """
    payload = _read_cache() or {}
    entries = payload.get("models") if isinstance(payload.get("models"), list) else []
    return {
        "total": len(entries),
        "version": payload.get("version", 1),
        "updated_at": payload.get("updated_at"),
        "refreshing": _state["running"],
        "last_error": _state["last_error"],
        "cached": bool(payload),
        "cache_file": str(cache_file()),
    }


def load(search: Optional[str] = None, limit: int = 0) -> dict:
    """Read the cached model list, optionally filtered.

    ``search``/``limit`` are kept for callers that want a slice; the dashboard
    asks for everything once per session and filters in the browser, which is why
    a keystroke no longer costs a request.
    """
    payload = _read_cache() or {}
    raw = payload.get("models") if isinstance(payload.get("models"), list) else []

    models: List[dict] = []
    for entry in raw:
        normalized = _normalize(entry)
        if normalized is not None:
            models.append(normalized)

    total = len(models)
    if search:
        needle = search.strip().lower()
        models = [m for m in models if needle in m["id"].lower()]
    matched = len(models)
    if limit and limit > 0:
        models = models[:limit]

    downloaded = _downloaded_index()
    models = [_decorate(m, downloaded) for m in models]

    state = load_state()
    state.update({
        "models": models,
        "count": len(models),
        "matched": matched,
        "total": total,
    })
    return state


# --- on-demand memory footprint -------------------------------------------
#
# The list is built with --no-memory (fast, no per-repo network). The weight /
# KV-cache / logits footprint for a given model is filled in the first time its
# page is opened, via search_models.py --enrich, and cached back into the same
# JSON so a second open is free.


def _memory_of(model_id: str) -> Optional[dict]:
    """The cached ``memory`` block for one model id, or None if it has none yet.

    "Has none yet" is any entry the enrich pass has not written a block onto:
    search_models.build_memory always returns a dict (at least ``{"params": ...}``)
    once run, so a dict here means enriched and its absence means deferred.
    """
    payload = _read_cache() or {}
    raw = payload.get("models") if isinstance(payload.get("models"), list) else []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("id") == model_id:
            mem = entry.get("memory")
            return mem if isinstance(mem, dict) else None
    return None


def ensure_memory(model_id: str) -> Optional[dict]:
    """Return one model's memory footprint, computing it on first request.

    Idempotent and cheap on repeat: an already-enriched entry is served straight
    from the cache with no subprocess and no network. Otherwise search_models.py
    --enrich fills it in (a few hub calls) and writes it back, and the fresh block
    is returned.

    None means the footprint is unavailable -- the model is not in the cache, or
    the enrich subprocess failed. That is the same "unknown" the dashboard already
    renders for a v1 cache, not an error to surface. A best-effort enrich that
    reached the hub but found little still writes a (sparse) block, so a transient
    failure is the one case that caches "unknown" until the next full refresh.
    """
    wanted = str(model_id or "").strip()
    if not wanted:
        return None

    existing = _memory_of(wanted)
    if existing is not None:
        return existing

    with _enrich_lock:
        # Re-check under the lock: a concurrent request for the same model may have
        # filled it in while this one waited, and enrichment is the expensive bit.
        existing = _memory_of(wanted)
        if existing is not None:
            return existing
        env.ensure_dirs()
        logger.info(f"Computing memory footprint for {wanted} ...")
        try:
            result = subprocess.run(
                [sys.executable, str(env.SEARCH_MODELS_SCRIPT), "--enrich", wanted],
                cwd=str(env.SRC_ROOT),
                env=env.build_subprocess_env(),
                capture_output=True, text=True, timeout=_ENRICH_TIMEOUT_SEC,
                # Unprivileged and network-facing, exactly like the full search.
                **privilege.spawn_kwargs(),
            )
        except Exception as exc:
            logger.warning(f"Memory footprint for {wanted} failed: {exc}")
            return None
        if result.returncode != 0:
            logger.warning(
                f"Memory footprint for {wanted} failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip()[-300:]}")
            return None

    return _memory_of(wanted)


# --- disk the next download would land on ---------------------------------


def disk_usage() -> dict:
    """Free space on the filesystem the downloaded weights land on.

    The pre-download check in the dashboard needs one number the browser cannot
    read for itself: how much room is left where the weights go. That is the
    models root -- gen_wrapper's `hf download --local-dir <models>/...` writes
    straight into it, and HF_HOME sits under the same runtime tree, so one
    filesystem answers for the whole fetch.

    Measured on whichever ancestor exists, so a runtime tree that has not been
    created yet still reports the volume it is going to be created on rather than
    nothing. `None` figures mean the filesystem could not be read at all; the
    dialog renders that as "unknown" and lets the download go ahead, the same way
    the memory check treats an unreadable device.
    """
    models_dir = env.paths()["models"]
    probe = models_dir
    # A path that does not exist yet is not a different volume: walk up to the
    # deepest parent that does, which is the one statvfs would be about anyway.
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        logger.warning(f"Could not read free space for {models_dir}: {exc}")
        return {
            "path": str(models_dir),
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
        }
    return {
        "path": str(models_dir),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


# --- deleting downloaded weights ------------------------------------------
#
# The one thing on this tab that takes real disk. A model is fetched once per
# precision into its own directory, each of them hundreds of megabytes to a
# couple of gigabytes, and nothing ever removed them -- a machine that has swept
# a handful of models is tens of gigabytes down with no way back short of an ssh
# session. So a precision can be given back, on its own: the three conversions of
# one model are separate downloads of very different sizes, and "keep int4, drop
# fp16" is the ordinary shape of the request.


def delete_local(model_id: str, precisions: Optional[Sequence[str]] = None) -> dict:
    """Remove downloaded weights for one model, by precision.

    ``precisions`` empty or None means every format on disk for it. Unknown
    formats are ignored rather than rejected: the caller is a browser holding a
    list that may be a moment out of date, and the useful answer to "delete the
    int4 that is no longer there" is that it is gone.

    Nothing the caller sends is joined onto a path. The directory is derived from
    the model id through _model_safe_name, exactly as the download does, and each
    candidate is confined under the models root before it is touched -- so a
    model id of "../../etc" resolves outside, matches nothing, and removes
    nothing.
    """
    wanted_model = str(model_id or "").strip()
    empty = {"model": wanted_model, "removed": [], "freed_bytes": 0, "skipped": []}
    if not wanted_model:
        return empty

    root = env.paths()["models"].resolve()
    model_dir = root / _model_safe_name(wanted_model)
    try:
        resolved_model = model_dir.resolve()
        resolved_model.relative_to(root)
    except (OSError, ValueError):
        logger.warning(f"Rejected a model path outside {root}: {model_id}")
        return empty
    if not resolved_model.is_dir():
        return empty

    on_disk = _downloaded_index().get(resolved_model.name, {})
    asked = [str(p).strip() for p in (precisions or []) if str(p).strip()]
    targets = [p for p in _PRECISIONS if p in on_disk and (not asked or p in asked)]

    removed: List[str] = []
    skipped: List[str] = []
    freed = 0
    for precision in targets:
        path = resolved_model / f"{precision}_ov"
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            logger.warning(f"Rejected a weights path outside {root}: {path}")
            skipped.append(precision)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning(f"Could not remove {path}: {exc}")
            skipped.append(precision)
            continue
        removed.append(precision)
        # Measured before the removal, by the index read above -- there is
        # nothing left to size afterwards.
        freed += on_disk.get(precision, 0)

    # The model's own directory goes with its last precision, so a model that has
    # been fully cleaned up leaves nothing behind that a later read would report
    # as still downloaded. Anything else in there (a stray log, a half-finished
    # download) keeps it: this removes weights, not whatever else was put there.
    if removed:
        try:
            if next(resolved_model.iterdir(), None) is None:
                resolved_model.rmdir()
        except OSError as exc:
            logger.debug(f"Left {resolved_model} in place: {exc}")

    if removed:
        logger.info(
            f"Deleted {', '.join(removed)} weights for {wanted_model} "
            f"({freed / float(1 << 30):.2f} GiB)"
        )
    return {
        "model": wanted_model,
        "removed": removed,
        "freed_bytes": freed,
        "skipped": skipped,
    }
