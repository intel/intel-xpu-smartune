#!/usr/bin/env python3
"""
Common routing script - unified router for the whole pipeline.

Replaces route_convert.py, route_quantize.py and route_benchmark.py. It emits a
single flat schema for every stage:

    {
      "stage": "build" | "benchmark",
      "total_models": N,
      "routes": [ {model, strategy, tool, parameters, reason, ...}, ... ],
      "failed_models": [ {model, reason}, ... ]
    }

`strategy` is the canonical family the generator selects on:
    build   -> optimum | notebook | ovc | mv
    benchmark -> genai | notebook | benchmark_app

Input modes:
    --models-json   HuggingFace/local model list -> Pass 1/2/3 build routes.
                    The same output feeds both the convert and quantize gens.
    --models-dir    A built IR tree (<model>/<precision>/<files>) -> benchmark
                    routes derived from the model files ("route from dir").
    --results-json  convert/quantize results JSON -> benchmark routes.

    --models-json + --models-dir together -> benchmark routes from the dir, but
    filtered to only the models that also appear in models-json (the intersection
    of the two, matched by filesystem-safe model name). Use this to benchmark a
    curated subset of an already-built IR tree instead of everything in it.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from scan_model_conversion_route import analyze_model_id
from analyze_model_conversion_candidates import get_ovc_abled_file
from download_model import download_model, have_openvino_xml
from utils.global_vars import load_global_vars, init_env

init_env()


# Canonical strategy family keyed by the underlying tool.
BUILD_TOOL_STRATEGY = {
    'optimum-cli': 'optimum',
    'notebook': 'notebook',
    'ovc': 'ovc',
    'mv': 'mv',
}
BENCH_TOOL_STRATEGY = {
    'openvino_genai': 'genai',
    'notebook': 'notebook',
    'benchmark_app': 'benchmark_app',
}


# =========================================================================== #
# Shared IR helpers
# =========================================================================== #
def _is_openvino_ir_xml(xml_path: Path) -> bool:
    """Return True if xml_path looks like an OpenVINO IR xml file."""
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return False
    if root.tag.lower() != 'net':
        return False
    child_tags = {child.tag.lower() for child in root}
    return 'layers' in child_tags and 'edges' in child_tags


def _count_ir_xml(model_dir: Path) -> int:
    """Return the number of valid OpenVINO IR xml files directly in model_dir."""
    return sum(1 for xml in model_dir.glob('*.xml') if _is_openvino_ir_xml(xml))


def _find_single_openvino_ir_pair(quantized_path: str) -> Optional[Path]:
    """Return the single OpenVINO IR xml path when the dir has exactly one xml/bin pair."""
    if not quantized_path:
        return None
    model_dir = Path(quantized_path)
    if not model_dir.is_dir():
        return None
    xml_files = sorted(model_dir.glob('*.xml'))
    bin_files = sorted(model_dir.glob('*.bin'))
    if len(xml_files) != 1 or len(bin_files) != 1:
        return None
    if xml_files[0].stem != bin_files[0].stem:
        return None
    if not _is_openvino_ir_xml(xml_files[0]):
        return None
    return xml_files[0]


def _iter_model_units(models_dir: Path) -> List[Tuple[Path, str, str]]:
    """
    Discover units under a models tree laid out as <model_name>/<precision>/<files>.
    A unit is a leaf directory directly containing at least one valid IR xml.
    Returns sorted (leaf_dir, model_name, precision) tuples.
    """
    leaf_dirs = set()
    for xml in models_dir.rglob('*.xml'):
        if xml.is_file() and _is_openvino_ir_xml(xml):
            leaf_dirs.add(xml.parent)

    units: List[Tuple[Path, str, str]] = []
    for leaf_dir in leaf_dirs:
        try:
            rel_parts = leaf_dir.relative_to(models_dir).parts
        except ValueError:
            continue
        if len(rel_parts) == 0:
            model_name, precision = models_dir.name, 'default'
        elif len(rel_parts) == 1:
            model_name, precision = rel_parts[0], 'default'
        else:
            model_name, precision = rel_parts[0], rel_parts[-1]
        units.append((leaf_dir, model_name, precision))

    units.sort(key=lambda item: (item[1], item[2]))
    return units


def _notebook_infer_script_for(model_safe_name: str) -> Optional[str]:
    """Return $DIR_SCRIPTS_NOTEBOOK/<model>/infer.py if it exists."""
    scripts_notebook_dir = load_global_vars().get(
        'DIR_SCRIPTS_NOTEBOOK', '/tmp/skill_env/scripts/notebook')
    infer_script = Path(scripts_notebook_dir) / model_safe_name / 'infer.py'
    return str(infer_script) if infer_script.is_file() else None


# =========================================================================== #
# Build routing (Pass 1/2/3) - from models-json
# =========================================================================== #
@dataclass
class ConversionRoute:
    model: str
    pass_num: int
    strategy: str
    tool: str
    command_template: str
    parameters: Dict
    reason: str
    needs_download: bool
    input_path: Optional[str] = None


def is_huggingface_model_id(model: str) -> bool:
    if model.startswith(('http://', 'https://', '/', '.')):
        return False
    return '/' in model and len(model.split('/')) == 2


def is_github_url(model: str) -> bool:
    return 'github.com' in model.lower()


def extract_model_name_from_path(path: str) -> str:
    """Take the last two path components as the model name."""
    path = path.rstrip('/')
    parts = path.split('/')
    if len(parts) >= 2:
        return '/'.join(parts[-2:])
    elif len(parts) == 1:
        return parts[0]
    return path


def check_optimum_compatible(model_id: str) -> Optional[ConversionRoute]:
    """Pass 1: direct optimum-cli compatibility (HF model IDs)."""
    if not is_huggingface_model_id(model_id):
        return None
    library_name, inferred_task_name, success, version_info = analyze_model_id(model_id)
    if not success:
        return None

    vl_model_indicators = ['vl', '-vision', 'qwen3-vl', 'qwen2.5-vl', 'minicpmv', 'llava',
                           'internvl', 'phi-3-vision', 'phi3-vision', 'cogvlm']
    model_lower = model_id.lower()
    if any(ind in model_lower for ind in vl_model_indicators) and inferred_task_name == "feature-extraction":
        print(f"VL model detected: {model_id}", file=sys.stderr)
        print("Correcting task from 'feature-extraction' to 'image-text-to-text'", file=sys.stderr)
        inferred_task_name = "image-text-to-text"

    return ConversionRoute(
        model=model_id, pass_num=1, strategy="optimum_hf_model_id", tool="optimum-cli",
        command_template="optimum-cli export openvino --model {model} --task {task} --library {library} {output_dir}",
        parameters={"model": model_id, "task": inferred_task_name,
                    "library": library_name, "version_info": version_info},
        reason="HuggingFace model ID detected, can convert directly with optimum-cli",
        needs_download=False)


def route_pass2_notebooks_batch(model_ids: List[str]) -> Dict[str, Optional[ConversionRoute]]:
    """Pass 2: batch-scan notebooks for matching conversion examples."""
    from scan_notebook import scan_notebooks_for_models_batch

    results = {model_id: None for model_id in model_ids}
    global_vars = load_global_vars()
    notebooks_dir = Path(global_vars.get('DIR_NOTEBOOKS', '/tmp/skill_env/notebooks'))
    if not notebooks_dir.exists():
        print(f"Warning: Notebooks directory does not exist: {notebooks_dir}", file=sys.stderr)
        return results

    try:
        scan_results = scan_notebooks_for_models_batch(model_ids, notebooks_dir, min_confidence=0.7)
        for model_id in model_ids:
            matches = scan_results.get(model_id, [])
            if matches:
                best_match = matches[0]
                notebook_path = best_match.notebook_path
                strategy = best_match.conversion_strategy or 'unknown'
                results[model_id] = ConversionRoute(
                    model=model_id, pass_num=2, strategy=f"notebook_{strategy}",
                    tool='notebook',
                    command_template="# See notebook: {notebook_path}",
                    parameters={"notebook_path": str(notebooks_dir / notebook_path), "model": model_id},
                    reason=f"Found conversion example in notebook: {notebook_path}",
                    needs_download=False, input_path=None)
    except Exception as e:
        print(f"Warning: Failed to scan notebooks: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()

    return results


def route_pass3_artifacts(model: str) -> Tuple[Optional[ConversionRoute], Optional[str]]:
    """Pass 3: download + artifact detection (ovc for ONNX, mv for prebuilt IR)."""
    if not is_github_url(model):
        try:
            model_dir = download_model(model)
        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "RepositoryNotFoundError" in error_msg or "Unauthorized" in error_msg:
                return None, f"HuggingFace error (likely invalid model name or requires authentication): {error_msg}"
            return None, f"Download failed: {error_msg}"

        file = get_ovc_abled_file(model_dir)
        if file is not None:
            return ConversionRoute(
                model=model, pass_num=3, strategy="ovc_direct", tool="ovc",
                command_template="ovc {input_path} --output_model {output_dir}",
                parameters={"input_path": str(file)},
                reason="ONNX file detected, use ovc directly",
                needs_download=False, input_path=str(file)), None
        elif have_openvino_xml(model_dir):
            model_name = extract_model_name_from_path(model)
            return ConversionRoute(
                model=model_name, pass_num=3, strategy="mv_directory", tool="mv",
                command_template="mv {input_path} {output_dir}",
                parameters={"input_path": str(model_dir)},
                reason="OpenVINO IR files detected, use mv directly",
                needs_download=False, input_path=str(model_dir)), None

    return None, None


def _passthrough_model_input(target: Dict, model_input: Dict) -> Dict:
    """Copy the model's passthrough config (build, device, ...) onto target."""
    for key, value in model_input.items():
        if key not in ("id", "type"):
            target[key] = value
    return target


def _conversion_route_to_flat(route: ConversionRoute, model_input: Dict) -> Dict:
    """Flatten a ConversionRoute, tagging canonical strategy and passthrough attrs."""
    d = asdict(route)
    d['route_strategy'] = d['strategy']
    d['strategy'] = BUILD_TOOL_STRATEGY.get(route.tool, route.tool)
    return _passthrough_model_input(d, model_input)


def _failed_model_to_flat(model_id: str, reason: str, model_input: Dict) -> Dict:
    """Build a failed_models entry, keeping the model's passthrough config.

    Retaining fields like `build` lets a later forced-download pass reuse the
    originally requested precisions instead of guessing them.
    """
    return _passthrough_model_input({"model": model_id, "reason": reason}, model_input)


def route_build(models: List[Dict[str, str]], skip_pass3: bool = False,
                skip_pass2: bool = False) -> Dict:
    """Route models for the build stage (convert/quantize share this output)."""
    result = {"stage": "build", "total_models": len(models), "routes": [], "failed_models": []}
    model_input_map = {m['id']: m for m in models if 'id' in m}

    pass1_failed = []
    for model_input in models:
        model_id = model_input['id']
        route1 = check_optimum_compatible(model_id)
        if route1:
            result['routes'].append(_conversion_route_to_flat(route1, model_input))
        else:
            pass1_failed.append(model_id)

    if skip_pass2:
        pass2_failed = pass1_failed
        pass1_failed = []

    if pass1_failed:
        print(f"Scanning notebooks for {len(pass1_failed)} models...", file=sys.stderr)
        pass2_routes = route_pass2_notebooks_batch(pass1_failed)
        pass2_failed = []
        for model_id in pass1_failed:
            route2 = pass2_routes.get(model_id)
            mi = model_input_map.get(model_id, {'id': model_id})
            if route2:
                result['routes'].append(_conversion_route_to_flat(route2, mi))
            else:
                pass2_failed.append(model_id)
    else:
        if skip_pass2:
            print("Pass 2 (notebook reference) skipped by user request", file=sys.stderr)
        else:
            print("No models failed Pass 1, skipping Pass 2 (notebook reference)", file=sys.stderr)
            pass2_failed = []

    if skip_pass3:
        for model_id in pass2_failed:
            mi = model_input_map.get(model_id, {'id': model_id})
            result['failed_models'].append(_failed_model_to_flat(
                model_id,
                "Pass 3 (download + artifact detection) skipped by user request", mi))
    else:
        for model_id in pass2_failed:
            mi = model_input_map.get(model_id, {'id': model_id})
            route3, error_msg = route_pass3_artifacts(model_id)
            if route3:
                result['routes'].append(_conversion_route_to_flat(route3, mi))
            else:
                result['failed_models'].append(_failed_model_to_flat(
                    model_id, error_msg or "No compatible conversion route found", mi))

    return result


# =========================================================================== #
# Benchmark routing - from models-dir / results-json
# =========================================================================== #
@dataclass
class BenchmarkRoute:
    model: str
    strategy: str
    tool: str
    command_template: str
    parameters: Dict
    reason: str
    quantized_path: Optional[str] = None
    notebook_infer_script: Optional[str] = None


def _benchmark_route_to_flat(route: BenchmarkRoute) -> Dict:
    d = asdict(route)
    d['route_strategy'] = d['strategy']
    d['strategy'] = BENCH_TOOL_STRATEGY.get(route.tool, route.tool)
    return d


def _model_safe_name(model: str) -> str:
    """Filesystem-safe model name (matches the build stage's output dir naming)."""
    return model.replace('/', '_').replace('.', '_')


def _passthrough_bench_model_inputs(routes: List[Dict], models: List[Dict]) -> None:
    """Carry each model's passthrough config (args, ...) onto its benchmark route.

    The build routes pick this up for free in _conversion_route_to_flat, which
    runs _passthrough_model_input over the model_input. Benchmark routes are
    derived from the IR tree instead (route_benchmark_from_dir), so they never saw
    the model_input at all. Match each route back to its model_input by the same
    filesystem-safe name and copy the same non-id/type fields, so a field like
    `args` reaches gen_wrapper without the routing logic having to know about it.
    """
    by_safe_name = {_model_safe_name(m['id']): m for m in models if m.get('id')}
    for route in routes:
        model_input = by_safe_name.get(_model_safe_name(route.get('model', '')))
        if model_input:
            _passthrough_model_input(route, model_input)


def route_benchmark_from_dir(models_dir: str,
                             allowed_safe_names: Optional[set] = None) -> Dict:
    """
    Derive benchmark routes directly from a built IR tree.

    If allowed_safe_names is provided, only units whose model name (normalized to
    its filesystem-safe form) is in that set are routed; the rest are skipped.
    Requested names that never appear in the tree are reported as failed_models.
    """
    result = {"stage": "benchmark", "total_models": 0, "routes": [], "failed_models": []}

    root = Path(models_dir)
    if not root.is_dir():
        result['failed_models'].append({
            'model': models_dir,
            'reason': f'Models directory does not exist or is not a directory: {models_dir}'})
        return result

    units = _iter_model_units(root)

    if allowed_safe_names is not None:
        found_safe_names = {_model_safe_name(name) for _, name, _ in units}
        units = [u for u in units if _model_safe_name(u[1]) in allowed_safe_names]
        for missing in sorted(allowed_safe_names - found_safe_names):
            result['failed_models'].append({
                'model': missing,
                'reason': 'Requested in models-json but not found in models-dir -> skipped'})

    result['total_models'] = len(units)

    for leaf_dir, model_name, precision in units:
        model_safe_name = model_name.replace('/', '_').replace('.', '_')
        model_label = f"{model_name}/{precision}"
        leaf_str = str(leaf_dir)

        # Rule 1: single OpenVINO IR xml/bin pair -> benchmark_app
        ir_xml_file = _find_single_openvino_ir_pair(leaf_str)
        if ir_xml_file is not None:
            route = BenchmarkRoute(
                model=model_name, strategy='benchmark_app', tool='benchmark_app',
                command_template='benchmark_app -m {model_xml}',
                parameters={'model_xml': str(ir_xml_file), 'quantized_path': leaf_str, 'precision': precision},
                reason='single OpenVINO IR xml/bin pair -> benchmark_app', quantized_path=leaf_str)
            result['routes'].append(_benchmark_route_to_flat(route))
            continue

        if _count_ir_xml(leaf_dir) >= 1:
            # Rule 2: extracted notebook infer.py -> notebook
            infer_script = _notebook_infer_script_for(model_safe_name)
            if infer_script is not None:
                route = BenchmarkRoute(
                    model=model_name, strategy='notebook_infer', tool='notebook',
                    command_template='bash {env_setup} && python {infer_script}',
                    parameters={'quantized_path': leaf_str, 'infer_script': infer_script,
                                'model_safe_name': model_safe_name, 'precision': precision},
                    reason='multi-xml model with extracted notebook infer.py, use notebook infer script',
                    quantized_path=leaf_str, notebook_infer_script=infer_script)
                result['routes'].append(_benchmark_route_to_flat(route))
                continue

            # Rule 3: config.json present -> genai with derived task
            library, task, success, version_info = analyze_model_id(leaf_dir)
            if success:
                route = BenchmarkRoute(
                    model=model_name, strategy='genai', tool='openvino_genai',
                    command_template='python -c "import openvino_genai as ov_genai; ..."',
                    parameters={'model_path': leaf_str, 'prompt': 'What is OpenVINO?',
                                'max_new_tokens': 128, 'task': task, 'library': library,
                                'precision': precision, 'version_info': version_info},
                    reason=f'multi-xml, no notebook script; task inferred from config.json -> {task}',
                    quantized_path=leaf_str)
                result['routes'].append(_benchmark_route_to_flat(route))
                continue

            result['failed_models'].append({
                'model': model_label,
                'reason': 'multi-xml model, no extracted notebook infer.py and no config.json -> skipped'})
            continue

        result['failed_models'].append({
            'model': model_label,
            'reason': f'No valid OpenVINO IR xml found in {leaf_str} -> skipped'})

    return result


def route_benchmark_batch(quantize_results: List[Dict]) -> Dict:
    """Derive benchmark routes from convert/quantize results JSON."""
    result = {"stage": "benchmark", "total_models": len(quantize_results),
              "routes": [], "failed_models": []}

    for model_result in quantize_results:
        if model_result.get('status') != 'success':
            result['failed_models'].append({
                'model': model_result['model'],
                'reason': 'Upstream stage (quantize/convert) failed, cannot benchmark'})
            continue

        quantize_tool = model_result.get('quantize_tool') or model_result.get('convert_tool', '')
        quantize_strategy = model_result.get('quantize_strategy') or model_result.get('convert_strategy', '')
        model_id = model_result['model']
        quantized_path = model_result.get('quantized_path') or model_result.get('ir_path', '')
        task = model_result.get('task', '')

        ir_xml_file = _find_single_openvino_ir_pair(quantized_path)
        if ir_xml_file is not None:
            route = BenchmarkRoute(
                model=model_id, strategy='benchmark_app', tool='benchmark_app',
                command_template='benchmark_app -m {model_xml}',
                parameters={'model_xml': str(ir_xml_file), 'quantized_path': quantized_path},
                reason='quantized_path has exactly one OpenVINO IR xml/bin pair, use benchmark_app',
                quantized_path=quantized_path)
            result['routes'].append(_benchmark_route_to_flat(route))
            continue

        if quantize_tool == 'optimum-cli':
            route = BenchmarkRoute(
                model=model_id, strategy='genai', tool='openvino_genai',
                command_template='python -c "import openvino_genai as ov_genai; ..."',
                parameters={'model_path': quantized_path, 'prompt': 'What is OpenVINO?',
                            'max_new_tokens': 128, 'task': task},
                reason='Model quantized with optimum-cli, use OpenVINO GenAI for text generation benchmark',
                quantized_path=quantized_path)
            result['routes'].append(_benchmark_route_to_flat(route))

        elif quantize_tool == 'notebook' or quantize_strategy == 'notebook_quantize':
            model_safe_name = model_id.replace('/', '_').replace('.', '_')
            notebook_infer_script = model_result.get('infer_script', '')
            if not notebook_infer_script:
                scripts_notebook_dir = load_global_vars().get(
                    'DIR_SCRIPTS_NOTEBOOK', '/tmp/skill_env/scripts/notebook')
                notebook_infer_script = f"{scripts_notebook_dir}/{model_safe_name}/infer.py"
            route = BenchmarkRoute(
                model=model_id, strategy='notebook_infer', tool='notebook',
                command_template='bash {env_setup} && python {infer_script}',
                parameters={'quantized_path': quantized_path, 'infer_script': notebook_infer_script,
                            'model_safe_name': model_safe_name},
                reason='Model quantized with notebook, use extracted infer.py script for benchmark',
                quantized_path=quantized_path, notebook_infer_script=notebook_infer_script)
            result['routes'].append(_benchmark_route_to_flat(route))

        elif quantize_tool == 'nncf':
            if 'text-generation' in task.lower() or 'causal-lm' in task.lower():
                route = BenchmarkRoute(
                    model=model_id, strategy='genai', tool='openvino_genai',
                    command_template='python -c "import openvino_genai as ov_genai; ..."',
                    parameters={'model_path': quantized_path, 'prompt': 'What is OpenVINO?',
                                'max_new_tokens': 128, 'task': task},
                    reason='NNCF quantized text-generation model, use OpenVINO GenAI',
                    quantized_path=quantized_path)
            else:
                route = BenchmarkRoute(
                    model=model_id, strategy='benchmark_app', tool='benchmark_app',
                    command_template='benchmark_app -m {model_xml}',
                    parameters={'model_xml': f"{quantized_path}/openvino_model.xml",
                                'quantized_path': quantized_path},
                    reason='NNCF quantized non-text model, use benchmark_app',
                    quantized_path=quantized_path)
            result['routes'].append(_benchmark_route_to_flat(route))

        else:
            route = BenchmarkRoute(
                model=model_id, strategy='benchmark_app', tool='benchmark_app',
                command_template='benchmark_app -m {model_xml}',
                parameters={'model_xml': f"{quantized_path}/openvino_model.xml",
                            'quantized_path': quantized_path},
                reason='Generic benchmark with benchmark_app',
                quantized_path=quantized_path)
            result['routes'].append(_benchmark_route_to_flat(route))

    return result


# =========================================================================== #
# CLI
# =========================================================================== #
def _text_summary(result: Dict) -> str:
    from collections import Counter
    counts = Counter(r.get('strategy', '?') for r in result['routes'])
    lines = [f"Stage: {result.get('stage')}",
             f"Total models: {result['total_models']}",
             f"Routes: {len(result['routes'])}"]
    for strat, n in sorted(counts.items()):
        lines.append(f"  {strat}: {n}")
    lines.append(f"Failed: {len(result['failed_models'])}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Unified router for convert/quantize/benchmark stages')
    parser.add_argument('--models-json', help='models_input.json -> build routes (Pass 1/2/3); '
                        'combine with --models-dir to benchmark only the intersection')
    parser.add_argument('--models-dir', help='built IR tree -> benchmark routes (route from dir); '
                        'with --models-json, filter to models present in both')
    parser.add_argument('--results-json', help='convert/quantize results JSON -> benchmark routes')
    parser.add_argument('--skip-pass2', action='store_true', help='skip Pass 2 (notebook reference)')
    parser.add_argument('--skip-pass3', action='store_true', help='skip Pass 3 (download + artifacts)')
    parser.add_argument('--format', choices=['json', 'text'], default='json')
    parser.add_argument('--output', help='Output file (default: stdout)')

    args = parser.parse_args()

    if args.results_json and (args.models_json or args.models_dir):
        parser.error('--results-json cannot be combined with --models-json / --models-dir')
    if not (args.models_json or args.models_dir or args.results_json):
        parser.error('Provide --models-json and/or --models-dir, or --results-json')

    if args.models_json and args.models_dir:
        # Intersection mode: benchmark only the models that are both listed in
        # models-json and present in the built IR tree.
        with open(args.models_json) as f:
            models = json.load(f).get('models', [])
        allowed = {_model_safe_name(m['id']) for m in models if 'id' in m}
        result = route_benchmark_from_dir(args.models_dir, allowed_safe_names=allowed)
        # Same passthrough the build routes get, applied to the dir-derived
        # benchmark routes: every non-id/type model_input field (args, ...) lands
        # on the route top-level, where gen_wrapper.py reads it.
        _passthrough_bench_model_inputs(result['routes'], models)
    elif args.models_json:
        with open(args.models_json) as f:
            models = json.load(f).get('models', [])
        result = route_build(models, skip_pass3=args.skip_pass3, skip_pass2=args.skip_pass2)
    elif args.models_dir:
        result = route_benchmark_from_dir(args.models_dir)
    else:
        with open(args.results_json) as f:
            data = json.load(f)
        if 'results' in data:
            models = data['results']
        elif isinstance(data, list):
            models = data
        else:
            models = []
        result = route_benchmark_batch(models)

    output_str = json.dumps(result, indent=2) if args.format == 'json' else _text_summary(result)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_str)
    else:
        print(output_str)


if __name__ == '__main__':
    main()
