# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Benchmark parameter schema for the model configuration page.
#
# The page renders each tool's parameter form from schema(): the hand-written
# layer of benchmark/scripts/params/bench_params.py merged with what the installed
# tool actually accepts, probed from its ArgumentParser inside the OpenVINO venv
# the model will run in (probe_cli_params.py). runner.py validates a request
# against the same merged schema, so the form and the check cannot drift.
#
# Probing imports the tool -- torch and openvino for llm_bench, several seconds --
# so results are cached on disk, keyed by what can change them:
#
#   genai          the sha256 of llm_bench's benchmark.py. There is one genai
#                  checkout shared by every OpenVINO version, and the parser is
#                  defined in that file, so the version does not enter the key.
#   benchmark_app  the OpenVINO version: it ships inside the openvino wheel.
#
# warm() probes in the background so the page normally never waits.

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger
logger = get_logger(__name__)

from benchmark.service import env, privilege
from benchmark.scripts.params import bench_params

_PROBE_SCRIPT = env.SRC_ROOT / "scripts" / "params" / "probe_cli_params.py"
_PREDICT_SCRIPT = env.SRC_ROOT / "scripts" / "routing" / "predict_bench_tool.py"
_PROBE_TIMEOUT_SEC = 180
_PREDICT_TIMEOUT_SEC = 180

_lock = threading.Lock()
# key -> probe payload, mirrored on disk under <cache>/bench_params/.
_probes: Dict[str, dict] = {}
_probe_errors: Dict[str, str] = {}
_inflight: Dict[str, threading.Lock] = {}
# (model, ov, built-tree mtime) -> prediction.
_predictions: Dict[tuple, dict] = {}


def _cache_dir() -> Path:
    return env.paths()["cache"] / "bench_params"


def _venv_python(ov: str) -> Path:
    return env.paths()["ov_pool"] / f"ov_{ov}" / "venv" / "bin" / "python"


def _benchmark_py() -> Path:
    return env.paths()["genai"] / "tools" / "llm_bench" / "benchmark.py"


def _probe_key(tool: str, ov: str) -> Optional[str]:
    if tool == "genai":
        path = _benchmark_py()
        if not path.is_file():
            return None
        return f"genai_{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
    return f"benchmark_app_{ov}"


def _last_json_line(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError("no JSON in output")


def _run_probe(tool: str, ov: str) -> dict:
    python = _venv_python(ov)
    if not python.is_file():
        raise RuntimeError(f"OpenVINO {ov} is not installed")
    argv = [str(python), str(_PROBE_SCRIPT), tool]
    if tool == "genai":
        argv += ["--benchmark-py", str(_benchmark_py())]
    result = subprocess.run(
        argv, cwd=str(env.SRC_ROOT), env=env.build_subprocess_env(),
        capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SEC,
        **privilege.spawn_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip()[-400:] or f"exit {result.returncode}")
    return _last_json_line(result.stdout)


def probe(tool: str, ov: Optional[str], wait: bool = True) -> Optional[dict]:
    """Layer A for (tool, ov): memory, then disk, then a probe (when `wait`).

    None when it is not known yet -- the venv is missing, the probe failed, or
    `wait` is False and nothing is cached.
    """
    if not ov:
        return None
    key = _probe_key(tool, ov)
    if key is None:
        return None
    with _lock:
        if key in _probes:
            return _probes[key]
        gate = _inflight.setdefault(key, threading.Lock())
    path = _cache_dir() / f"{key}.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            with _lock:
                _probes[key] = payload
            return payload
        except (OSError, ValueError):
            pass
    if not wait:
        return None
    # One probe per key at a time; a second caller waits for the first's result.
    with gate:
        with _lock:
            if key in _probes:
                return _probes[key]
        try:
            payload = _run_probe(tool, ov)
        except Exception as exc:
            logger.warning(f"Parameter probe for {tool} (OpenVINO {ov}) failed: {exc}")
            with _lock:
                _probe_errors[key] = str(exc)
            return None
        try:
            privilege.ensure_dir(_cache_dir())
            path.write_text(json.dumps(payload), encoding="utf-8")
            privilege.chown(path)
        except OSError as exc:
            logger.warning(f"Could not cache parameter probe {path}: {exc}")
        with _lock:
            _probes[key] = payload
            _probe_errors.pop(key, None)
        return payload


def warm(ov_versions) -> None:
    """Probe both tools for every installed version, in the background."""
    def _work():
        for ov in ov_versions:
            for tool in bench_params.TOOLS:
                probe(tool, ov)
    threading.Thread(target=_work, name="bench-param-probe", daemon=True).start()


def prompt_files() -> List[dict]:
    """llm_bench's stock prompt files, each with the tasks it is meant for."""
    root = env.paths()["genai"] / "tools" / "llm_bench" / bench_params.PROMPTS_SUBDIR
    if not root.is_dir():
        return []
    return [{"name": p.name, "tasks": bench_params.prompt_file_tasks(p.name)}
            for p in sorted(root.glob("*.jsonl")) if p.is_file()]


def file_choices() -> Dict[str, List[str]]:
    """Allowed values of every `file` parameter, by its `source`."""
    return {"prompts": [f["name"] for f in prompt_files()]}


def merged(tool: str, ov: Optional[str], wait: bool = True) -> Dict[str, dict]:
    return bench_params.merged_schema(tool, probe(tool, ov, wait=wait))


def schema(tool: str, ov: Optional[str]) -> dict:
    """What the page renders one tool's form from."""
    probed = probe(tool, ov)
    key = _probe_key(tool, ov) if ov else None
    params = bench_params.merged_schema(tool, probed)
    return {
        "tool": tool,
        "ov": ov,
        # False: layer B only, every parameter unverified against the tool.
        "probed": probed is not None,
        "probe_error": _probe_errors.get(key) if key else None,
        "source": (probed or {}).get("source"),
        "params": list(params.values()),
        "prompt_files": prompt_files() if tool == "genai" else [],
        "limits": {"max_values": bench_params.MAX_VALUES_PER_PARAM,
                   "max_variants": bench_params.MAX_VARIANTS},
    }


def normalize(bench, ov: Optional[str]) -> Optional[dict]:
    """A model's whole `bench` config, validated. Raises bench_params.ParamError."""
    if bench in (None, {}):
        return None
    if not isinstance(bench, dict):
        raise bench_params.ParamError("bench must be an object of tool -> configuration")
    out = {}
    files = file_choices()
    for tool, cfg in bench.items():
        if tool not in bench_params.TOOLS:
            raise bench_params.ParamError(f"unknown benchmark tool {tool!r}")
        normalized = bench_params.normalize_config(tool, cfg, merged(tool, ov), files)
        if normalized:
            out[tool] = normalized
    return out or None


def validate(tool: str, ov: Optional[str], cfg) -> dict:
    """Check one tool section the way a run would, for the form's live feedback."""
    try:
        normalized = bench_params.normalize_config(tool, cfg, merged(tool, ov), file_choices())
    except bench_params.ParamError as exc:
        return {"ok": False, "error": str(exc), "variants": 0}
    return {"ok": True, "error": None, "variants": bench_params.count_variants(normalized),
            "config": normalized}


def predict(model_id: str, ov: str) -> dict:
    """Which tool the router will give `model_id`'s built IR; see predict_bench_tool.py.

    Only a downloaded model has an answer -- the router decides from the IR, and
    its per-precision model_routing.json cache is shared with the run's plan.
    """
    models_dir = env.paths()["models"]
    safe = model_id.replace("/", "_").replace(".", "_")
    built = models_dir / safe
    try:
        mtime = built.stat().st_mtime if built.exists() else 0.0
    except OSError:
        mtime = 0.0
    cache_key = (model_id, ov, mtime)
    with _lock:
        if cache_key in _predictions:
            return _predictions[cache_key]
    python = _venv_python(ov)
    if not python.is_file():
        raise RuntimeError(f"OpenVINO {ov} is not installed")
    result = subprocess.run(
        [str(python), str(_PREDICT_SCRIPT), "--model", model_id, "--models-dir", str(models_dir)],
        cwd=str(env.SRC_ROOT), env=env.build_subprocess_env(),
        capture_output=True, text=True, timeout=_PREDICT_TIMEOUT_SEC,
        **privilege.spawn_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip()[-400:] or f"exit {result.returncode}")
    payload = _last_json_line(result.stdout)
    # Nothing built is not cached: the page asks for a download, and the next
    # open after it has to see the IR. (A download also changes the model
    # directory's mtime, which is in the key.)
    if payload.get("source") == "built":
        with _lock:
            _predictions[cache_key] = payload
    return payload
