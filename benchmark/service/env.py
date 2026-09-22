# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Benchmark toolchain path and subprocess environment resolution.
# Generated scripts must not contain deployment credentials.

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from config.config import b_config
from utils.logger import get_logger
logger = get_logger(__name__)

from benchmark.service import jobs, privilege

# <repo>/benchmark/service/env.py -> <repo>/benchmark -> <repo>
PKG_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PKG_ROOT.parent
REPO_ROOT = SRC_ROOT.parent
SETUP_SCRIPT = SRC_ROOT / "setup_env.sh"
# Builds and switches benchmark virtual environments.
BUILD_SCRIPT = SRC_ROOT / "uv_build_envs.sh"
RUN_TEMPLATE = SRC_ROOT / "templates" / "run_template.sh"
SEARCH_MODELS_SCRIPT = PKG_ROOT / "search_models.py"

# Runtime-generated data lives here, never in source directories.
DEFAULT_ENV_ROOT = SRC_ROOT / "runtime"

# Create the runtime skeleton up front so UI paths are stable and ownership is
# consistent before any subprocess writes as an unprivileged user.
_RUNTIME_SUBDIRS = (
    "models", "benchmarks", "scripts",
    ".gen/logs", ".gen/runs", ".gen/cache", ".gen/home",
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
    """Return one key from the benchmark config block.

    Reads config on each call so runtime config edits are visible immediately.
    """
    value = _cfg().get(name)
    return default if value is None else value


def enabled() -> bool:
    """True only when benchmark is enabled and benchmark assets are present."""
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
        # Per-OpenVINO prebuilt environments, one ov_<version>/ directory each.
        "ov_pool": root / ".gen/multi_env/ov_pool",
        "models": root / "models",
        "logs": root / ".gen/logs",
        "benchmarks": root / "benchmarks",
        "runs": root / ".gen/runs",
        "cache": root / ".gen/cache",
        "genai": root / ".gen/genai",
    }


def ensure_dirs() -> None:
    """Create runtime directories with subprocess ownership semantics."""
    root = env_root()
    for name in _RUNTIME_SUBDIRS:
        privilege.ensure_dir(root / name)


def build_subprocess_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for benchmark subprocesses.

    Inject secrets and proxy settings via environment variables only so rendered
    run scripts never contain credentials.
    """
    cfg = _cfg()
    p = paths()
    env = os.environ.copy()

    env["SMARTUNE_BENCH_ENV_ROOT"] = str(p["env_root"])
    env["BENCH_SRC_ROOT"] = str(SRC_ROOT)
    # Cache path for model discovery output.
    env["BENCH_MODELS_CACHE_DIR"] = str(p["cache"])
    # Ensure subprocesses resolve tools from the benchmark venv.
    env["PYENV_VENV_DIR"] = str(p["venv"])
    env["PATH"] = f"{p['venv'] / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    # Keep child logs near-real-time in dashboard tailing.
    env["PYTHONUNBUFFERED"] = "1"

    # Children run unprivileged; do not leak root HOME/XDG paths into that context.
    who = privilege.target()
    if who is not None:
        home = privilege.child_home(p["env_root"])
        privilege.ensure_dir(home)
        env["HOME"] = str(home)
        env["USER"] = env["LOGNAME"] = who.name
        for name in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                     "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            env.pop(name, None)

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

    # Export lower/upper-case proxy variants for tool compatibility.
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


def available_ov_versions() -> list:
    """Built OpenVINO versions with usable venvs, newest first."""
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
    # Numeric sort: 2026.10.0 must come after 2026.3.0.
    return sorted(versions, key=_version_key, reverse=True)


# Strict OpenVINO version format accepted from requests.
_OV_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Oldest version guaranteed across the package trio.
OV_VERSION_FLOOR = "2025.3.0"

# Static fallback list for offline or refresh-failure cases.
OV_CHOICES_STATIC = (
    "2026.3.1", "2026.3.0",
    "2026.2.1", "2026.2.0",
    "2026.1.0",
    "2026.0.0",
    "2025.4.1", "2025.4.0",
    "2025.3.0",
)

_OV_CHOICES_TTL_SEC = 6 * 3600
_ov_choices_lock = threading.Lock()
_ov_choices_cache: dict = {"versions": (), "at": 0.0, "running": False}

# Package trio and version suffix conventions.
_OV_TRIO = (("openvino", ""), ("openvino-tokenizers", ".0"), ("openvino-genai", ".0"))


def _version_key(version: str) -> tuple:
    """Numeric sort key for a dotted version ("2026.10.0" after "2026.3.0")."""
    return tuple(int(p) if p.isdigit() else -1 for p in version.split("."))


def _pypi_versions(name: str, opener) -> set:
    """PyPI releases of name that actually publish installable files."""
    with opener.open(f"https://pypi.org/pypi/{name}/json", timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    releases = payload.get("releases") or {}
    return {version for version, files in releases.items() if files}


def _refresh_ov_choices() -> None:
    """Refresh the intersection of versions published by all required packages."""
    versions: tuple = ()
    try:
        # Reuse benchmark HTTP opener so proxy behavior matches subprocesses.
        from benchmark.service.search_models import _http_opener

        opener = _http_opener(build_subprocess_env())
        found = None
        for name, suffix in _OV_TRIO:
            raw = _pypi_versions(name, opener)
            # Normalize all versions to X.Y.Z before intersecting.
            bare = {v[: -len(suffix)] for v in raw if v.endswith(suffix)} if suffix else raw
            found = bare if found is None else (found & bare)
        floor = _version_key(OV_VERSION_FLOOR)
        versions = tuple(
            v for v in (found or ())
            if len(v.split(".")) == 3 and all(p.isdigit() for p in v.split("."))
            and _version_key(v) >= floor
        )
    except Exception as exc:
        logger.debug(f"OpenVINO version list not refreshed from PyPI: {exc}")

    with _ov_choices_lock:
        _ov_choices_cache["running"] = False
        if versions:
            _ov_choices_cache["versions"] = versions
            _ov_choices_cache["at"] = time.time()

    if versions:
        from benchmark.service import events
        events.publish_env()


def ov_choices() -> list:
    """All selectable OpenVINO versions, newest first.

    Combines static fallback, cached PyPI data, and locally built versions.
    """
    with _ov_choices_lock:
        live = _ov_choices_cache["versions"]
        stale = (time.time() - _ov_choices_cache["at"]) > _OV_CHOICES_TTL_SEC
        if stale and not _ov_choices_cache["running"]:
            _ov_choices_cache["running"] = True
            start = True
        else:
            start = False
    if start:
        try:
            threading.Thread(target=_refresh_ov_choices, daemon=True,
                             name="bench-ov-choices").start()
        except Exception:
            with _ov_choices_lock:
                _ov_choices_cache["running"] = False

    everything = set(OV_CHOICES_STATIC) | set(live) | set(available_ov_versions())
    return sorted(everything, key=_version_key, reverse=True)


def active_ov_version() -> Optional[str]:
    """OpenVINO version resolved by the active venv symlink, if any."""
    venv = paths()["venv"]
    try:
        target = venv.resolve()
    except OSError:
        return None
    # Match ov_<version> directory, not ov_pool.
    for part in target.parts:
        if part.startswith("ov_") and part[len("ov_"):len("ov_") + 1].isdigit():
            return part[len("ov_"):]
    return None


def switch_ov(version: str) -> dict:
    """Relink the active venv to a built OpenVINO version and return fresh status."""
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
        # Relink as the pool owner so future builds can still modify links.
        **privilege.spawn_kwargs(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"failed to switch OpenVINO to {version}: {tail}")

    # Active interpreter changed; publish state so package probes refresh.
    from benchmark.service import events
    events.publish_env()
    return probe()


# Per-interpreter probe cache. Probing can be slow (imports transformers/torch),
# so requests return cached data and report probing state while refreshing.
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


def _probe_key(python: Path) -> tuple:
    """Cache key from interpreter identity and site-packages modification state."""
    def mtime(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    site_packages = sorted(python.parent.parent.glob("lib/python*/site-packages"))
    return (str(python), mtime(python),
            mtime(site_packages[0]) if site_packages else -1)


def _refresh_versions(python: Path, key: tuple) -> None:
    """Probe package versions and store the result under key."""
    with _probe_run_lock:
        # Whoever we queued behind may already have answered for this exact key.
        with _probe_lock:
            slot = _probe_slot(python)
            if slot["key"] == key:
                slot["running"] = False
                return
        versions: Dict[str, Optional[str]] = {}
        try:
            # Probe in one subprocess; run as venv owner to avoid root-owned pyc.
            result = subprocess.run(
                [str(python), "-c", _PROBE_SCRIPT],
                env=build_subprocess_env(),
                capture_output=True, text=True, timeout=120,
                **privilege.spawn_kwargs(),
            )
            if result.returncode == 0:
                versions = json.loads(result.stdout.strip() or "{}")
        except Exception as exc:
            logger.debug(f"Benchmark venv probe failed: {exc}")
        with _probe_lock:
            slot = _probe_slot(python)
            slot["running"] = False
            # Cache only successful answers so transient failures are retried.
            if versions:
                slot["key"] = key
                slot["versions"] = versions

    if versions:
        # Imported here to avoid circular import at module scope.
        from benchmark.service import events
        events.publish_env()


def _probe_versions(python: Path, wait: bool = False) -> tuple:
    """Return (versions, probing) for an interpreter.

    wait=True forces inline refresh for callers that need a definitive answer.
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
        # Wait until this key is refreshed (or refresh fails).
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
    """Package details for each built OpenVINO venv, newest first."""
    pool = paths()["ov_pool"]
    detail = []
    probing = _setup_running()
    for ver in available_ov_versions():
        python = _venv_python(pool / f"ov_{ver}" / "venv")
        versions: Dict[str, Optional[str]] = {}
        # Skip probing while setup may rewrite site-packages.
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
    """Environment status payload for GET /bench/env."""
    ensure_dirs()
    p = paths()
    python = _venv_python(p["venv"])

    if python is None:
        versions, probing = {}, False
    elif _setup_running():
        # Setup may be mutating site-packages; avoid stale/expensive probe.
        versions, probing = {}, False
    else:
        versions, probing = _probe_versions(python, wait=wait)

    # llm_bench comes from genai checkout; missing means incomplete setup.
    genai_ready = (p["genai"] / "tools" / "llm_bench").is_dir()
    venv_exists = python is not None
    # Base-tab readiness depends on huggingface_hub; full benchmark readiness may
    # still require a selected OpenVINO column.
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
        # Tab is usable once huggingface_hub is available.
        "ready": hf_ready,
        # True while package probing is still in progress.
        "probing": probing,
        "versions": versions,
        # Versions built on disk.
        "ov_versions": available_ov_versions(),
        "active_ov": active_ov_version(),
        # Dropdown choices.
        "ov_choices": ov_choices(),
        # Package details for each built OpenVINO version.
        "ov_versions_detail": ov_versions_detail(),
        "models_dir": str(p["models"]),
        "model_count": _count_models(p["models"]),
        "setup_script": str(SETUP_SCRIPT),
    }


def start_setup(force: bool = False, ov: Optional[str] = None) -> jobs.Job:
    """Run setup_env.sh in background, optionally building one OpenVINO column.

    force=False refuses if requested assets already exist; force=True moves
    existing assets aside before rebuilding.
    """
    ov = str(ov or "").strip() or None
    if ov is not None and not _OV_VERSION_RE.match(ov):
        raise ValueError(f"invalid OpenVINO version: {ov!r} (expected X.Y.Z)")

    # Need definitive status before deciding whether to move existing env assets.
    status = probe(wait=True)
    if not status["enabled"]:
        raise RuntimeError("the benchmark feature is disabled")

    p = paths()
    # Treat missing requested OV column as not-yet-installed, even if base is ready.
    already = (status["hf_ready"] and status["genai_ready"]
               and (ov is None or ov in status["ov_versions"]))
    if already and not force:
        raise SetupAlreadyDone(status)

    ensure_dirs()
    if force and p["venv"].exists():
        backup = p["venv"].with_name(f"venv.bak-{int(time.time())}")
        logger.info(f"Rebuilding benchmark venv; moving {p['venv']} -> {backup}")
        p["venv"].rename(backup)
    if force and ov is not None:
        # Move aside to force rebuild; installer skips already-usable columns.
        column = p["ov_pool"] / f"ov_{ov}"
        if column.exists():
            backup = column.with_name(f"ov_{ov}.bak-{int(time.time())}")
            logger.info(f"Rebuilding OpenVINO {ov}; moving {column} -> {backup}")
            column.rename(backup)

    log_path = p["logs"] / f"setup_{int(time.time())}.log"
    # Include execution context to make privilege/permission failures actionable.
    header = (
        f"=== benchmark environment setup ===\n"
        f"script : {SETUP_SCRIPT}\n"
        f"root   : {p['env_root']}\n"
        f"force  : {force}\n"
        f"ov     : {ov or '-'}\n"
        + "".join(f"{line}\n" for line in privilege.describe())
        + "\n"
    )
    # setup_env.sh reads this to build one requested OpenVINO column.
    extra = {"BENCH_SETUP_OV": ov} if ov else None
    return jobs.manager.start(
        kind="setup",
        argv=["bash", str(SETUP_SCRIPT)],
        # Use source tree as CWD for pip/git stability.
        cwd=str(SRC_ROOT),
        env=build_subprocess_env(extra),
        log_path=log_path,
        header=header,
        meta={"force": force, "ov": ov},
        on_finish=_after_setup,
    )


# --- minimum environment bootstrap -----------------------------------------

# Prevent retry storms when bootstrap fails in one service session.
_bootstrap_attempted = False


def maybe_bootstrap() -> Optional[str]:
    """Start background base-venv bootstrap when missing.

    Returns None if started, otherwise a normal reason for skipping.
    """
    global _bootstrap_attempted
    if not enabled():
        return "the benchmark feature is disabled"
    if _bootstrap_attempted:
        return "a bootstrap has already been attempted in this session"
    if _venv_python(paths()["venv"]) is not None:
        return "the base environment is already installed"
    current = jobs.manager.current()
    if current is not None:
        # Keep retriable if the running job is unrelated to bootstrap.
        return f"a benchmark {current.kind} job is already running"

    ensure_dirs()
    p = paths()
    log_path = p["logs"] / f"bootstrap_{int(time.time())}.log"
    header = (
        f"=== benchmark base environment ===\n"
        f"script : {BUILD_SCRIPT} bootstrap\n"
        f"root   : {p['env_root']}\n"
        f"reason : opened the Benchmark tab with no base venv present\n"
        + "".join(f"{line}\n" for line in privilege.describe())
        + "\n"
    )
    _bootstrap_attempted = True
    logger.info(f"Installing the benchmark base environment via {BUILD_SCRIPT} bootstrap")
    try:
        # Reuse setup kind for shared UI state and SSE wiring.
        jobs.manager.start(
            kind="setup",
            argv=["bash", str(BUILD_SCRIPT), "bootstrap"],
            cwd=str(SRC_ROOT),
            env=build_subprocess_env(),
            log_path=log_path,
            header=header,
            meta={"scope": "bootstrap"},
            on_finish=_after_bootstrap,
        )
    except jobs.BenchBusy as exc:
        _bootstrap_attempted = False
        return str(exc)
    return None


def _after_bootstrap(job: jobs.Job) -> None:
    """Refresh stale model list after successful bootstrap.

    Import locally to avoid module-scope circular dependency.
    """
    if job.returncode != 0:
        return
    from benchmark.service import models

    reason = models.refresh_if_stale()
    if reason:
        logger.info(f"Benchmark base environment installed; model list not "
                    f"refreshed: {reason}")


def _after_setup(job: jobs.Job) -> None:
    """Refresh stale model list after successful setup.

    Import locally to avoid module-scope circular dependency.
    """
    if job.returncode != 0:
        return
    from benchmark.service import models

    reason = models.refresh_if_stale()
    if reason:
        logger.info(f"Benchmark setup finished; model list not refreshed: {reason}")
