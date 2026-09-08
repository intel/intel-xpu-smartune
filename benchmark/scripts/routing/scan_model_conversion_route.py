#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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

    def _task_from_model_type(path: str, library: str | None) -> str | None:
        if library != "transformers":
            return None, None

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


def analyze_model_id(model_id: str) -> tuple[str | None, str | None, bool | None, dict[str, Any]]:
    """Infer (library, task, supported) from model_id via optimum behavior.

    Returns:
    - (library_name, task_name, True, version_info) when both can be inferred and supported
    - (None, None, False, version_info) otherwise
    """
    library_name, task_name, version_info, error = _run_optimum_task_probe(model_id)
    if error is not None:
        print(f"ERROR: {error}")
        return None, None, False, {}

    if library_name is None or task_name is None:
        return None, None, False, {}

    if library_name not in SUPPORTED_OPTIMUM_LIBRARIES:
        return None, None, False, {} 

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
