#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_OPTIMUM_PYTHON = "/tmp/skill_env/venv/bin/python"
SUPPORTED_OPTIMUM_LIBRARIES = {
    "transformers",
    "diffusers",
    "timm",
    "open_clip",
    "kokoro",
}

# Per-model cache of the optimum probe result, so repeated routing runs do not
# pay the multi-second `import optimum` cost again.
#
# One file per MODEL: <DIR_IR>/<model>/model_routing.json, shared by all of its
# precision directories (int4_ov, int8_ov, ...) -- they are exports of one model
# and probe to the same library and task. Schema 1 kept a copy in every
# precision directory; one found there is moved up on first read.
#
# It holds what optimum says about the model and nothing about the environment
# reading it: library, task, the transformers bounds of its export config and
# the tasks it supports. Whether the installed transformers falls inside those
# bounds is decided by whoever needs to know (python_env_selector), against the
# venv it is choosing -- so a different environment does not invalidate it.
ROUTING_CACHE_FILENAME = "model_routing.json"
ROUTING_CACHE_SCHEMA_VERSION = 2
# The environment-independent part of a probe's version_info.
_MODEL_VERSION_KEYS = ("min_transformers_version", "max_transformers_version", "tasks")


def _normalize_library_name(library_name: str | None) -> str | None:
    if library_name is None:
        return None
    mapping = {
        "sentence-transformers": "transformers",
        "sentence_transformers": "transformers",
    }
    return mapping.get(library_name, library_name)


def _normalize_version_bound(version_value: Any, *, is_max: bool = False) -> str | None:
    if version_value is None:
        return None
    text = getattr(version_value, "base_version", None) or str(version_value)
    return text.replace("99", "*") if is_max else text


def _compute_transformers_version_bounds(
    model_name_or_path: str,
    task_name: str | None,
    library_name: str | None,
    auto_config_cls: Any,
    tasks_manager_cls: Any,
) -> dict[str, Any]:
    version_info: dict[str, Any] = {
        "current_transformers_version": None,
        "min_transformers_version": None,
        "max_transformers_version": None,
        "transformers_version_compatible": None,
    }

    if library_name != "transformers" or not task_name:
        return version_info

    try:
        from optimum.intel.utils.import_utils import _transformers_version, is_transformers_version
    except Exception:
        return version_info

    version_info["current_transformers_version"] = str(_transformers_version)

    try:
        config = auto_config_cls.from_pretrained(model_name_or_path, trust_remote_code=True)
    except Exception:
        return version_info

    model_type = getattr(config, "export_model_type", None) or getattr(config, "model_type", None)
    if not model_type:
        return version_info

    try:
        task_map = tasks_manager_cls.get_supported_tasks_for_model_type(
            model_type,
            exporter="openvino",
            library_name="transformers",
        )
    except Exception:
        return version_info

    constructor = task_map.get(task_name)
    if constructor is None and task_name.endswith("-with-past"):
        constructor = task_map.get(task_name.replace("-with-past", ""))
    if constructor is None:
        return version_info

    try:
        export_config = constructor(config)
    except Exception:
        return version_info

    min_version = _normalize_version_bound(getattr(export_config, "MIN_TRANSFORMERS_VERSION", None))
    max_version = _normalize_version_bound(getattr(export_config, "MAX_TRANSFORMERS_VERSION", None), is_max=True)

    version_info["min_transformers_version"] = min_version
    version_info["max_transformers_version"] = max_version

    compatible = True
    if min_version and is_transformers_version("<", min_version):
        compatible = False
    if max_version and is_transformers_version(">", max_version.replace("*", "99")):
        compatible = False
    version_info["transformers_version_compatible"] = compatible

    return version_info


def _run_optimum_task_probe(model_name_or_path: str) -> tuple[str | None, str | None, dict[str, Any], str | None]:
    """Infer library/task using the same path as optimum openvino main_export.

    This calls:
    - optimum.exporters.openvino.__main__.infer_library_name
    - optimum.exporters.openvino.__main__.infer_task(task="auto", ...)

    so behavior stays aligned with main_export's auto resolution semantics.
    """
    _ = DEFAULT_OPTIMUM_PYTHON  # kept for backward compatibility with external tooling

    try:
        from optimum.exporters.openvino.__main__ import infer_library_name as ov_infer_library_name
        from optimum.exporters.openvino.__main__ import infer_task as ov_infer_task
        from optimum.exporters.tasks import TasksManager
        from transformers import AutoConfig
    except Exception as exc:
        return None, None, {
            "current_transformers_version": None,
            "min_transformers_version": None,
            "max_transformers_version": None,
            "transformers_version_compatible": None,
        }, f"Failed to import optimum/transformers APIs: {exc}"

    def _task_from_non_transformers(path: str, library: str | None) -> tuple[str | None, list[str] | None]:
        """kokoro/open_clip/diffusers/timm: same auto-resolution as main_export, plus
        registry fallbacks for local dirs that optimum's infer_task cannot handle."""
        try:
            task = ov_infer_task("auto", path, library_name=library)
            return task, [task]
        except Exception:
            pass

        if library == "diffusers":
            model_index = Path(path) / "model_index.json"
            try:
                class_name = json.loads(model_index.read_text(encoding="utf-8")).get("_class_name")
            except Exception:
                return None, None
            for task, mapping in TasksManager._DIFFUSERS_TASKS_TO_MODEL_MAPPINGS.items():
                if class_name in mapping.values():
                    return task, [task]
            return None, None

        if library == "timm":
            default_model_type = TasksManager._MODEL_TYPE_FOR_DEFAULT_CONFIG.get("timm")
            try:
                tasks = list(TasksManager.get_supported_tasks_for_model_type(
                    default_model_type, exporter="openvino", library_name="timm").keys())
            except Exception:
                return None, None
            return (tasks[0], tasks) if tasks else (None, None)

        return None, None

    def _task_from_model_type(path: str, library: str | None) -> tuple[str | None, list[str] | None]:
        if library != "transformers":
            return _task_from_non_transformers(path, library)

        try:
            config = AutoConfig.from_pretrained(path, trust_remote_code=True)
        except Exception:
            return None, None

        model_type = getattr(config, "export_model_type", None) or getattr(config, "model_type", None)
        if not model_type:
            return None, None

        try:
            task_map = TasksManager.get_supported_tasks_for_model_type(
                model_type,
                exporter="openvino",
                library_name=library,
            )
        except Exception:
            return None, None

        tasks = list(task_map.keys())
        if not tasks:
            return None, None

        # Prefer canonical tasks first; keep deterministic fallback ordering.
        preferred = [
            "image-text-to-text",
            "automatic-speech-recognition",
            "text2text-generation",
            "text-generation",
            "text-classification", #for rerank model will both have feature-extraction and text-classification, we prefer text-classification for rerank model
            "feature-extraction",
            "image-classification",
            "zero-shot-image-classification",
        ]
        for base in preferred:
            with_past = f"{base}-with-past"
            if with_past in tasks:
                return with_past, tasks
            if base in tasks:
                return base, tasks

        non_with_past = sorted(t for t in tasks if not t.endswith("-with-past"))
        if non_with_past:
            base = non_with_past[0]
            return base, tasks

        return sorted(tasks)[0], tasks

    try:
        library_name = _normalize_library_name(ov_infer_library_name(model_name_or_path))
    except Exception as exc:
        return None, None, {
            "current_transformers_version": None,
            "min_transformers_version": None,
            "max_transformers_version": None,
            "transformers_version_compatible": None,
        }, f"Failed to infer library for {model_name_or_path}: {exc}"

    task_name: str | None = None
    tasks: list[str] | None = None
    try:
        task_name, tasks = _task_from_model_type(model_name_or_path, library_name)
    except Exception:
        if Path(model_name_or_path).is_dir():
            task_name, tasks = _task_from_model_type(model_name_or_path, library_name)

    if isinstance(task_name, str):
        task_name = task_name.strip() or None
    else:
        task_name = None

    version_info = _compute_transformers_version_bounds(
        model_name_or_path=model_name_or_path,
        task_name=task_name,
        library_name=library_name,
        auto_config_cls=AutoConfig,
        tasks_manager_cls=TasksManager,
    )
    version_info['tasks'] = tasks if isinstance(tasks, list) else []

    return library_name, task_name, version_info, None


def infer_task(model_name_or_path: str) -> str | None:
    """Infer export task via optimum openvino main_export inference path.

    This is a direct wrapper around optimum's infer_library_name + infer_task(auto)
    behavior and intentionally does not apply extra heuristics.
    """
    
    _, task_name, _, _ = _run_optimum_task_probe(model_name_or_path)
    return task_name


def _routing_cache_paths(model_id: Any) -> tuple[Path | None, Path | None]:
    """(cache file, schema-1 per-precision file) for a local model directory.

    A directory inside DIR_IR maps to its top-level model directory, whichever
    precision directory it is. One outside DIR_IR keeps the cache in itself. A
    hub id has no directory and is not cached.
    """
    try:
        candidate = Path(model_id).expanduser()
    except Exception:
        return None, None
    if not candidate.is_dir():
        return None, None
    root = os.environ.get("DIR_IR")
    if root:
        try:
            rel = candidate.resolve().relative_to(Path(root).expanduser().resolve()).parts
        except (OSError, ValueError):
            rel = ()
        if rel:
            model_dir = Path(root).expanduser() / rel[0]
            legacy = candidate / ROUTING_CACHE_FILENAME if len(rel) > 1 else None
            return model_dir / ROUTING_CACHE_FILENAME, legacy
    return candidate / ROUTING_CACHE_FILENAME, None


def _model_facts(version_info: Any) -> dict[str, Any]:
    """The environment-independent part of a probe's version_info."""
    info = version_info if isinstance(version_info, dict) else {}
    return {key: info.get(key) for key in _MODEL_VERSION_KEYS}


def _read_routing_cache(cache_file: Path | None) -> tuple[str, str, dict[str, Any]] | None:
    if cache_file is None or not cache_file.is_file():
        return None

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    # Schema 1 also recorded the transformers it was probed under and was
    # discarded when that changed; its model facts are as good as schema 2's.
    if payload.get("schema_version") not in (1, ROUTING_CACHE_SCHEMA_VERSION):
        return None

    library_name = payload.get("library")
    task_name = payload.get("task")
    if not isinstance(library_name, str) or not isinstance(task_name, str):
        return None

    return library_name, task_name, _model_facts(payload.get("version_info"))


def _load_routing_cache(
    cache_file: Path | None, legacy_file: Path | None,
) -> tuple[str, str, dict[str, Any]] | None:
    """The model's cache, or a schema-1 per-precision copy moved up to it.

    A per-precision copy is removed once the model-level file exists, so each
    model ends up with exactly one.
    """
    cached = _read_routing_cache(cache_file)
    if cached is None and legacy_file is not None:
        cached = _read_routing_cache(legacy_file)
        if cached is not None and not _store_routing_cache(cache_file, *cached):
            return cached
    if cached is not None and legacy_file is not None and legacy_file.is_file():
        try:
            legacy_file.unlink()
        except OSError:
            pass
    return cached


def _store_routing_cache(
    cache_file: Path | None,
    library_name: str,
    task_name: str,
    version_info: dict[str, Any],
) -> bool:
    if cache_file is None:
        return False

    payload = {
        "schema_version": ROUTING_CACHE_SCHEMA_VERSION,
        "library": library_name,
        "task": task_name,
        "version_info": _model_facts(version_info),
    }

    tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
    try:
        tmp_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(cache_file)
        return True
    except Exception:
        # A read-only model directory must not break routing.
        try:
            tmp_file.unlink()
        except Exception:
            pass
        return False


def analyze_model_id(model_id: str) -> tuple[str | None, str | None, bool | None, dict[str, Any]]:
    """Infer (library, task, supported) from model_id via optimum behavior.

    For a local model directory the probe result is cached once per model in
    `<DIR_IR>/<model>/model_routing.json` (see ROUTING_CACHE_FILENAME) and reused
    by every precision, which avoids the expensive optimum import.

    version_info carries only the model's transformers bounds and supported
    tasks, cached or not; comparing them with an installed transformers is the
    caller's job (python_env_selector.select_venv_for_route).

    Returns:
    - (library_name, task_name, True, version_info) when both can be inferred and supported
    - (None, None, False, version_info) otherwise
    """
    cache_file, legacy_file = _routing_cache_paths(model_id)
    cached = _load_routing_cache(cache_file, legacy_file)
    if cached is not None:
        library_name, task_name, version_info = cached
        if library_name not in SUPPORTED_OPTIMUM_LIBRARIES:
            return None, None, False, {}
        return library_name, task_name, True, version_info

    library_name, task_name, version_info, error = _run_optimum_task_probe(str(model_id))
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        return None, None, False, {}

    if library_name is None or task_name is None:
        return None, None, False, {}

    if library_name not in SUPPORTED_OPTIMUM_LIBRARIES:
        return None, None, False, {}

    version_info = _model_facts(version_info)
    _store_routing_cache(cache_file, library_name, task_name, version_info)

    return library_name, task_name, True, version_info


def _analyze_model_with_versions(model_name_or_path: str) -> tuple[str | None, str | None, bool, dict[str, Any]]:
    library_name, task_name, version_info, error = _run_optimum_task_probe(model_name_or_path)
    if error is not None:
        return None, None, False, version_info

    if library_name is None or task_name is None:
        return None, None, False, version_info

    if library_name not in SUPPORTED_OPTIMUM_LIBRARIES:
        return None, None, False, version_info

    return library_name, task_name, True, version_info


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Infer OpenVINO library/task via optimum TaskManager behavior.")
    parser.add_argument("model", help="Hugging Face model ID or local model directory.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


def _resolve_local_model_dir(model_input: str) -> Path | None:
    candidate = Path(model_input).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.exists() and candidate.is_dir():
        return candidate.resolve()
    return None


def _analyze_local_dir(model_dir: Path) -> tuple[str | None, str | None, bool]:
    library_name, task_name, supported, _ = _analyze_model_with_versions(str(model_dir))
    return library_name, task_name, supported


def main() -> int:
    args = parse_args()
    model_dir = _resolve_local_model_dir(args.model)

    if model_dir is not None:
        library_name, task_name, supported, version_info = _analyze_model_with_versions(str(model_dir))
        payload = {
            "input_type": "local_directory",
            "input": str(model_dir),
            "library_name": library_name,
            "task": task_name,
            "supported": supported,
            "current_transformers_version": version_info["current_transformers_version"],
            "min_transformers_version": version_info["min_transformers_version"],
            "max_transformers_version": version_info["max_transformers_version"],
            "transformers_version_compatible": version_info["transformers_version_compatible"],
        }
    else:
        library_name, task_name, supported, version_info = _analyze_model_with_versions(args.model)
        payload = {
            "input_type": "huggingface_model_id",
            "input": args.model,
            "library_name": library_name,
            "task": task_name,
            "supported": supported,
            "current_transformers_version": version_info["current_transformers_version"],
            "min_transformers_version": version_info["min_transformers_version"],
            "max_transformers_version": version_info["max_transformers_version"],
            "transformers_version_compatible": version_info["transformers_version_compatible"],
        }

    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"- input_type: {payload['input_type']}")
        print(f"- input: {payload['input']}")
        print(f"- library_name: {payload['library_name']}")
        print(f"- task: {payload['task']}")
        print(f"- supported: {payload['supported']}")
        print(f"- current_transformers_version: {payload['current_transformers_version']}")
        print(f"- min_transformers_version: {payload['min_transformers_version']}")
        print(f"- max_transformers_version: {payload['max_transformers_version']}")
        print(f"- transformers_version_compatible: {payload['transformers_version_compatible']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
