# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Benchmark parameter schema, sweep expansion and command rendering.

Shared by the service and gen_wrapper.py, so the two always agree.

Schema layers: A = flags the installed tool accepts (probe_cli_params.py);
B = PARAMS below (shared names, tasks, sweep, widget). 

Per-model config; only the routed tool's section is used. A list of 2+ values
is swept; a `zip` group advances together instead of multiplying:

"""

import copy
import hashlib
import itertools
import json
import re
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

TOOLS = ("genai", "benchmark_app")

# Bounds on one request. A sweep is a product, and 4 params x 4 values is already
# 256 cases per model, precision and device -- hours of machine time per click.
MAX_VALUES_PER_PARAM = 16
MAX_VARIANTS = 64
MAX_STR_LEN = 256

# Where llm_bench resolves a bare prompt-file name from (run_case runs it with
# the llm_bench directory as cwd).
PROMPTS_SUBDIR = "prompts"

# genai task groups, as task_maps.to_genai_task names them.
_TEXT = ["text_gen", "text_gen_chat", "code_gen"]
_VLM = ["visual_text_gen", "visual_text_gen_chat"]
_IMAGE = ["image_gen", "text-to-image", "image-to-image", "inpainting"]
_VIDEO = ["text-to-video", "image-to-video"]

# --------------------------------------------------------------------------- #
# Layer B
# --------------------------------------------------------------------------- #
# Spec keys:
#   flags      every spelling the tool has used; matched against layer A, and
#              flags[0] is the one rendered
#   type       int | float | str | enum | bool | shape | file
#   default    rendered when the user sets nothing; None renders nothing
#   tasks      genai tasks it applies to (None = all)
#   sweep      may take several values
#   short      tag used in the variant directory name
#   source     file type only: the directory the dropdown lists
PARAMS: Dict[str, Dict[str, dict]] = {
    "genai": {
        "num_iters":      dict(flags=["-n", "--num_iters"], type="int", default=2, min=0, max=1000,
                               label="Iterations", short="n"),
        "batch_size":     dict(flags=["-bs", "--batch_size"], type="int", default=1, min=1, max=256,
                               label="Batch size", sweep=True, short="bs"),
        "infer_count":    dict(flags=["-ic", "--infer_count"], type="int", min=1, max=8192,
                               label="Output tokens", tasks=_TEXT + _VLM, sweep=True, short="ic"),
        "prompt_file":    dict(flags=["-pf", "--prompt_file"], type="file", source="prompts",
                               label="Prompt file", sweep=True, short="pf"),
        "prompt":         dict(flags=["-p", "--prompt"], type="str", label="Prompt", sweep=True, short="p"),
        "prompt_index":   dict(flags=["-pi", "--prompt_index"], type="int", min=0, max=10000,
                               label="Prompt index", short="pi"),
        "num_beams":      dict(flags=["--num_beams"], type="int", min=1, max=16, label="Beams",
                               tasks=_TEXT + _VLM, sweep=True, short="beams"),
        "seed":           dict(flags=["-s", "--seed"], type="int", min=0, label="Seed", short="seed"),
        "use_cb":         dict(flags=["--use_cb"], type="bool", label="Continuous batching",
                               tasks=_TEXT + _VLM, sweep=True, short="cb"),
        "apply_chat_template": dict(flags=["--apply_chat_template"], type="bool",
                                    label="Apply chat template", tasks=_TEXT + _VLM, short="chat"),
        "embedding_max_length": dict(flags=["--embedding_max_length"], type="int", min=1, max=32768,
                                     label="Embedding max length", tasks=_TEXT + ["text_embed"],
                                     sweep=True, short="eml"),
        "embedding_pooling": dict(flags=["--embedding_pooling"], type="enum",
                                  choices=["cls", "mean", "last_token"], label="Embedding pooling",
                                  tasks=["text_embed"], short="pool"),
        "texts_file":     dict(flags=["--texts_file"], type="file", source="prompts",
                               label="Rerank texts file", tasks=["text_rerank"], short="tf"),
        "images":         dict(flags=["-i", "--images"], type="str", label="Input image(s)",
                               tasks=_VLM + ["image-to-image", "inpainting", "image_cls",
                                             "ldm_super_resolution", "image-to-video"], short="img"),
        "mask_image":     dict(flags=["-mi", "--mask_image"], type="str", label="Mask image",
                               tasks=["inpainting"], short="mask"),
        "media":          dict(flags=["--media"], type="str", label="Audio file",
                               tasks=["speech_to_text"], short="media"),
        "num_steps":      dict(flags=["--num_steps"], type="int", min=1, max=1000, label="Steps",
                               tasks=_IMAGE + _VIDEO + ["ldm_super_resolution"], sweep=True, short="steps"),
        "height":         dict(flags=["--height"], type="int", min=8, max=8192, label="Height",
                               tasks=_IMAGE + _VIDEO, sweep=True, short="h"),
        "width":          dict(flags=["--width"], type="int", min=8, max=8192, label="Width",
                               tasks=_IMAGE + _VIDEO, sweep=True, short="w"),
        "num_frames":     dict(flags=["--num_frames"], type="int", min=1, max=1024, label="Frames",
                               tasks=_VIDEO, sweep=True, short="frames"),
        "frame_rate":     dict(flags=["--frame_rate"], type="float", min=0.1, max=240, label="Frame rate",
                               tasks=_VIDEO, short="fps"),
    },
    "benchmark_app": {
        "batch_size":     dict(flags=["-b", "--batch_size"], type="int", min=1, max=1024,
                               label="Batch size", sweep=True, short="bs"),
        "shape":          dict(flags=["-shape"], type="shape", label="Input shape",
                               help="e.g. [1,3,224,224] or input[1,3,224,224]", sweep=True, short="shape"),
        "data_shape":     dict(flags=["-data_shape"], type="shape", label="Data shape", sweep=True, short="dshape"),
        "layout":         dict(flags=["-layout"], type="str", label="Layout", short="layout"),
        "hint":           dict(flags=["-hint", "--perf_hint"], type="enum",
                               choices=["throughput", "cumulative_throughput", "latency", "none"],
                               label="Performance hint", sweep=True, short="hint"),
        "nstreams":       dict(flags=["-nstreams", "--number_streams"], type="str",
                               pattern=r"^(\d+|AUTO|[A-Z]+:\d+(,[A-Z]+:\d+)*)$",
                               label="Streams", help="a number, AUTO, or CPU:4,GPU:2", sweep=True, short="ns"),
        "nireq":          dict(flags=["-nireq", "--number_infer_requests"], type="int", min=1, max=1024,
                               label="Infer requests", sweep=True, short="nireq"),
        "nthreads":       dict(flags=["-nthreads", "--number_threads"], type="int", min=1, max=1024,
                               label="Threads", sweep=True, short="nt"),
        "api":            dict(flags=["-api", "--api_type"], type="enum", choices=["sync", "async"],
                               label="API", sweep=True, short="api"),
        "niter":          dict(flags=["-niter", "--number_iterations"], type="int", min=1,
                               label="Iterations", short="niter"),
        "time":           dict(flags=["-t", "--time"], type="int", min=1, max=3600, label="Duration (s)",
                               short="t"),
        "infer_precision": dict(flags=["-infer_precision"], type="str", label="Inference precision",
                                help="e.g. f32, f16, bf16, or CPU:bf16,GPU:f16", sweep=True, short="ip"),
        "pin":            dict(flags=["-pin", "--infer_threads_pinning"], type="enum", choices=["YES", "NO"],
                               label="Thread pinning", short="pin"),
    },
}

# Flags the commons set themselves (model, device, task) or that would write
# somewhere the results tree does not know about. Never offered, never accepted.
RESERVED_DESTS = {
    "genai": {"help", "model", "device", "task", "report", "report_json", "output_dir"},
    "benchmark_app": {"help", "path_to_model", "target_device", "report_folder",
                      "exec_graph_path", "dump_config"},
}

# Pairs where the user's choice of one drops the other's task default: a VLM's
# default is "-p ... -i ...", and a user who picks a prompt file means instead.
_EXCLUSIVE = {"genai": [("prompt", "prompt_file")], "benchmark_app": []}

_INT_TYPES = {"int", "positive_integer", "greater_than_zero", "check_positive", "check_nonneg"}
_SHAPE_RE = re.compile(r"^([\w./:-]*\[[\d,?.\s-]+\])(,[\w./:-]*\[[\d,?.\s-]+\])*$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ParamError(ValueError):
    """A configuration that cannot be run; the message is meant for the operator."""


# --------------------------------------------------------------------------- #
# Merged schema (A x B)
# --------------------------------------------------------------------------- #
def _spec_from_probe(entry: dict) -> dict:
    """A generic spec for a parameter only layer A knows."""
    if entry.get("kind") == "flag":
        type_ = "bool"
    elif entry.get("choices"):
        type_ = "enum"
    elif entry.get("type") in _INT_TYPES:
        type_ = "int"
    elif entry.get("type") == "float":
        type_ = "float"
    else:
        type_ = "str"
    spec = dict(flags=list(entry.get("flags") or []), type=type_, label=entry.get("dest"),
                help=entry.get("help") or "", sweep=True, short=entry.get("dest"), advanced=True)
    if type_ == "enum":
        spec["choices"] = [str(c) for c in entry["choices"]]
    if type_ == "int" and entry.get("type") in {"positive_integer", "greater_than_zero", "check_positive"}:
        spec["min"] = 1
    return spec


def merged_schema(tool: str, probed: Optional[dict] = None) -> Dict[str, dict]:
    """Every parameter the page may offer for `tool`, keyed by name, in order.

    With no probe (the OpenVINO venv is not installed yet) this is layer B alone,
    each entry marked `available: None` -- unknown rather than absent.
    """
    if tool not in PARAMS:
        raise ParamError(f"unknown benchmark tool {tool!r}")
    schema = {name: dict(copy.deepcopy(spec), name=name, available=None)
              for name, spec in PARAMS[tool].items()}
    if not probed:
        return schema

    reserved = RESERVED_DESTS[tool]
    by_flag = {}
    for entry in probed.get("params") or []:
        for flag in entry.get("flags") or []:
            by_flag[flag] = entry
    claimed = set()
    for name, spec in schema.items():
        entry = next((by_flag[f] for f in spec["flags"] if f in by_flag), None)
        spec["available"] = entry is not None
        if entry is not None:
            claimed.add(entry["dest"])
            # Render the spelling the installed tool actually has.
            spec["flags"] = [f for f in spec["flags"] if f in entry["flags"]] or spec["flags"]
            if entry.get("help") and not spec.get("help"):
                spec["help"] = entry["help"]
            _set_tool_default(spec, entry)
    for entry in probed.get("params") or []:
        dest = entry.get("dest")
        if not dest or dest in claimed or dest in reserved or dest in schema:
            continue
        if not _NAME_RE.match(dest):
            continue
        schema[dest] = dict(_spec_from_probe(entry), name=dest, available=True)
        _set_tool_default(schema[dest], entry)
        claimed.add(dest)
    return schema


def _set_tool_default(spec: dict, entry: dict) -> None:
    """What the tool itself uses when the flag is not passed, for the form's help.

    Informational only -- never rendered: a parameter left empty is not on the
    command line at all, and this is the value the tool then falls back to.
    Skipped when argparse's default is no real value (None, "", a suppressed or
    False flag) or one the form could not show (outside the spec's range or
    choices, like benchmark_app's -nireq 0 meaning "automatic").
    """
    value = entry.get("default")
    # Identity for the bool: `0 in (False,)` is true, and 0 is a real default.
    if value is None or value is False or value in ("", "==SUPPRESS==") \
            or isinstance(value, (list, dict)):
        return
    if spec.get("choices") and str(value) not in spec["choices"]:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if spec.get("min") is not None and value < spec["min"]:
            return
        if spec.get("max") is not None and value > spec["max"]:
            return
    spec["tool_default"] = value


def schema_from_extra(tool: str, extra_specs: Optional[dict]) -> Dict[str, dict]:
    """Layer B plus the advanced specs a normalized config carries with it.

    The generator has no probe of its own; runner.py copies the specs of any
    advanced parameter the user set into the config, so this is enough to render.
    """
    schema = merged_schema(tool)
    for name, spec in (extra_specs or {}).items():
        if name not in schema and _NAME_RE.match(name):
            schema[name] = dict(spec, name=name, advanced=True)
    return schema


# --------------------------------------------------------------------------- #
# Prompt files
# --------------------------------------------------------------------------- #
# Which tasks a stock llm_bench prompt file is meant for, by name. A file that
# matches nothing is offered for every task -- unknown, not wrong.
_PROMPT_TASKS = (
    (re.compile(r"^(llama|llm|qwen|phi|mistral|chatglm)", re.I), _TEXT + ["text_embed"]),
    (re.compile(r"^(vlm|llava|minicpm|internvl)", re.I), _VLM),
    (re.compile(r"^stable-diffusion|^flux|^sd", re.I), _IMAGE),
    (re.compile(r"^video", re.I), _VIDEO),
    (re.compile(r"^speech", re.I), ["speech_to_text"]),
    (re.compile(r"^texts_for_rerank", re.I), ["text_rerank"]),
)


def prompt_file_tasks(name: str) -> Optional[List[str]]:
    for pattern, tasks in _PROMPT_TASKS:
        if pattern.search(name):
            return list(tasks)
    return None


# --------------------------------------------------------------------------- #
# Normalisation (service side)
# --------------------------------------------------------------------------- #
def _coerce(spec: dict, name: str, raw):
    """One value, checked and converted to its canonical type."""
    type_ = spec["type"]
    if isinstance(raw, str):
        raw = raw.strip()
    if type_ == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in {"true", "1", "yes", "on"}:
            return True
        if str(raw).lower() in {"false", "0", "no", "off"}:
            return False
        raise ParamError(f"{name}: {raw!r} is not true/false")
    if type_ in {"int", "float"}:
        try:
            value = int(raw) if type_ == "int" else float(raw)
        except (TypeError, ValueError):
            raise ParamError(f"{name}: {raw!r} is not a{'n integer' if type_ == 'int' else ' number'}")
        if isinstance(raw, float) and type_ == "int" and raw != int(raw):
            raise ParamError(f"{name}: {raw!r} is not an integer")
        if spec.get("min") is not None and value < spec["min"]:
            raise ParamError(f"{name}: {value} is below the minimum {spec['min']}")
        if spec.get("max") is not None and value > spec["max"]:
            raise ParamError(f"{name}: {value} is above the maximum {spec['max']}")
        return value
    value = str(raw)
    if not value:
        raise ParamError(f"{name}: empty value")
    if len(value) > MAX_STR_LEN or any(c in value for c in "\n\r\0"):
        raise ParamError(f"{name}: value too long or contains a line break")
    if type_ == "enum" and value not in spec["choices"]:
        raise ParamError(f"{name}: {value!r} is not one of {spec['choices']}")
    if type_ == "shape" and not _SHAPE_RE.match(value.replace(" ", "")):
        raise ParamError(f"{name}: {value!r} is not a shape like [1,3,224,224] or input[1,3,224,224]")
    if type_ == "shape":
        value = value.replace(" ", "")
    if spec.get("pattern") and not re.match(spec["pattern"], value):
        raise ParamError(f"{name}: {value!r} is not valid ({spec.get('help') or spec['pattern']})")
    if type_ == "file":
        # A name picked from the dropdown: no directories, no traversal.
        if "/" in value or value.startswith("."):
            raise ParamError(f"{name}: pick a file from the list, not a path")
    return value


def normalize_config(tool: str, cfg, schema: Dict[str, dict],
                     files: Optional[Dict[str, Iterable[str]]] = None) -> Optional[dict]:
    """Validate one tool section of a model's `bench` config.

    Returns the canonical form -- every value a list, zip groups checked,
    advanced specs attached -- or None when nothing was set. Raises ParamError.
    Task applicability is NOT checked here: the task is only known once the
    router has run, so that check is the generator's (see validate_variant).
    """
    if cfg in (None, {}):
        return None
    if not isinstance(cfg, dict):
        raise ParamError(f"{tool}: configuration must be an object")
    params = cfg.get("params") or {}
    if not isinstance(params, dict):
        raise ParamError(f"{tool}: params must be an object")

    out: Dict[str, list] = {}
    extra_specs: Dict[str, dict] = {}
    for name, raw in params.items():
        spec = schema.get(name)
        if spec is None:
            raise ParamError(f"{tool}: unknown parameter {name!r}")
        if spec.get("available") is False:
            raise ParamError(f"{tool}: {name} is not supported by the installed tool")
        values = raw if isinstance(raw, list) else [raw]
        values = [v for v in values if v is not None and v != ""]
        if not values:
            continue
        if len(values) > MAX_VALUES_PER_PARAM:
            raise ParamError(f"{tool}: {name} has more than {MAX_VALUES_PER_PARAM} values")
        if len(values) > 1 and not spec.get("sweep"):
            raise ParamError(f"{tool}: {name} takes a single value")
        coerced = []
        for v in values:
            c = _coerce(spec, name, v)
            if c not in coerced:
                coerced.append(c)
        if spec["type"] == "file" and files is not None:
            allowed = set(files.get(spec.get("source") or "", ()))
            missing = [v for v in coerced if v not in allowed]
            if missing:
                raise ParamError(f"{tool}: {name} {missing} not found in the {spec.get('source')} list")
        out[name] = coerced
        if spec.get("advanced"):
            extra_specs[name] = {k: spec[k] for k in ("flags", "type", "label", "short", "choices")
                                 if k in spec}

    zips: List[List[str]] = []
    seen_in_zip = set()
    for group in cfg.get("zip") or []:
        if not isinstance(group, list):
            raise ParamError(f"{tool}: each zip group must be a list of parameter names")
        group = [str(g) for g in group if str(g) in out and len(out[str(g)]) > 1]
        if len(group) < 2:
            continue
        if seen_in_zip & set(group):
            raise ParamError(f"{tool}: a parameter can be in only one zip group")
        lengths = {len(out[g]) for g in group}
        if len(lengths) != 1:
            raise ParamError(
                f"{tool}: zipped parameters {group} need the same number of values "
                f"(got {[len(out[g]) for g in group]})")
        seen_in_zip.update(group)
        zips.append(group)

    if not out:
        return None
    result = {"params": out}
    if zips:
        result["zip"] = zips
    if extra_specs:
        result["extra_specs"] = extra_specs
    n = count_variants(result)
    if n > MAX_VARIANTS:
        raise ParamError(f"{tool}: the sweep expands to {n} cases (at most {MAX_VARIANTS})")
    return result


# --------------------------------------------------------------------------- #
# Sweep expansion
# --------------------------------------------------------------------------- #
def _sweep_units(cfg: dict) -> List[List[str]]:
    """The independent axes of a sweep: each zip group is one, every other
    swept parameter is its own."""
    params = cfg.get("params") or {}
    zips = [list(g) for g in cfg.get("zip") or []]
    zipped = {p for g in zips for p in g}
    units = list(zips)
    for name, values in params.items():
        if len(values) > 1 and name not in zipped:
            units.append([name])
    return units


def count_variants(cfg: Optional[dict]) -> int:
    if not cfg:
        return 1
    n = 1
    for unit in _sweep_units(cfg):
        n *= len(cfg["params"][unit[0]])
    return n


def expand(cfg: Optional[dict]) -> List[Tuple[Dict[str, object], Dict[str, object]]]:
    """Every combination as (values, swept): all user-set values, and the subset
    that varies across the sweep."""
    if not cfg:
        return [({}, {})]
    params = cfg["params"]
    fixed = {name: values[0] for name, values in params.items() if len(values) == 1}
    units = _sweep_units(cfg)
    axes = [range(len(params[unit[0]])) for unit in units]
    variants = []
    for picks in itertools.product(*axes):
        swept = {}
        for unit, index in zip(units, picks):
            for name in unit:
                swept[name] = params[name][index]
        variants.append(({**fixed, **swept}, swept))
    return variants


# --------------------------------------------------------------------------- #
# Resolution, validation, rendering (generator side)
# --------------------------------------------------------------------------- #
def resolve(tool: str, schema: Dict[str, dict], task_defaults: Dict[str, object],
            values: Dict[str, object]) -> Dict[str, object]:
    """Full parameter set for one case: schema default < task default < user.

    Ordered by the schema, so the rendered command and the variant hash do not
    depend on the order the page happened to send things in.
    """
    merged: Dict[str, object] = {}
    for name, spec in schema.items():
        if spec.get("default") is not None:
            merged[name] = spec["default"]
    for name, value in (task_defaults or {}).items():
        if value is not None:
            merged[name] = value
    for a, b in _EXCLUSIVE.get(tool, []):
        if a in values and b not in values:
            merged.pop(b, None)
        if b in values and a not in values:
            merged.pop(a, None)
    merged.update(values)
    order = list(schema) + [k for k in merged if k not in schema]
    return {name: merged[name] for name in order if name in merged}


def _shape_batches(shape: str) -> List[str]:
    """First dimension of every shape in a -shape value."""
    return [m.split(",")[0] for m in re.findall(r"\[([^\]]*)\]", shape)]


def _set_shape_batch(shape: str, batch) -> str:
    return re.sub(r"\[([^,\]]*)", lambda m: f"[{batch}", shape)


def validate_variant(tool: str, task: Optional[str], schema: Dict[str, dict],
                     params: Dict[str, object], user_set: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(errors, warnings) for one resolved case. Errors stop the case from
    running unless the operator overrides its command in the plan review."""
    errors: List[str] = []
    warnings: List[str] = []
    user_set = set(user_set)

    for name in sorted(user_set):
        spec = schema.get(name) or {}
        tasks = spec.get("tasks")
        if tool == "genai" and task and tasks and task not in tasks:
            errors.append(f"{name} does not apply to task {task}")

    if tool == "genai":
        if {"prompt", "prompt_file"} <= user_set:
            errors.append("set either prompt or prompt_file, not both")

    if tool == "benchmark_app":
        bs = params.get("batch_size")
        shape = params.get("shape")
        if bs is not None and shape:
            batches = _shape_batches(str(shape))
            if any(b not in (str(bs), "?", "-1") and ".." not in b for b in batches):
                if "shape" in user_set:
                    errors.append(f"shape {shape} has batch {'/'.join(batches)} but batch_size is {bs}; "
                                  f"make them agree or zip them")
        hint = params.get("hint")
        if hint == "latency" and params.get("nstreams") is not None:
            warnings.append("nstreams overrides the latency hint")
        if params.get("niter") is not None and params.get("time") is not None:
            warnings.append("both niter and time are set; benchmark_app stops at whichever comes first")
    return errors, warnings


def fit_auto_shape(params: Dict[str, object], user_set: Iterable[str]) -> Optional[str]:
    """benchmark_app: point an auto-detected shape's batch dimension at the
    batch size the user chose. Returns a warning when it did."""
    user_set = set(user_set)
    if "shape" in user_set or "batch_size" not in user_set or not params.get("shape"):
        return None
    params["shape"] = _set_shape_batch(str(params["shape"]), params["batch_size"])
    return f"auto-detected shape batch set to {params['batch_size']}: {params['shape']}"


def render_args(schema: Dict[str, dict], params: Dict[str, object]) -> List[str]:
    """argv for the resolved parameters (unquoted tokens)."""
    argv: List[str] = []
    for name, value in params.items():
        spec = schema.get(name)
        if spec is None or value is None:
            continue
        flag = spec["flags"][0]
        if spec["type"] == "bool":
            if value:
                argv.append(flag)
            continue
        if spec["type"] == "file" and spec.get("source") == "prompts" and "/" not in str(value):
            value = f"{PROMPTS_SUBDIR}/{value}"
        argv += [flag, str(value)]
    return argv


def managed_flags(schema: Dict[str, dict]) -> set:
    return {f for spec in schema.values() for f in spec["flags"]}


# --------------------------------------------------------------------------- #
# Variant identity
# --------------------------------------------------------------------------- #
def _slug_value(spec: dict, value) -> str:
    if spec.get("type") == "file":
        value = PurePosixPath(str(value)).stem
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).replace("[", "").replace("]", "")
    text = re.sub(r"[,\s]+", "x", text)
    text = re.sub(r"[^A-Za-z0-9.+-]+", "-", text).strip("-")
    return text[:32] or "x"


def variant_id(schema: Dict[str, dict], swept: Dict[str, object],
               params: Dict[str, object], extra_args: Iterable[str] = ()) -> str:
    """Directory name of one case: the swept values, readable, plus a short hash
    of everything that went into it.

    The readable part only names what this sweep varied, so it stays short
    whatever was swept. The hash covers the full resolved set, so two cases that
    agree on the swept values but not on a fixed one never share a directory.
    Nothing reads configuration back out of this name -- case.json does that.
    """
    parts = []
    for name in schema:
        if name in swept:
            spec = schema[name]
            value = swept[name]
            tag = spec.get("short") or name
            sv = _slug_value(spec, value)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            parts.append(f"{tag}{sv}" if numeric else f"{tag}-{sv}")
    slug = "_".join(parts)[:96].rstrip("_-") or "default"
    digest = hashlib.sha1(json.dumps({"params": params, "extra": list(extra_args)},
                                     sort_keys=True, default=str).encode()).hexdigest()[:6]
    return f"{slug}-{digest}"
