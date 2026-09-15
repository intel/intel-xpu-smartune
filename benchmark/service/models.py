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
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Set

from utils.logger import logger

from benchmark.service import env, privilege

# A full search is long; this bounds a wedged search rather than reflecting an
# expected duration.
_SEARCH_TIMEOUT_SEC = 900

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
            [sys.executable, str(env.SEARCH_MODELS_SCRIPT)],
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

    age = _cache_age_days()
    ttl = float(env.setting("models_cache_ttl_days", _DEFAULT_TTL_DAYS) or 0)
    if age is not None and ttl > 0 and age < ttl:
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


def _downloaded_index() -> Dict[str, Set[str]]:
    """Weight formats already on disk, keyed by safe model name.

    build_download (benchmark/scripts/generators/gen_wrapper.py) downloads each
    conversion to <models>/<safe_name>/<format>_ov, so the directory layout is the
    record of what has been fetched. Read once per request and intersected with
    the model list, rather than stat-ing a path per model per precision.
    """
    index: Dict[str, Set[str]] = {}
    models_dir = env.paths()["models"]
    try:
        model_dirs = list(models_dir.iterdir())
    except OSError:
        return index
    for model_dir in model_dirs:
        if not model_dir.is_dir():
            continue
        formats = set()
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
            formats.add(entry.name[:-len("_ov")])
        if formats:
            index[model_dir.name] = formats
    return index


def _decorate(model: dict, downloaded: Dict[str, Set[str]]) -> dict:
    """Attach what this deployment knows about a model that HuggingFace does not."""
    have = downloaded.get(_model_safe_name(model["id"]), set())
    offered = {v.get("precision") for v in model["variants"] if v.get("precision")}
    model["local"] = {p: (p in have) for p in _PRECISIONS if p in have or p in offered}
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
