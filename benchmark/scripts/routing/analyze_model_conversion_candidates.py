#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from operator import contains
from pathlib import Path

#ONNX:
#.onnx

#PaddlePaddle:
#.pdmodel (+ .pdiparams)

#TensorFlow:
#.pb
#.pbtxt
#.meta
#.tflite
#SavedModel directory (contains saved_model.pb)

#PyTorch:
#.pt2 (ExportedProgram, documented disk format)
#.pt / .pth (common TorchScript-style files, but usually convert via Python API with example_input rather than assuming raw ovc CLI will handle arbitrary checkpoints)


DIRECT_ARTIFACT_PATTERNS = (
    "*.onnx",       # ONNX models
    "*.pb",         # TensorFlow frozen graph (binary format with embedded weights)
    "*.pdmodel",    # PaddlePaddle model
    "*.pt2",        # PyTorch ExportedProgram (official ovc format)
)
IMPORTANT_FILE_NAMES = (
    "config.json",
    "configuration.json",
    "config.yaml",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "modules.json",
    "sentence_bert_config.json",
    "README.md",
)
SOURCE_DIR_NAMES = {"huggingface", "modelscope", "github"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze local model packages and suggest whether they are better suited for optimum or OVC conversion."
    )
    parser.add_argument(
        "--models-dir",
        default="/tmp/skill_env/models",
        help="Models root directory. Can be a source root like /tmp/skill_env/models or a direct source dir like /tmp/skill_env/models/huggingface.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--json-output",
        default="",
        help="Optional path to also write the analysis result as JSON.",
    )
    return parser.parse_args()


def safe_read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def gather_model_dirs(models_dir: Path) -> list[tuple[str, Path]]:
    child_dirs = sorted([path for path in models_dir.iterdir() if path.is_dir()])
    if not child_dirs:
        return []

    if {path.name for path in child_dirs}.issubset(SOURCE_DIR_NAMES):
        result: list[tuple[str, Path]] = []
        for source_dir in child_dirs:
            for model_dir in sorted([path for path in source_dir.iterdir() if path.is_dir()]):
                result.append((source_dir.name, model_dir))
        return result

    return [(models_dir.name, model_dir) for model_dir in child_dirs]


def find_direct_artifacts(model_dir: Path) -> list[Path]:
    artifacts: list[Path] = []
    for pattern in DIRECT_ARTIFACT_PATTERNS:
        artifacts.extend(sorted(path for path in model_dir.rglob(pattern) if path.is_file()))
    return artifacts


def pick_largest(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return sorted(paths, key=lambda item: item.stat().st_size, reverse=True)[0]


def infer_task(model_name: str, model_dir: Path) -> str:
    lower = model_name.lower()
    if any(token in lower for token in ("whisper", "asr", "paraformer", "sensevoice", "moonshine")):
        return "automatic-speech-recognition"
    if any(token in lower for token in ("tts", "cosyvoice", "kokoro", "speech")):
        return "text-to-speech"
    if any(token in lower for token in ("vl", "vision", "multimodal", "llava", "internvl", "ocr")):
        return "image-text-to-text"
    if any(token in lower for token in ("bge", "embedding", "reranker")):
        return "feature-extraction"
    if (model_dir / "tokenizer.json").exists() and (model_dir / "config.json").exists():
        return "text-generation"
    return "text-generation"


def detect_library(config: dict | None, model_dir: Path) -> str | None:
    if (model_dir / "modules.json").exists() or (model_dir / "1_Pooling" / "config.json").exists():
        if config and config.get("model_type"):
            return "sentence_transformers or transformers"
        return "sentence_transformers"
    if config and config.get("model_type"):
        return "transformers"
    return None


def gather_important_files(model_dir: Path, primary_artifact: Path | None) -> list[str]:
    important: list[str] = []
    for name in IMPORTANT_FILE_NAMES:
        for path in sorted(model_dir.rglob(name)):
            if path.is_file():
                important.append(str(path.relative_to(model_dir)))
    if primary_artifact is not None:
        artifact_rel = str(primary_artifact.relative_to(model_dir))
        if artifact_rel not in important:
            important.insert(0, artifact_rel)
    # Keep the list short and stable for human review.
    deduped: list[str] = []
    for item in important:
        if item not in deduped:
            deduped.append(item)
    return deduped[:12]

def get_ovc_abled_file(model_dir: Path) -> Path | None:
    artifacts = find_direct_artifacts(model_dir)
    if not artifacts:
        return None
    return pick_largest(artifacts)

def analyze_model(source: str, model_dir: Path) -> dict:
    model_name = model_dir.name
    config_path = model_dir / "config.json"
    config = safe_read_json(config_path) if config_path.exists() else None
    has_model_type = bool(config and config.get("model_type"))
    direct_artifacts = find_direct_artifacts(model_dir)
    primary_artifact = pick_largest(direct_artifacts)
    inferred_task = infer_task(model_name, model_dir)
    library_hint = detect_library(config, model_dir)
    reasons: list[str] = []

    ovc_candidate = primary_artifact is not None
    optimum_candidate = has_model_type or (model_dir / "modules.json").exists()

    if ovc_candidate:
        reasons.append(
            f"Found direct export artifact {primary_artifact.relative_to(model_dir)}; OVC can try this file directly."
        )
    if has_model_type:
        reasons.append(f"config.json provides model_type={config['model_type']}; this matches optimum/AutoConfig expectations.")
    elif (model_dir / "modules.json").exists():
        reasons.append("Found modules.json; package looks like a sentence-transformers style model that optimum may load.")
    else:
        reasons.append("No config.json with model_type detected; optimum may fail on AutoConfig-based loading.")

    if ovc_candidate:
        primary_strategy = "ovc"
    elif optimum_candidate:
        primary_strategy = "optimum"
    else:
        primary_strategy = "manual_review"

    if ovc_candidate and optimum_candidate:
        reasons.append("Both paths are plausible: OVC has a concrete artifact, while optimum may still work on the package view.")
    elif primary_strategy == "manual_review":
        reasons.append("Package lacks both a standard HF config and a direct OVC-friendly artifact; manual export logic may be needed.")

    return {
        "source": source,
        "model": model_name,
        "model_dir": str(model_dir),
        "primary_strategy": primary_strategy,
        "ovc_candidate": ovc_candidate,
        "ovc_input": str(primary_artifact) if primary_artifact else None,
        "optimum_candidate": optimum_candidate,
        "inferred_task": inferred_task,
        "library_hint": library_hint,
        "config_model_type": config.get("model_type") if config else None,
        "important_files": gather_important_files(model_dir, primary_artifact),
        "reasons": reasons,
    }


def render_text(results: list[dict]) -> str:
    lines: list[str] = []
    ovc_primary = [item["model"] for item in results if item["primary_strategy"] == "ovc"]
    optimum_primary = [item["model"] for item in results if item["primary_strategy"] == "optimum"]
    manual_review = [item["model"] for item in results if item["primary_strategy"] == "manual_review"]

    lines.append("Summary")
    lines.append(f"- OVC primary: {len(ovc_primary)}")
    lines.append(f"- Optimum primary: {len(optimum_primary)}")
    lines.append(f"- Manual review: {len(manual_review)}")
    lines.append("")

    if ovc_primary:
        lines.append("OVC Primary")
        for item in ovc_primary:
            lines.append(f"- {item}")
        lines.append("")

    if optimum_primary:
        lines.append("Optimum Primary")
        for item in optimum_primary:
            lines.append(f"- {item}")
        lines.append("")

    if manual_review:
        lines.append("Manual Review")
        for item in manual_review:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("Details")
    for item in results:
        lines.append(f"[{item['model']}]")
        lines.append(f"- source: {item['source']}")
        lines.append(f"- primary_strategy: {item['primary_strategy']}")
        lines.append(f"- ovc_candidate: {item['ovc_candidate']}")
        if item["ovc_input"]:
            lines.append(f"- ovc_input: {item['ovc_input']}")
        lines.append(f"- optimum_candidate: {item['optimum_candidate']}")
        lines.append(f"- inferred_task: {item['inferred_task']}")
        if item["library_hint"]:
            lines.append(f"- library_hint: {item['library_hint']}")
        if item["config_model_type"]:
            lines.append(f"- config_model_type: {item['config_model_type']}")
        lines.append("- important_files:")
        for path in item["important_files"]:
            lines.append(f"  - {path}")
        lines.append("- reasons:")
        for reason in item["reasons"]:
            lines.append(f"  - {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    models_dir = Path(args.models_dir).expanduser().resolve()
    if not models_dir.exists() or not models_dir.is_dir():
        raise SystemExit(f"models dir does not exist: {models_dir}")

    results = [analyze_model(source, model_dir) for source, model_dir in gather_model_dirs(models_dir)]

    if args.json_output:
        output_path = Path(args.json_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.format == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(render_text(results), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())