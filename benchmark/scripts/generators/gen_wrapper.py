#!/usr/bin/env python3
"""
Common wrapper generator - Generate executable wrapper scripts from routing
results for every stage (build / benchmark).

This reads the unified flat `routes` list emitted by route.py, selects the
routes for the requested (stage, strategy) via a small registry, and dispatches
to a per-strategy command builder.

  - build merges the former convert and quantize stages: every requested weight
    format (fp16/fp32/int8/int4 ...) is produced from a single stage, reading
    the route's `build` field. All build strategies share one shell wrapper
    template (common_command_wrapper.sh.j2). optimum/download handle base and
    quantization precisions with the same command builder; notebook branches
    internally and, for quantization precisions, converts a base IR first if it
    is missing (dependency resolved inside the wrapper itself); ovc/mv produce
    base IR only and nncf quantizes from it.
  - benchmark uses benchmark_command_wrapper.sh.j2.

All GenAI task maps and shared helpers live in task_maps.py.
"""

import argparse
import json
import os
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.global_vars import (
    load_global_vars, get_templates_root, get_scripts_dir, get_ir_dir, get_logs_dir,
)
from utils.python_env_selector import select_venv_for_route
import task_maps as tm
from model_precision_check import probe_candidate_precision

GLOBAL_VARS = load_global_vars()

BUILD_TEMPLATE = 'common_command_wrapper.sh.j2'
BENCH_TEMPLATE = 'benchmark_command_wrapper.sh.j2'

STAGE_TITLES = {'build': 'Build', 'benchmark': 'Benchmark'}

# Weight formats to attempt when a forced-download route carries no `build` field
# (e.g. a model that failed routing entirely, so its requested precisions were
# never recorded). Missing precisions simply fail per-format resolution and are
# skipped with a warning, so a superset here is safe.
DOWNLOAD_FALLBACK_FMT = ['fp16', 'int8', 'int4']


def _is_base_precision(weight_format) -> bool:
    """Full-precision "convert" formats (fp16/fp32) vs quantization formats.

    Notebook/NNCF quantization depends on a base IR built from one of these
    precisions, so the merged build stage uses this to tell the two apart.
    """
    return bool(weight_format) and str(weight_format).lower().startswith('fp')

# Only run expensive XML precision probing when explicitly requested.
ENABLE_DOWNLOAD_PRECISION_CHECK = False


# --------------------------------------------------------------------------- #
# Build result container
# --------------------------------------------------------------------------- #
def _build_result(command: str, output_dir, template: str,
                  context: Dict, meta: Dict) -> Dict:
    return {
        'command': command,
        'output_dir': str(output_dir),
        'template': template,
        'context': context,
        'meta': meta,
    }


# --------------------------------------------------------------------------- #
# Build-stage command builders (convert / quantize)
# --------------------------------------------------------------------------- #
def build_optimum(route, stage, weight_format, env, notebook_inputs):
    """optimum-cli export command (shared by convert and quantize)."""
    model_safe_name = tm.get_model_safe_name(route['model'])
    output_dir = get_ir_dir() / model_safe_name / weight_format
    params = route.get('parameters', {})
    model_ref = params.get('model_id', route['model'])
    task = params.get('task', 'text-generation-with-past')
    library = params.get('library', 'transformers')

    command = (
        f"optimum-cli export openvino \\\n"
        f"    --model {model_ref} \\\n"
        f"    --task {task} \\\n"
        f"    --library {library} \\\n"
        f"    --weight-format {weight_format} \\\n"
        f"    {output_dir}"
    )
    context = {'tool': 'optimum-cli', 'weight_format': weight_format, 'hf_home': GLOBAL_VARS['HF_HOME']}
    return _build_result(command, output_dir, BUILD_TEMPLATE, context, {})


def build_ovc(route, stage, weight_format, env, notebook_inputs):
    """ovc conversion command (convert only)."""
    model_safe_name = Path(route['model']).stem.replace('.', '_')
    output_dir = get_ir_dir() / model_safe_name / weight_format
    input_path = route.get('input_path', route['model'])
    command = f"ovc {input_path} --output_model {output_dir}/openvino_model.xml"
    context = {'tool': 'ovc', 'input_path': input_path, 'hf_home': None}
    return _build_result(command, output_dir, BUILD_TEMPLATE, context, {})


def build_mv(route, stage, weight_format, env, notebook_inputs):
    """mv/copy conversion command (convert only)."""
    model_safe_name = route['model'].replace('/', '_').replace('.', '_').replace('-', '_')
    output_dir = get_ir_dir() / model_safe_name / weight_format
    input_path = route.get('input_path', route['model'])
    command = (
        "# Copy OpenVINO IR files\n"
        f'if [ -d "{input_path}" ]; then\n'
        f"    cp {input_path}/* {output_dir}/ 2>/dev/null || true\n"
        "else\n"
        f'    echo "ERROR: Input path {input_path} does not exist"\n'
        "    exit 1\n"
        "fi"
    )
    context = {'tool': 'mv', 'input_path': input_path, 'hf_home': None}
    return _build_result(command, output_dir, BUILD_TEMPLATE, context, {})


def _resolve_notebook_scripts_dir(route, notebook_inputs, default_dir, action_label):
    """Apply notebook_inputs reuse_from override when present."""
    if notebook_inputs and route['model'] in notebook_inputs.get('models', {}):
        model_info = notebook_inputs['models'][route['model']]
        if model_info.get('action') == 'reuse' and 'reuse_from' in model_info:
            reuse = model_info['reuse_from']
            print(f"  {route['model']}: Reusing {action_label} from {reuse}")
            return reuse
    return default_dir


def build_notebook(route, stage, weight_format, env, notebook_inputs):
    """Notebook-based build.

    For base precisions (fp16/fp32) this is a plain conversion (env_setup.sh +
    convert.py). For quantization precisions it depends on a base IR: the
    wrapper checks whether that IR already exists and, if not, converts it first
    before running quantize.py. The dependency is resolved inside the wrapper
    itself so it stays self-contained regardless of execution order.
    """
    model_safe_name = tm.get_model_safe_name(route['model'])
    output_dir = get_ir_dir() / model_safe_name / weight_format
    notebook_path = route['parameters'].get('notebook_path', '')

    default_dir = str(get_scripts_dir() / 'notebook' / model_safe_name)
    scripts_output_dir = _resolve_notebook_scripts_dir(route, notebook_inputs, default_dir, 'scripts')

    if _is_base_precision(weight_format):
        command = f"""
# goto scripts output dir
cd "{scripts_output_dir}"
# Execute env_setup.sh
echo "[$(date)] Setting up environment..."
bash "{scripts_output_dir}/env_setup.sh"

# Execute convert.py
echo "[$(date)] Running conversion..."
source "{scripts_output_dir}/venv/bin/activate" || source {env.venv_dir}/bin/activate
python "{scripts_output_dir}/convert.py" \\
    --model-id "{route['model']}" \\
    --output "{output_dir}" """
        context = {'tool': 'notebook', 'ir_path': str(output_dir), 'hf_home': GLOBAL_VARS.get('HF_HOME')}
        return _build_result(command, output_dir, BUILD_TEMPLATE, context,
                             {'notebook_path': notebook_path, 'scripts_dir': scripts_output_dir})

    # Quantization precision: depends on a base IR built by convert.py.
    ir_path = _derive_ir_path(route)
    quantize_script = route.get('quantize_script', '') or f"{scripts_output_dir}/quantize.py"
    mode = weight_format if '-' in weight_format else f"{weight_format}-asym"

    command = f"""
# goto scripts output dir
cd "{scripts_output_dir}"
# Ensure environment
echo "[$(date)] Setting up environment..."
bash "{scripts_output_dir}/env_setup.sh"
source "{scripts_output_dir}/venv/bin/activate" || source {env.venv_dir}/bin/activate

# Ensure the base IR exists; convert first if the dependency is missing.
if [ -f "{ir_path}/openvino_model.xml" ] && [ -f "{ir_path}/openvino_model.bin" ]; then
    echo "[$(date)] Reusing existing base IR: {ir_path}"
else
    echo "[$(date)] Base IR missing, running conversion first..."
    python "{scripts_output_dir}/convert.py" \\
        --model-id "{route['model']}" \\
        --output "{ir_path}"
fi

# Execute quantize.py from the base IR
echo "[$(date)] Running quantization..."
python {quantize_script} \\
    --model-id {route['model']} \\
    --mode {mode} \\
    --ov-model-dir {ir_path} \\
    --output {output_dir}"""
    context = {'tool': 'notebook', 'ir_path': ir_path, 'hf_home': GLOBAL_VARS['HF_HOME']}
    return _build_result(command, output_dir, BUILD_TEMPLATE, context,
                         {'notebook_path': notebook_path, 'scripts_dir': scripts_output_dir,
                          'quantize_script': quantize_script})


def _derive_ir_path(route) -> str:
    """Derive the base IR source path (first base precision) for quantization."""
    model_safe_name = tm.get_model_safe_name(route['model'])
    build_fmts = route.get('build', 'fp16')
    if isinstance(build_fmts, str):
        candidates = [v.strip() for v in build_fmts.split(',') if v.strip()]
    elif isinstance(build_fmts, list):
        candidates = [str(v).strip() for v in build_fmts if str(v).strip()]
    else:
        candidates = []
    base_fmt = next((c for c in candidates if _is_base_precision(c)), 'fp16')
    return str(get_ir_dir() / model_safe_name / base_fmt)


def build_nncf_quantize(route, stage, weight_format, env, notebook_inputs):
    """NNCF quantization (placeholder calibration dataset)."""
    model_safe_name = tm.get_model_safe_name(route['model'])
    output_dir = get_ir_dir() / model_safe_name / weight_format
    ir_path = _derive_ir_path(route)
    params = route.get('parameters') or {}
    preset = params.get('preset', 'MIXED')

    command = f"""python -c "
import sys
from pathlib import Path
from openvino.runtime import Core
import nncf
import numpy as np

# Load IR model
core = Core()
model = core.read_model('{ir_path}/openvino_model.xml')

# Calibration dataset (placeholder)
def calibration_dataset():
    # TODO: Replace with actual dataset for your model
    for i in range(300):
        # Generate dummy input matching model input shape
        dummy_input = np.random.randn(1, 128).astype(np.float32)
        yield {{model.input().any_name: dummy_input}}

# Quantize model
quantized_model = nncf.quantize(
    model,
    calibration_dataset(),
    preset=nncf.QuantizationPreset.{preset}
)

# Save quantized model
output_path = Path('{output_dir}')
output_path.mkdir(parents=True, exist_ok=True)
core.serialize(quantized_model, output_path / 'openvino_model.xml')
print(f'Quantized model saved to: {{output_path}}')
" """
    context = {'tool': 'nncf', 'ir_path': ir_path, 'preset': preset, 'hf_home': GLOBAL_VARS['HF_HOME']}
    return _build_result(command, output_dir, BUILD_TEMPLATE, context,
                         {'note': 'NNCF script requires manual calibration dataset implementation'})


def build_download(route, stage, weight_format, env, notebook_inputs):
    """Download pre-converted OpenVINO model from Hugging Face OpenVINO org."""
    model_id = route.get('parameters', {}).get('model_id', route['model'])
    model_safe_name = tm.get_model_safe_name(route['model'])
    output_dir = get_ir_dir() / model_safe_name / f"{weight_format}_ov"
    resolved_model = resolve_openvino_model_id(
        model_id,
        weight_format,
        enable_precision_check=ENABLE_DOWNLOAD_PRECISION_CHECK,
    )
    command = f'hf download "{resolved_model}" --local-dir "{output_dir}"'

    context = {
        'tool': 'download',
        'source_model': model_id,
        'weight_format': weight_format,
        'hf_home': GLOBAL_VARS['HF_HOME'],
    }
    return _build_result(command, output_dir, BUILD_TEMPLATE, context,
                         {'source_model': model_id, 'target_org': 'OpenVINO'})


def resolve_openvino_model_id(model_id: str, weight_format: str,
                              enable_precision_check: bool = False) -> str:
    """Resolve OpenVINO model id by score, optionally with precision probing.

    TODO(smartune): this repeats work already cached. benchmark/service/
    search_models.py enumerates the OpenVINO org and writes the source-model ->
    OpenVINO-repo mapping to models_cache_openvino.json (its precision is parsed
    from the repo name by search_models.precision_of), which the dashboard
    already reads to offer per-model precisions. Consulting that file first would
    make a build deterministic and drop these live searches. Left alone for now
    because this file is part of the upstream vendor drop and re-syncing it must
    stay a plain copy.
    """
    api = _get_hf_api()
    base_model = str(model_id).split('/')[-1].strip()
    fmt = str(weight_format).strip().lower()
    queries = [
        f"OpenVINO/{base_model}-{fmt}-ov",
        f"OpenVINO/{base_model}",
    ]

    def score_candidate(candidate_id: str, base: str, precision: str) -> int:
        cid = candidate_id.lower()
        score = 0
        if cid.startswith('openvino/'):
            score += 8
        if base.lower() in cid:
            score += 4
        if f"{precision}" in cid:
            score += 2
        if cid.endswith('-ov'):
            score += 1
        return score

    scored_candidates = {}

    for query in queries:
        try:
            payload = list(api.list_models(search=query, limit=5))
        except Exception:
            continue

        for item in payload:
            candidate_id = str(getattr(item, 'id', '')).strip()
            if not candidate_id:
                continue
            candidate_score = score_candidate(candidate_id, base_model, fmt)
            prev = scored_candidates.get(candidate_id)
            if prev is None or candidate_score > prev:
                scored_candidates[candidate_id] = candidate_score

    ranked = sorted(scored_candidates.items(), key=lambda x: x[1], reverse=True)

    if not ranked:
        raise RuntimeError(
            f"No OpenVINO model candidate found for source model '{model_id}' "
            f"with precision '{weight_format}'"
        )

    if not enable_precision_check:
        threshold = 13
        for candidate_id, candidate_score in ranked:
            if candidate_score > threshold:
                print(
                    f"  Select by score only: {candidate_id} "
                    f"(score={candidate_score}, threshold>{threshold})"
                )
                return candidate_id
        raise RuntimeError(
            f"No OpenVINO model candidate passed score threshold >13 for "
            f"source model '{model_id}' with precision '{weight_format}'"
        )

    for candidate_id, candidate_score in ranked:
        payload = probe_candidate_precision(candidate_id, fmt)
        ok = bool(payload.get('ok', False))
        reason = str(payload.get('detail', '')) or 'precision checker returned no detail'

        status = 'PASS' if ok else 'FAIL'
        print(f"  Probe {candidate_id} (score={candidate_score}): {status} - {reason}")
        if ok:
            return candidate_id

    raise RuntimeError(
        f"No precision-validated OpenVINO model found for source model '{model_id}' "
        f"with precision '{weight_format}'"
    )


def _get_hf_api():
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise RuntimeError(
            "download strategy requires huggingface_hub. "
            "Please install it (e.g. pip install huggingface_hub)."
        ) from e
    return HfApi()


def build_convert_fallback(route, stage, weight_format, env, notebook_inputs):
    """Dispatch a Pass-3 convert route by its underlying tool."""
    tool = route.get('tool')
    if tool == 'ovc':
        return build_ovc(route, stage, weight_format, env, notebook_inputs)
    if tool == 'mv':
        return build_mv(route, stage, weight_format, env, notebook_inputs)
    if tool == 'notebook':
        return build_notebook(route, stage, weight_format, env, notebook_inputs)
    raise ValueError(f"Unsupported fallback tool '{tool}' for model {route['model']}")


# --------------------------------------------------------------------------- #
# Benchmark-stage command builders
# --------------------------------------------------------------------------- #
def _quantized_path(route) -> str:
    return route.get('quantized_path', route.get('parameters', {}).get('quantized_path', ''))


def _extra_case_args(route) -> str:
    """User-supplied extra run_case arguments for this route, as a shell suffix.

    route.py passes the page's `args` field (already shlex-split into a token list
    by runner.py) straight through onto the route. Each token is re-quoted with
    shlex.quote so it reaches the benchmark as exactly one literal argument, with
    no shell interpretation -- that is why a quoted prompt like -p "what is
    openvino" survives as a single argument. Returns a leading-space-prefixed
    string, or '' when nothing was requested.
    """
    extra = route.get('args') or []
    # Tolerate a plain string too (a hand-written route, or an older payload):
    # split it the same way runner.py would before re-quoting.
    if isinstance(extra, str):
        extra = shlex.split(extra)
    tokens = [shlex.quote(str(t)) for t in extra if str(t)]
    return f" {' '.join(tokens)}" if tokens else ''


# Every device a benchmark case can be run on, in the order the report reads
# them. CPU first because it is the baseline the derived comparisons (speedup vs
# CPU) are defined against.
BENCH_DEVICES = ('CPU', 'GPU', 'NPU')


def _bench_devices() -> List[str]:
    """The devices this run was asked for, or all of them if it said nothing.

    Set by the caller through BENCH_DEVICES (benchmark/service/runner.py exports
    it from the run request). A user who only has a GPU should not spend an hour
    watching the NPU cases fail -- but the default has to stay "everything", so
    that running this generator by hand behaves as it always did.
    """
    raw = os.environ.get('BENCH_DEVICES', '')
    picked = {token.strip().upper() for token in raw.replace(',', ' ').split() if token.strip()}
    return [device for device in BENCH_DEVICES if device in picked] or list(BENCH_DEVICES)


def _device_loop(body: str) -> str:
    """A shell loop that runs `body` once per requested device, with DEVICE set.

    One idiom for all four backends: they used to spell the same three-device
    sweep as three copy-pasted blocks, which is also why adding a fourth device
    (or dropping one) had to be done four times over.
    """
    return (f'    for device in {" ".join(_bench_devices())}; do\n'
            f'        DEVICE=$device\n'
            f'    {body}\n'
            f'    done\n')


def build_genai_benchmark(route, stage, weight_format, env, notebook_inputs):
    model_safe_name = tm.get_model_safe_name(route['model'])
    quantized_path = _quantized_path(route)
    quant = quantized_path.split('/')[-1] if quantized_path else 'unknown_quantized_model'
    task = tm.to_genai_task(route['parameters'].get('task', 'text-generation-with-past'), route['model'])
    opts = tm.genai_task_specific_opt(task, route['parameters'])

    # Free-form extra arguments the user attached to this model on the page, passed
    # through models_input.json -> route parameters. Appended verbatim after the
    # fixed run_case arguments; genai's run_case forwards everything past its first
    # four positionals to the llm_bench command, so these reach the benchmark.
    extra_args = _extra_case_args(route)
    case_command = f'run_case "{model_safe_name}" "{quant}" "{task}" "{quantized_path}" {opts}{extra_args}'
    benchmark_command = f"""
    . {GLOBAL_VARS['DIR_TEMPLATES_ROOT']}/benchmark_genai_common.sh
{_device_loop(case_command)}"""
    context = {'tool': 'openvino_genai', 'benchmark_command': benchmark_command,
               'hf_home': GLOBAL_VARS['HF_HOME']}
    meta = {'metrics_file': str(_bench_scripts_dir('genai') / f"metrics_{model_safe_name}.json"),
            'strategy': 'genai'}
    return _build_result('', quantized_path, BENCH_TEMPLATE, context, meta)


def build_notebook_benchmark(route, stage, weight_format, env, notebook_inputs):
    model_safe_name = tm.get_model_safe_name(route['model'])
    quantized_path = _quantized_path(route)
    quant = quantized_path.split('/')[-1] if quantized_path else 'unknown_quantized_model'
    infer_script = route.get('notebook_infer_script', route['parameters'].get('infer_script', ''))

    default_dir = f"{GLOBAL_VARS.get('DIR_SCRIPTS_NOTEBOOK', '')}/{model_safe_name}"
    script_dir = _resolve_notebook_scripts_dir(route, notebook_inputs, default_dir, 'benchmark scripts')

    case_command = f'run_case "{model_safe_name}" "{quant}" "{quantized_path}" "{script_dir}"'
    benchmark_command = f"""
    . {GLOBAL_VARS['DIR_TEMPLATES_ROOT']}/benchmark_notebook_common.sh
{_device_loop(case_command)}"""
    context = {'tool': 'notebook', 'benchmark_command': benchmark_command,
               'hf_home': GLOBAL_VARS['HF_HOME']}
    meta = {'strategy': 'notebook', 'scripts_dir': script_dir, 'infer_script': infer_script}
    return _build_result('', quantized_path, BENCH_TEMPLATE, context, meta)


def build_benchmark_app(route, stage, weight_format, env, notebook_inputs):
    quantized_path = _quantized_path(route)
    model_xml = route['parameters'].get('model_xml', '')
    if not model_xml:
        qp = Path(quantized_path)
        model_xml = str(qp / 'openvino_model.xml') if (qp / 'openvino_model.xml').exists() \
            else f"{quantized_path}/openvino_model.xml"

    shape_param = ""
    if Path(model_xml).exists():
        shape_spec = analyze_model_input_shapes(model_xml)
        if shape_spec:
            shape_param = f' {shape_spec}'
            print(f"  {route['model']}: Detected dynamic shapes, adding: {shape_spec}")

    case_command = f'run_case "{quantized_path}"{shape_param}'
    benchmark_command = f"""
    . {GLOBAL_VARS['DIR_TEMPLATES_ROOT']}/benchmark_app_common.sh
{_device_loop(case_command)}"""
    context = {'tool': 'benchmark_app', 'benchmark_command': benchmark_command,
               'hf_home': GLOBAL_VARS['HF_HOME']}
    meta = {'strategy': 'benchmark_app', 'model_xml': model_xml}
    return _build_result('', quantized_path, BENCH_TEMPLATE, context, meta)


def _bench_scripts_dir(strategy: str) -> Path:
    return get_scripts_dir() / 'benchmark' / strategy


# --------------------------------------------------------------------------- #
# benchmark_app dynamic-shape inference (moved verbatim from gen_benchmark)
# --------------------------------------------------------------------------- #
def analyze_model_input_shapes(model_xml: str) -> Optional[str]:
    """Analyze an OpenVINO IR XML for dynamic input shapes, return -shape arg or None."""
    try:
        from analysis.infer_xml_input_shapes import infer_shapes
        inferred = infer_shapes(Path(model_xml))
        if inferred.get('status') != 'failed':
            return inferred.get('shape_argument') or None
        print(f"Warning: XML graph shape inference failed for {model_xml}: {inferred.get('error')}",
              file=sys.stderr)
    except Exception as e:
        print(f"Warning: XML graph shape inference unavailable for {model_xml}: {e}", file=sys.stderr)

    try:
        tree = ET.parse(model_xml)
        root = tree.getroot()

        dynamic_inputs = []
        all_params = []

        for layer in root.findall(".//layer[@type='Parameter']"):
            layer_name = layer.get('name', '')
            data_elem = layer.find('data')
            if data_elem is None:
                continue
            element_type = data_elem.get('element_type', '')
            output_port = layer.find('.//output/port')
            if output_port is not None:
                dims = [dim.text for dim in output_port.findall('dim')]
                has_dynamic = any(d in ['-1', '?', '', None] for d in dims)
                all_params.append({'name': layer_name, 'dims': dims,
                                   'has_dynamic': has_dynamic, 'element_type': element_type})

        for param in all_params:
            if not param['has_dynamic']:
                continue
            layer_name = param['name']
            dims = param['dims']
            ndim = len(dims)
            element_type = param['element_type']
            static_indices = [i for i, d in enumerate(dims) if d not in ['-1', '?', '', None]]
            dynamic_indices = [i for i, d in enumerate(dims) if d in ['-1', '?', '', None]]
            static_dims = []
            for i, dim in enumerate(dims):
                if dim not in ['-1', '?', '', None]:
                    static_dims.append(dim)
                else:
                    inferred = infer_dynamic_dimension(
                        layer_name, ndim, i, static_indices, dynamic_indices, element_type, all_params)
                    static_dims.append(str(inferred))
            dynamic_inputs.append(f"{layer_name}[{','.join(static_dims)}]")

        if dynamic_inputs:
            return "-shape " + ",".join(dynamic_inputs)
        return None
    except ET.ParseError as e:
        print(f"Warning: Failed to parse XML {model_xml}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Warning: Error analyzing shapes for {model_xml}: {e}", file=sys.stderr)
        return None


def infer_dynamic_dimension(param_name, ndim, dim_index, static_indices,
                            dynamic_indices, element_type, all_params) -> int:
    """Infer a reasonable value for a dynamic dimension using multi-factor heuristics."""
    name_lower = param_name.lower()

    if dim_index == 0 and len(dynamic_indices) > 1:
        return 1

    sequence_names = ['attention_mask', 'position_ids', 'input_ids', 'decoder_input_ids',
                      'token', 'prompt_token']
    if any(seq_name in name_lower for seq_name in sequence_names):
        if ndim == 1:
            return 1
        elif ndim == 2:
            return 1 if dim_index == 0 else 128
        elif ndim == 3:
            if 'position' in name_lower and dim_index == 0:
                return 1
            return 128
        return 128

    if 'embed' in name_lower or 'hidden' in name_lower:
        if ndim == 2:
            return 1 if dim_index == 0 else 128
        elif ndim == 3:
            return 1 if dim_index == 0 else 128
        return 128

    if 'beam' in name_lower:
        return 1

    if 'len' in name_lower or 'length' in name_lower:
        return 1

    if 'mask' in name_lower:
        if ndim == 2:
            return 1 if dim_index == 0 else 128
        elif ndim == 3:
            return 1 if dim_index == 0 else 128
        return 128

    image_indicators = ['pixel', 'image', 'vision', 'visual']
    if any(ind in name_lower for ind in image_indicators) or name_lower == 'x':
        if ndim == 4:
            if dim_index == 0:
                return 1
            elif dim_index == 1:
                return 3 if len(static_indices) == 0 else 224
            elif dim_index in [2, 3]:
                return 224
        elif ndim == 3:
            return 1 if dim_index == 0 else 128
        return 224

    if name_lower == 'input' or 'input' in name_lower:
        if ndim == 1:
            return 1
        elif ndim == 2:
            return 1 if dim_index == 0 else 128
        elif ndim == 3:
            return 1 if dim_index == 0 else 128
        elif ndim == 4:
            if dim_index == 0:
                return 1
            elif dim_index == 1:
                return 3
            return 224
        return 128

    if 'audio' in name_lower or 'speech' in name_lower or 'feature' in name_lower:
        if ndim <= 2:
            return 1 if dim_index == 0 else 128
        elif ndim == 3:
            return 1 if dim_index == 0 else 128
        return 128

    if 'cond' in name_lower or 'spks' in name_lower or name_lower in ['mu', 't']:
        if ndim == 1:
            return 1
        elif ndim == 2:
            return 1 if dim_index == 0 else 128
        return 128

    if ndim == 1:
        return 1
    elif ndim == 2:
        return 1 if dim_index == 0 else 128
    elif ndim == 3:
        return 1 if dim_index == 0 else 128
    elif ndim == 4:
        if dim_index == 0:
            return 1
        elif dim_index == 1:
            return 3
        return 224
    elif ndim >= 5:
        return 1 if dim_index == 0 else 64
    return 128


# --------------------------------------------------------------------------- #
# (stage, strategy) registry
# --------------------------------------------------------------------------- #
# select: route['strategy'] values consumed. default_fmt: weight-format default
# when the model's convert/quantize field is empty ([] means "generate nothing").
# fmt_kind restricts which precisions a strategy owns in the merged build stage:
#   'all'   - handles every requested precision (optimum/download convert and
#             quantize with the same tool; notebook branches internally)
#   'base'  - only fp16/fp32 conversions (ovc/mv/fallback)
#   'quant' - only quantization precisions, depending on a base IR (nncf)
REGISTRY = {
    ('build', 'optimum'):   dict(select={'optimum'},  build=build_optimum,   default_fmt=[],       fmt_kind='all'),
    ('build', 'notebook'):  dict(select={'notebook'}, build=build_notebook,  default_fmt=['fp16'], fmt_kind='all'),
    ('build', 'ovc'):       dict(select={'ovc'},      build=build_ovc,       default_fmt=['fp16'], fmt_kind='base'),
    ('build', 'mv'):        dict(select={'mv'},       build=build_mv,        default_fmt=['fp16'], fmt_kind='base'),
    ('build', 'fallback'):  dict(select={'ovc', 'mv', 'notebook'}, build=build_convert_fallback, default_fmt=['fp16'], fmt_kind='base'),
    ('build', 'nncf'):      dict(select={'ovc', 'mv'}, build=build_nncf_quantize, default_fmt=['int8'], fmt_kind='quant'),
    ('build', 'download'):  dict(select={'optimum', 'download'}, build=build_download, default_fmt=[], fmt_kind='all'),

    ('benchmark', 'genai'):         dict(select={'genai'},         build=build_genai_benchmark,    default_fmt=[None]),
    ('benchmark', 'notebook'):      dict(select={'notebook'},      build=build_notebook_benchmark, default_fmt=[None]),
    ('benchmark', 'benchmark_app'): dict(select={'benchmark_app'}, build=build_benchmark_app,      default_fmt=[None]),
}


# Ordered gen-strategies that together cover every canonical route strategy the
# router emits, exactly once. --strategy all fans out over these in order; the
# first strategy whose select set matches a route claims it. download/nncf are
# opt-in alternatives, intentionally omitted (run them explicitly instead).
ALL_STRATEGY_ORDER = {
    'build':     ['optimum', 'notebook', 'fallback'],
    'benchmark': ['genai', 'notebook', 'benchmark_app'],
}


def _partition_routes_for_all(routes: List[Dict], stage: str) -> List:
    """Assign each route to exactly one gen-strategy for --strategy all.

    The router gives every route a single canonical strategy, but REGISTRY select
    sets overlap, so we walk ALL_STRATEGY_ORDER and let the first strategy whose
    select set matches claim the route. Returns an ordered list of
    (strategy, [routes]) pairs, skipping strategies with no routes.
    """
    order = ALL_STRATEGY_ORDER[stage]
    buckets = {s: [] for s in order}
    for route in routes:
        route_strategy = route.get('strategy')
        for s in order:
            if route_strategy in REGISTRY[(stage, s)]['select']:
                buckets[s].append(route)
                break
    return [(s, buckets[s]) for s in order if buckets[s]]


def generate_all_wrappers(routes: List[Dict], stage: str,
                          notebook_inputs: Dict = None) -> List[Dict]:
    """Generate wrappers for every strategy of a stage into one metadata list."""
    combined = []
    for strategy, subset in _partition_routes_for_all(routes, stage):
        md = generate_batch_wrappers(subset, stage, strategy, notebook_inputs)
        for m in md:
            m.setdefault('strategy', strategy)  # keep the combined JSON self-describing
        combined.extend(md)
    return combined


def _iter_formats(route, stage, default_fmt, strategy):
    """Yield the weight formats a route requests for a build stage."""
    if stage == 'benchmark':
        yield None
        return
    field = route.get(stage)  # 'build' field on the route (all requested weight formats)
    for weight_format in tm.normalize_formats(field, default_fmt):
        yield weight_format


def generate_batch_wrappers(routes: List[Dict], stage: str, strategy: str,
                            notebook_inputs: Dict = None) -> List[Dict]:
    """Generate wrapper scripts for all routes matching (stage, strategy)."""
    entry = REGISTRY.get((stage, strategy))
    if entry is None:
        print(f"Error: unsupported stage/strategy combination '{stage}/{strategy}'", file=sys.stderr)
        return []

    template_env = Environment(loader=FileSystemLoader(str(get_templates_root())))
    scripts_dir = get_scripts_dir() / stage / strategy
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # download is an opt-in fallback: when requested explicitly, attempt every
    # model in the routes JSON regardless of its canonical route strategy (or
    # whether it was routed at all), rather than only optimum/download routes.
    if strategy == 'download':
        selected = list(routes)
    else:
        selected = [r for r in routes if r.get('strategy') in entry['select']]
    fmt_kind = entry.get('fmt_kind', 'all')
    # Forced-download routes may carry no `build` field (models that failed
    # routing); fall back to a default precision set so they still get scripts.
    default_fmt = DOWNLOAD_FALLBACK_FMT if strategy == 'download' else entry['default_fmt']
    metadata = []

    for i, route in enumerate(selected):
        model_safe_name = tm.get_model_safe_name(route['model'])
        precision = route.get('parameters', {}).get('precision', 'unknown')

        for weight_format in _iter_formats(route, stage, default_fmt, strategy):
            # In the merged build stage each strategy only owns some precisions.
            if fmt_kind == 'base' and not _is_base_precision(weight_format):
                continue
            if fmt_kind == 'quant' and _is_base_precision(weight_format):
                continue

            env = select_venv_for_route(route, GLOBAL_VARS['PYENV_VENV_DIR'])

            if stage == 'benchmark':
                script_name = f"benchmark_{strategy}_{model_safe_name}_{precision}.sh"
            else:
                script_name = f"{stage}_{strategy}_{model_safe_name}_{weight_format}.sh"
            output_path = scripts_dir / script_name

            try:
                result = entry['build'](route, stage, weight_format, env, notebook_inputs)
            except Exception as e:
                print(f"Warning: failed to build wrapper for {route['model']}: {e}", file=sys.stderr)
                continue

            metadata.append(_render_and_write(result, route, stage, output_path, env, template_env))

    return metadata


def _render_and_write(result: Dict, route: Dict, stage: str, output_path: Path,
                      env, template_env: Environment) -> Dict:
    """Render the chosen template with common + builder context and write the script."""
    template = template_env.get_template(result['template'])
    model_safe_name = tm.get_model_safe_name(route['model'])

    context = {
        'model': route['model'],
        'model_safe_name': model_safe_name,
        'venv_dir': env.venv_dir,
        'timestamp': datetime.now().strftime('%Y%m%d_%H%M%S'),
        'output_dir': result['output_dir'],
        'stage': stage,
        'stage_title': STAGE_TITLES.get(stage, stage.title()),
        'command': result['command'],
    }
    context.update(result['context'])

    output_path.write_text(template.render(**context))
    output_path.chmod(0o755)

    meta = {
        'model': route['model'],
        'script_path': str(output_path),
        'output_dir': result['output_dir'],
        'venv_dir': env.venv_dir,
        'venv_source': env.source,
        'selected_transformers_version': env.selected_transformers_version,
        'venv_reason': env.reason,
    }
    meta.update(result['meta'])
    return meta


def main():
    parser = argparse.ArgumentParser(description='Generate wrapper scripts from unified routing results')
    parser.add_argument('--routes-json', required=True,
                        help='Path to unified routing JSON (from route.py)')
    parser.add_argument('--stage', required=True, choices=['build', 'benchmark'],
                        help='Pipeline stage to generate wrappers for (build merges convert+quantize)')
    parser.add_argument('--strategy', required=True,
                        help='Strategy within the stage (e.g. optimum, notebook, nncf, '
                            'ovc, mv, fallback, download, genai, benchmark_app), or "all" to '
                            'script every route of the stage in one pass.')
    parser.add_argument('--notebook-inputs',
                        default=os.path.join(get_logs_dir(), 'notebook_inputs.json'),
                        help='Path to notebook_inputs.json (for notebook script reuse)')
    parser.add_argument('--output-metadata', help='Output file for generated scripts metadata (JSON)')
    parser.add_argument('--enable-precision-check', action='store_true',
                        help='Enable XML precision probe on candidates (slower but safer).')

    args = parser.parse_args()
    global ENABLE_DOWNLOAD_PRECISION_CHECK
    ENABLE_DOWNLOAD_PRECISION_CHECK = bool(args.enable_precision_check)

    if args.strategy != 'all' and (args.stage, args.strategy) not in REGISTRY:
        supported = sorted(s for (st, s) in REGISTRY if st == args.stage)
        parser.error(f"strategy '{args.strategy}' not supported for stage '{args.stage}'. "
                     f"Supported: {', '.join(supported)}, all")

    with open(args.routes_json) as f:
        routes_data = json.load(f)
    routes = routes_data.get('routes', [])

    # For an explicit download run, also attempt models that failed routing
    # entirely: they never made it into `routes`, but the user still wants a
    # download tried for every model in the JSON. route.py passes their config
    # (e.g. the requested `build` precisions) through onto the failed entry, so
    # reuse it here and only fall back to DOWNLOAD_FALLBACK_FMT when absent.
    if args.strategy == 'download':
        for failed in routes_data.get('failed_models', []):
            routes.append({
                **failed,
                'strategy': 'download',
                'route_strategy': 'forced_download',
            })

    notebook_inputs = None
    if args.notebook_inputs and Path(args.notebook_inputs).exists():
        with open(args.notebook_inputs) as f:
            notebook_inputs = json.load(f)

    if args.strategy == 'all':
        metadata = generate_all_wrappers(routes, args.stage, notebook_inputs)
    else:
        metadata = generate_batch_wrappers(routes, args.stage, args.strategy, notebook_inputs)
    result = {
        'stage': args.stage,
        'strategy': args.strategy,
        'generated_scripts': metadata,
        'total_scripts': len(metadata),
        'count': len(metadata),
    }

    output_str = json.dumps(result, indent=2)
    if args.output_metadata:
        with open(args.output_metadata, 'w') as f:
            f.write(output_str)
        print(f"Generated {len(metadata)} wrapper scripts for {args.stage}/{args.strategy}")
        print(f"Metadata written to: {args.output_metadata}")
    else:
        print(output_str)


if __name__ == '__main__':
    main()
