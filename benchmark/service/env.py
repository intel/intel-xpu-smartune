# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Path and environment resolution for the vendored benchmark toolchain, plus the
# "install the Python environment" action the dashboard exposes.
#
# Everything the pipeline reads about where things live comes from
# benchmark/configs/global_vars.sh, which derives all of its paths from
# DIR_ENV_ROOT. This module is the single place that decides what DIR_ENV_ROOT
# is (via SMARTUNE_BENCH_ENV_ROOT) and what credentials/proxies the subprocesses
# see -- so the vendored tree needs no per-deployment edits, and no secret is ever
# written into a script on disk.

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from config.config import b_config
from utils.logger import logger

from benchmark.service import jobs

# <repo>/benchmark/service/env.py -> <repo>/benchmark -> <repo>
PKG_ROOT = Path(__file__).resolve().parent
# The read-only vendor drop that this package sits inside.
SRC_ROOT = PKG_ROOT.parent
REPO_ROOT = SRC_ROOT.parent
SETUP_SCRIPT = SRC_ROOT / "setup_env.sh"
# Multi-version environment builder. `bootstrap` builds the huggingface-only
# base venv (what setup runs); `ensure <OV>` builds one OpenVINO column on demand
# at benchmark time (run_template.sh). The old `use <OV>` switch is no longer
# driven from the dashboard -- the version is chosen per-run on the Models tab.
BUILD_SCRIPT = SRC_ROOT / "uv_build_envs.sh"
RUN_TEMPLATE = SRC_ROOT / "templates" / "run_template.sh"
# Ours, not the vendor's: the upstream copy lived in benchmark/webui/ alongside a
# web UI that SmarTune replaces with the dashboard's Benchmark tab, so it moved
# in here with the rest of the SmarTune-side code.
SEARCH_MODELS_SCRIPT = PKG_ROOT / "search_models.py"

# Everything generated at runtime lives here, never inside the vendor drop.
DEFAULT_ENV_ROOT = SRC_ROOT / "runtime"

# Created up front so the UI can show real paths (and the user can drop models in
# by hand) before setup_env.sh has ever run. setup_env.sh creates the same set
# from global_vars.sh; mkdir is idempotent so the two agree.
_RUNTIME_SUBDIRS = (
    "models", "benchmarks", "scripts",
)


class SetupAlreadyDone(RuntimeError):
    """Raised when setup is requested but a usable venv already exists and the
    caller did not ask to rebuild it."""

    def __init__(self, status: dict):
        super().__init__("a usable benchmark environment already exists")
        self.status = status


def _cfg() -> dict:
    """The `benchmark:` block from config.yaml (absent -> empty)."""
    cfg = getattr(b_config, "benchmark", None)
    return cfg if isinstance(cfg, dict) else {}


def setting(name: str, default=None):
    """One key of the `benchmark:` block, for the other modules in this package.

    Read on each call rather than captured at import: config.yaml is re-read at
    runtime, and a value cached here would outlive an edit to it.
    """
    value = _cfg().get(name)
    return default if value is None else value


def enabled() -> bool:
    """Whether the benchmark feature is switched on AND its source tree is present.

    The tree check matters for trimmed deployments (and for the monitor-only deb,
    which does not ship benchmark/): with no run template there is nothing to
    expose, so the blueprint is not registered and the dashboard hides the tab.
    """
    if not _cfg().get("enabled", True):
        return False
    return RUN_TEMPLATE.is_file()


def env_root() -> Path:
    """Runtime root (DIR_ENV_ROOT for the vendored scripts)."""
    configured = str(_cfg().get("env_root") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_ENV_ROOT


def paths() -> Dict[str, Path]:
    root = env_root()
    return {
        "env_root": root,
        "src_root": SRC_ROOT,
        "venv": root / ".gen/multi_env/venv",
        # The pool of pre-built per-OpenVINO venvs the `venv` symlink is switched
        # between (uv_build_envs.sh's OV_POOL); one ov_<version>/ dir per build.
        "ov_pool": root / ".gen/multi_env/ov_pool",
        "models": root / "models",
        "logs": root / ".gen/logs",
        "benchmarks": root / "benchmarks",
        "runs": root / ".gen/runs",
        "cache": root / ".gen/cache",
        "genai": root / ".gen/genai",
    }


def ensure_dirs() -> None:
    """Create the runtime tree. Safe to call on every request."""
    root = env_root()
    for name in _RUNTIME_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)


def build_subprocess_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for every benchmark subprocess.

    This is the only place the HF token and the proxy enter the pipeline: they are
    passed through the environment so they never land in a rendered run script,
    which is world-readable under the runtime tree.
    """
    cfg = _cfg()
    p = paths()
    env = os.environ.copy()

    env["SMARTUNE_BENCH_ENV_ROOT"] = str(p["env_root"])
    env["BENCH_SRC_ROOT"] = str(SRC_ROOT)
    # search_models.py writes its caches here instead of into the vendor tree.
    env["BENCH_MODELS_CACHE_DIR"] = str(p["cache"])
    # Point subprocesses at the benchmark venv: search_models.py's find_hf() reads
    # PYENV_VENV_DIR to locate the `hf` CLI, and prepending the venv's bin to PATH
    # keeps that (and any other venv tool) resolvable. The service itself runs from
    # a different interpreter, so without this the `hf` CLI is not found and the
    # model-list refresh falls back to querying the hub over HTTP.
    env["PYENV_VENV_DIR"] = str(p["venv"])
    env["PATH"] = f"{p['venv'] / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    # Unbuffered child output, so the dashboard's log tail is close to live.
    env["PYTHONUNBUFFERED"] = "1"

    token = str(cfg.get("hf_token") or "").strip() or os.environ.get("HF_TOKEN", "")
    if token:
        env["HF_TOKEN"] = token
    else:
        env.pop("HF_TOKEN", None)

    endpoint = str(cfg.get("hf_endpoint") or "").strip()
    if endpoint:
        env["HF_ENDPOINT"] = endpoint

    profile = str(cfg.get("network_profile") or "").strip()
    if profile:
        env["NETWORK_PROFILE"] = profile

    # Proxy config is site-specific and lives in config.yaml. Setting it here for
    # all four spellings keeps wget/curl/requests consistent; leaving it unset
    # means the service's own environment decides (usually: direct).
    http_proxy = str(cfg.get("http_proxy") or "").strip()
    https_proxy = str(cfg.get("https_proxy") or "").strip() or http_proxy
    if http_proxy:
        env["http_proxy"] = env["HTTP_PROXY"] = http_proxy
    if https_proxy:
        env["https_proxy"] = env["HTTPS_PROXY"] = https_proxy
    no_proxy = str(cfg.get("no_proxy") or "").strip()
    if no_proxy:
        env["no_proxy"] = env["NO_PROXY"] = no_proxy

    if extra:
        env.update(extra)
    return env


def _venv_python(venv_dir: Path) -> Optional[Path]:
    python = venv_dir / "bin" / "python"
    return python if python.is_file() and os.access(python, os.X_OK) else None


# --- OpenVINO version selection -------------------------------------------
#
# uv_build_envs.sh builds one complete venv per OpenVINO version under
# ov_pool/ov_<version>/ and points the active `venv` symlink at one of them.
# The dashboard lists the built versions and switches between them by running
# `uv_build_envs.sh use <version>`, which just relinks -- no rebuild.


def available_ov_versions() -> list:
    """OpenVINO versions with a usable pre-built venv, newest first.

    Enumerated from the on-disk pool rather than the script's OV_VERSIONS array:
    a version is only selectable once its venv actually exists, and a half-built
    or removed one must not be offered.
    """
    pool = paths()["ov_pool"]
    versions = []
    try:
        for entry in pool.iterdir():
            if not entry.is_dir() or not entry.name.startswith("ov_"):
                continue
            if _venv_python(entry / "venv") is not None:
                versions.append(entry.name[len("ov_"):])
    except OSError:
        return []
    # Descending so the highest release sorts first; version parts compared
    # numerically ("2026.10.0" after "2026.3.0", not before it).
    def key(v: str) -> tuple:
        return tuple(int(p) if p.isdigit() else p for p in v.split("."))

    return sorted(versions, key=key, reverse=True)


def active_ov_version() -> Optional[str]:
    """The OpenVINO version the active `venv` symlink currently resolves to.

    Read from where the link points (ov_pool/ov_<version>/venv) rather than a
    recorded value, so it stays true even if the link was moved by hand or by a
    build. None when the venv is not yet installed or is not a pool link.
    """
    venv = paths()["venv"]
    try:
        target = venv.resolve()
    except OSError:
        return None
    # Resolves to ov_pool/ov_<version>/venv. Match the ov_<version> dir, not the
    # ov_pool container above it -- both start with "ov_", so key on the digit a
    # version begins with.
    for part in target.parts:
        if part.startswith("ov_") and part[len("ov_"):len("ov_") + 1].isdigit():
            return part[len("ov_"):]
    return None


def switch_ov(version: str) -> dict:
    """Point the active venv at OpenVINO ``version`` via `uv_build_envs.sh use`.

    Fast -- it only relinks the pool -- so it runs inline rather than as a job.
    Refuses while the single execution slot is busy: swapping the venv underneath
    a running setup or benchmark would break it, exactly as a rebuild would.
    Returns the fresh environment status. Raises ValueError for an unknown
    version, jobs.BenchBusy when a job holds the slot, and RuntimeError if the
    switch command fails.
    """
    version = str(version or "").strip()
    if version not in available_ov_versions():
        raise ValueError(f"OpenVINO {version!r} is not a built version")

    current = jobs.manager.current()
    if current is not None:
        raise jobs.BenchBusy(current)

    if not BUILD_SCRIPT.is_file():
        raise RuntimeError(f"build script not found: {BUILD_SCRIPT}")

    logger.info(f"Switching active OpenVINO to {version} via {BUILD_SCRIPT}")
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "use", version],
        cwd=str(SRC_ROOT),
        env=build_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"failed to switch OpenVINO to {version}: {tail}")

    # The link now resolves to a different venv, so the cached probe (keyed on the
    # interpreter's identity and site-packages mtime) no longer applies. Announce
    # the new status; probe() will re-read the freshly linked venv on the next poll.
    from benchmark.service import events
    events.publish_env()
    return probe()


# Cached answer from the venv, plus the state of the venv it was taken from.
#
# The probe imports transformers, which drags in torch and costs seconds. The
# dashboard polls GET /bench/env every 2s while a job runs, so doing this inline
# would blow past the browser's request timeout and make the whole tab report
# itself broken -- which is exactly what it used to do on the first poll after a
# setup finished. So the probe runs on a background thread and the request always
# answers from cache, saying `probing` when the cache has nothing for the venv it
# is currently looking at.
# Keyed by str(python): the base venv and every built OpenVINO column each get
# their own slot, so the Environment drawer can show the exact packages inside a
# selected OV venv without the venvs clobbering one another's cached answer.
_probe_lock = threading.Lock()       # guards _probe_cache
_probe_run_lock = threading.Lock()   # serialises the actual subprocess probes
_probe_cache: Dict[str, dict] = {}   # str(python) -> {"key", "versions", "running"}


def _probe_slot(python: Path) -> dict:
    """The cache slot for ``python`` (created on first use). Call under _probe_lock."""
    slot = _probe_cache.get(str(python))
    if slot is None:
        slot = {"key": None, "versions": {}, "running": False}
        _probe_cache[str(python)] = slot
    return slot

_PROBE_SCRIPT = (
    "import json,platform\n"
    "out={'python': platform.python_version()}\n"
    "for name in ('huggingface_hub','transformers','openvino','nncf','optimum'):\n"
    "    try:\n"
    "        mod=__import__(name)\n"
    "        out[name]=getattr(mod,'__version__',None)\n"
    "    except Exception:\n"
    "        out[name]=None\n"
    "print(json.dumps(out))\n"
)


# Static reference: what package versions each OpenVINO release installs. Shown
# read-only in the dashboard's Environment drawer. It does NOT drive any build --
# the version actually built comes from the per-run choice on the Models tab and
# uv_build_envs.sh builds that column on demand.
OV_REFERENCE_FILE = SRC_ROOT / "requirements" / "ov_reference.json"
_ov_reference_cache: Optional[list] = None


def ov_reference() -> list:
    """The OpenVINO reference table (``[{version, packages}, ...]``), or []."""
    global _ov_reference_cache
    if _ov_reference_cache is not None:
        return _ov_reference_cache
    try:
        data = json.loads(OV_REFERENCE_FILE.read_text())
        versions = data.get("versions", [])
        _ov_reference_cache = versions if isinstance(versions, list) else []
    except (OSError, ValueError) as exc:
        logger.debug(f"OpenVINO reference not loaded: {exc}")
        _ov_reference_cache = []
    return _ov_reference_cache


def _probe_key(python: Path) -> tuple:
    """Cache key: identity of the interpreter plus the state of its packages.

    site-packages' mtime moves whenever pip adds or removes a distribution, which
    is the only way the answer can change while the interpreter stays put. Two
    stats, versus an interpreter start plus a torch import.
    """
    def mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    site_packages = sorted(python.parent.parent.glob("lib/python*/site-packages"))
    return (str(python), mtime(python),
            mtime(site_packages[0]) if site_packages else -1)


def _refresh_versions(python: Path, key: tuple) -> None:
    """Ask the venv what it has and cache it under ``key``. Slow; runs on a thread
    for status polls, and inline for :func:`probe` with ``wait=True``."""
    with _probe_run_lock:
        # Whoever we queued behind may already have answered for this exact key.
        with _probe_lock:
            slot = _probe_slot(python)
            if slot["key"] == key:
                slot["running"] = False
                return
        versions: Dict[str, Optional[str]] = {}
        try:
            # One subprocess for all packages -- the alternative (one import per
            # package) costs an interpreter start each. Generous timeout: this is
            # off the request path, and a cold torch import is slow.
            result = subprocess.run(
                [str(python), "-c", _PROBE_SCRIPT],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                versions = json.loads(result.stdout.strip() or "{}")
        except Exception as exc:
            logger.debug(f"Benchmark venv probe failed: {exc}")
        with _probe_lock:
            slot = _probe_slot(python)
            slot["running"] = False
            # Only a good answer is remembered. A timeout or a half-built venv has
            # to be retried on the next poll, not cached as "nothing installed".
            if versions:
                slot["key"] = key
                slot["versions"] = versions

    if versions:
        # The dashboard is told the answer landed rather than being left to ask
        # again: `probing` is the one status that resolves on its own timetable,
        # with no request or job transition to hang the news on.
        #
        # Imported here, not at module scope: events.py imports this module.
        from benchmark.service import events
        events.publish_env()


def _probe_versions(python: Path, wait: bool = False) -> tuple:
    """``(versions, probing)`` for the given interpreter.

    ``probing`` means the answer is not known yet and a refresh is running; the
    caller reports that rather than letting an empty result read as a broken venv.
    With ``wait`` the refresh happens inline instead, for the one caller that must
    not guess -- see :func:`start_setup`.
    """
    key = _probe_key(python)
    with _probe_lock:
        slot = _probe_slot(python)
        if slot["key"] == key:
            return slot["versions"], False
        running = slot["running"]
        if not running:
            slot["running"] = True

    if wait:
        # Blocks on _probe_run_lock if a background refresh is already going, so
        # either way this returns only once an answer for `key` exists or failed.
        _refresh_versions(python, key)
        with _probe_lock:
            slot = _probe_slot(python)
            hit = slot["key"] == key
            return (slot["versions"] if hit else {}), False

    if running:
        return {}, True
    try:
        threading.Thread(
            target=_refresh_versions, args=(python, key), daemon=True,
            name="bench-venv-probe",
        ).start()
    except Exception:
        with _probe_lock:
            _probe_slot(python)["running"] = False
        raise
    return {}, True


def ov_versions_detail() -> list:
    """For each built OpenVINO column, the exact packages installed in its venv.

    ``[{"version": "2026.2.0", "packages": {"openvino": "...", ...}}, ...]``,
    newest first. Empty until a version has actually been built -- which is what
    the Environment drawer shows: pick a built version, see what is inside it.

    Each venv is probed with the same per-venv cache as the base venv, on a
    background thread, so this never blocks the 2s status poll; a freshly built
    version reads as empty packages for one poll, then fills in (a env SSE event
    is published when it lands).
    """
    pool = paths()["ov_pool"]
    detail = []
    probing = _setup_running()
    for ver in available_ov_versions():
        python = _venv_python(pool / f"ov_{ver}" / "venv")
        versions: Dict[str, Optional[str]] = {}
        # Don't probe while a setup is rewriting site-packages (see probe()).
        if python is not None and not probing:
            versions, _ = _probe_versions(python)
        detail.append({"version": ver, "packages": versions})
    return detail


def _count_models(models_dir: Path) -> int:
    """Number of downloaded model directories (one level under models/<source>/)."""
    if not models_dir.is_dir():
        return 0
    count = 0
    try:
        for source in models_dir.iterdir():
            if not source.is_dir():
                continue
            for entry in source.iterdir():
                if entry.is_dir():
                    count += 1
    except OSError:
        pass
    return count


def _setup_running() -> bool:
    current = jobs.manager.current()
    return current is not None and current.kind == "setup"


def probe(wait: bool = False) -> dict:
    """Full environment status for GET /bench/env.

    Never blocks on the venv unless ``wait`` is set, which only start_setup does:
    it is about to move an existing environment aside, so it cannot act on a
    not-known-yet.
    """
    ensure_dirs()
    p = paths()
    python = _venv_python(p["venv"])

    if python is None:
        versions, probing = {}, False
    elif _setup_running():
        # pip is rewriting site-packages right now: any answer is stale before it
        # reaches the browser, and each probe costs a torch import. The UI shows
        # the setup job's own status for the duration.
        versions, probing = {}, False
    else:
        versions, probing = _probe_versions(python, wait=wait)

    # The genai checkout supplies llm_bench, which the benchmark stage drives.
    # A venv without it means setup was interrupted partway.
    genai_ready = (p["genai"] / "tools" / "llm_bench").is_dir()
    venv_exists = python is not None
    # The active `venv` is now the huggingface-only base venv: listing models and
    # the build/download stage (`hf download`) need only the `hf` CLI, not
    # OpenVINO, which is built per-version on demand at benchmark time. So "ready
    # enough to use the tab" is: the base venv can import huggingface_hub. While
    # `probing` this is not yet known -- the UI must not read the interim False as
    # "broken". transformers is tracked separately (venv_usable) for callers that
    # still care whether a full OV column is present in the active venv.
    hf_ready = venv_exists and bool(versions.get("huggingface_hub"))
    venv_usable = venv_exists and bool(versions.get("transformers"))

    return {
        "enabled": enabled(),
        "env_root": str(p["env_root"]),
        "src_root": str(SRC_ROOT),
        "venv_dir": str(p["venv"]),
        "venv_exists": venv_exists,
        "venv_usable": venv_usable,
        "hf_ready": hf_ready,
        "genai_ready": genai_ready,
        # A run can be started once huggingface is available: the build/download
        # stage needs only that, and a benchmark builds its OpenVINO column on
        # demand (run_template.sh) rather than requiring it to exist up front.
        "ready": hf_ready,
        # True while the venv's package list is still being read on a background
        # thread. Distinguishes "we do not know yet" from "the venv is broken",
        # which otherwise look identical (both have empty `versions`).
        "probing": probing,
        "versions": versions,
        # OpenVINO columns already built on disk (may be empty until a benchmark
        # has built one). No longer drives a switch UI; kept for diagnostics.
        "ov_versions": available_ov_versions(),
        "active_ov": active_ov_version(),
        # The Environment drawer's OpenVINO dropdown: each BUILT version and the
        # exact packages inside its venv. Empty until a version is built, so the
        # drawer's package list is empty on a fresh environment.
        "ov_versions_detail": ov_versions_detail(),
        # Static reference of known releases -> package versions. Not shown in the
        # drawer; used only to seed the Models tab's version suggestions.
        "ov_reference": ov_reference(),
        "models_dir": str(p["models"]),
        "model_count": _count_models(p["models"]),
        "setup_script": str(SETUP_SCRIPT),
    }


def start_setup(force: bool = False) -> jobs.Job:
    """Run setup_env.sh in the background.

    With ``force`` false and a usable venv already in place this refuses (raising
    SetupAlreadyDone) rather than spending an hour rebuilding what already works;
    the dashboard turns that into a confirmation prompt. With ``force`` true the
    old venv is moved aside rather than deleted, so a botched rebuild can be
    rolled back by renaming it back.
    """
    # wait=True: "is there already a usable venv here" decides whether this moves
    # an hour of work aside, so it must be answered, not guessed at.
    status = probe(wait=True)
    if not status["enabled"]:
        raise RuntimeError("the benchmark feature is disabled")

    p = paths()
    # Setup now clones genai and builds the huggingface-only base venv (OpenVINO
    # columns come later, on demand). "Already done" is therefore: the base venv
    # can list/download models and the genai checkout is present -- not a full OV
    # venv, which no longer exists until a benchmark builds one.
    if status["hf_ready"] and status["genai_ready"] and not force:
        raise SetupAlreadyDone(status)

    ensure_dirs()
    if force and p["venv"].exists():
        backup = p["venv"].with_name(f"venv.bak-{int(time.time())}")
        logger.info(f"Rebuilding benchmark venv; moving {p['venv']} -> {backup}")
        p["venv"].rename(backup)

    log_path = p["logs"] / f"setup_{int(time.time())}.log"
    header = (
        f"=== benchmark environment setup ===\n"
        f"script : {SETUP_SCRIPT}\n"
        f"root   : {p['env_root']}\n"
        f"force  : {force}\n\n"
    )
    return jobs.manager.start(
        kind="setup",
        argv=["bash", str(SETUP_SCRIPT)],
        # setup_env.sh resolves its own directory, but the pip/git steps behave
        # better with the source tree as CWD.
        cwd=str(SRC_ROOT),
        env=build_subprocess_env(),
        log_path=log_path,
        header=header,
        meta={"force": force},
        on_finish=_after_setup,
    )


def _after_setup(job: jobs.Job) -> None:
    """Build the model list now that a successful setup has provided the `hf` CLI.

    Until this ran, installing the environment left the Benchmark tab facing the
    same empty model list it started with, with nothing to say that the one step
    left was to press Refresh -- the startup prefetch had already declined,
    minutes earlier, because there was no `hf` to search with.

    Imported here rather than at module scope: models imports this module.
    """
    if job.returncode != 0:
        return
    from benchmark.service import models

    reason = models.refresh_async()
    if reason:
        logger.info(f"Benchmark setup finished; model list not refreshed: {reason}")
