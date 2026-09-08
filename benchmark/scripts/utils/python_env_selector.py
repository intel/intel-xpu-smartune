"""Select a Python virtual environment by transformers version bounds."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# Side venvs (one per pinned transformers version) live under the multi-version
# env tree that uv_build_envs.sh builds -- PYENV_VERSION_DIR in
# configs/global_vars.sh, i.e. ${DIR_ENV_ROOT}/.gen/multi_env/python_version.
# SmarTune relocates DIR_ENV_ROOT away from /tmp; the fallback mirrors the shell
# default for callers that did not source global_vars.sh.
DEFAULT_PYTHON_VERSION_ROOT = os.environ.get(
    "PYENV_VERSION_DIR",
    os.path.join(
        os.environ.get("DIR_ENV_ROOT", "/tmp/skill_env"),
        ".gen",
        "multi_env",
        "python_version",
    ),
)


@dataclass
class PythonEnvSelection:
    """Result of selecting a python environment for wrapper execution."""

    venv_dir: str
    selected_transformers_version: Optional[str]
    source: str
    reason: str


def _safe_version_key(version_text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version_text)
    if not numbers:
        return (0,)
    key = [int(num) for num in numbers]
    while len(key) < 3:
        key.append(0)
    return tuple(key)


def _normalize_version(version_text: Optional[str]) -> Optional[str]:
    if version_text is None:
        return None
    text = str(version_text).strip()
    return text or None


def _normalize_upper_bound(bound: str) -> str:
    # Convert wildcard bounds like 5.2.* to a comparable numeric ceiling.
    return bound.replace("*", "999999")


def _version_in_range(version: str, min_version: Optional[str], max_version: Optional[str]) -> bool:
    version_key = _safe_version_key(version)
    if min_version:
        if version_key < _safe_version_key(min_version):
            return False
    if max_version:
        if version_key > _safe_version_key(_normalize_upper_bound(max_version)):
            return False
    return True


def _read_transformers_version(venv_dir: Path) -> Optional[str]:
    python_bin = venv_dir / "bin" / "python"
    if not python_bin.exists():
        return None
    try:
        result = subprocess.run(
            [
                str(python_bin),
                "-c",
                "import transformers; print(transformers.__version__)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None
    return _normalize_version(result.stdout)


def _discover_candidate_envs(base_dir: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    if not base_dir.exists() or not base_dir.is_dir():
        return candidates

    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        version = _read_transformers_version(entry)
        if not version:
            continue
        candidates.append((entry, version))

    return candidates


def select_venv_for_route(
    route: Dict[str, Any],
    default_venv_dir: str,
    python_version_root: str = DEFAULT_PYTHON_VERSION_ROOT,
) -> PythonEnvSelection:
    """
    Select venv for a route.

    Rule:
    1) If current transformers version is within [min, max], use default venv.
    2) Otherwise, find a compatible env under python_version_root and choose the
       highest compatible transformers version.
    3) Fallback to default venv when no compatible env is found.
    """
    params = route.get("parameters", {}) if isinstance(route, dict) else {}
    version_info = params.get("version_info", {}) if isinstance(params, dict) else {}

    current_version = _normalize_version(version_info.get("current_transformers_version"))
    min_version = _normalize_version(version_info.get("min_transformers_version"))
    max_version = _normalize_version(version_info.get("max_transformers_version"))

    # If max_version is None, use min_version as the upper bound
    if max_version is None:
        max_version = min_version

    if not current_version:
        return PythonEnvSelection(
            venv_dir=default_venv_dir,
            selected_transformers_version=None,
            source="default",
            reason="No current_transformers_version in route, fallback to default venv",
        )

    if _version_in_range(current_version, min_version, max_version):
        return PythonEnvSelection(
            venv_dir=default_venv_dir,
            selected_transformers_version=current_version,
            source="default",
            reason="Current transformers version is compatible with route bounds",
        )

    candidates = _discover_candidate_envs(Path(python_version_root))
    compatible = [
        (env_path, version)
        for env_path, version in candidates
        if _version_in_range(version, min_version, max_version)
    ]

    if compatible:
        compatible.sort(key=lambda item: _safe_version_key(item[1]), reverse=True)
        selected_env, selected_version = compatible[0]
        return PythonEnvSelection(
            venv_dir=str(selected_env),
            selected_transformers_version=selected_version,
            source="python_version_pool",
            reason=f"Selected compatible env from {python_version_root}",
        )

    return PythonEnvSelection(
        venv_dir=default_venv_dir,
        selected_transformers_version=current_version,
        source="fallback_default",
        reason=f"No compatible env found under {python_version_root}, fallback to default venv",
    )
