# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Render and execute a benchmark pipeline run.
#
# The vendored pipeline is driven by benchmark/templates/run_template.sh, which
# carries two placeholders: the model-selection JSON and the stage to run. We
# substitute those, write the result under the runtime tree (NOT into the vendor
# drop -- BENCH_SRC_ROOT in the template is what lets the rendered script still
# find configs/global_vars.sh), and hand it to the shared single-slot job manager.

import json
import re
import shlex
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from utils.logger import logger

from benchmark.service import env, jobs, sampler

# Pipeline stages accepted by run_template.sh's `opt` switch.
VALID_STAGES = ("build", "benchmark", "all")

# Build precisions the template's routing understands.
VALID_PRECISIONS = ("fp16", "int8", "int4")

# Devices a benchmark case can be run on, in the order the pipeline sweeps them.
# Mirrors benchmark/scripts/generators/gen_wrapper.py's BENCH_DEVICES, which is
# what actually emits the per-device loop; this end validates what may be asked
# for. CPU leads because the report's derived comparisons (speedup vs CPU) are
# defined against it -- dropping it is allowed, but it costs those columns.
VALID_DEVICES = ("CPU", "GPU", "NPU")

# A HuggingFace repo id: owner/name, both restricted to the characters the hub
# actually allows. This is the security boundary for the run request -- the id is
# interpolated into a shell script, so anything outside this set is rejected
# rather than escaped.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")

# The OpenVINO version a benchmark builds its environment column against. Bare
# X.Y.Z: it is interpolated into a venv path and a pip specifier by
# uv_build_envs.sh, so, like the model id, it is validated rather than escaped.
_OV_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Free-form extra arguments the user appends to each benchmark `run_case`
# invocation (gen_wrapper.py). We split them here with the shell's own word rules
# (shlex) so a quoted value like -p "what is openvino" survives as ONE argument,
# and store the token list. gen_wrapper re-quotes every token with shlex.quote
# before it reaches the run script, so no shell metacharacter is ever interpreted
# -- the tokens arrive at the benchmark verbatim as argv. That re-quoting is the
# security boundary, so here we only bound the length and require the quoting to
# balance (an unbalanced quote is a typo, not a run).
_MAX_ARGS_LEN = 512

# Guards a single request from queueing an unbounded amount of GPU-hours.
_MAX_MODELS = 32


class InvalidRunRequest(ValueError):
    """The requested models/stage cannot be turned into a run."""


def normalize_devices(devices: Optional[Sequence[str]], stage: str) -> List[str]:
    """The devices to benchmark on, canonicalised.

    Absent means all of them, which is what the pipeline did before the request
    could carry a choice at all. An empty list is different: the caller named a
    set and it came out empty, and a benchmark of nothing is a mistake worth
    reporting rather than silently turning into a three-device sweep.
    """
    if devices is None:
        return list(VALID_DEVICES)
    if isinstance(devices, str):
        devices = devices.replace(",", " ").split()
    if not isinstance(devices, (list, tuple)):
        raise InvalidRunRequest("devices must be a list")

    wanted = {str(device).strip().upper() for device in devices if str(device).strip()}
    unknown = sorted(wanted - set(VALID_DEVICES))
    if unknown:
        raise InvalidRunRequest(
            f"unknown device(s) {unknown}; expected any of {list(VALID_DEVICES)}"
        )
    # Canonical order, so two requests that differ only in checkbox order render
    # the same sweep -- and so the CPU baseline is measured first.
    ordered = [device for device in VALID_DEVICES if device in wanted]
    if not ordered and stage != "build":
        raise InvalidRunRequest("a benchmark needs at least one device")
    return ordered


def normalize_ov(ov: Optional[str], stage: str) -> Optional[str]:
    """The OpenVINO version to build/run against, validated.

    Required for a benchmark (that stage installs and runs an OpenVINO column);
    ignored for a pure build/download, which only fetches weights with the `hf`
    CLI and never touches OpenVINO. Returns None when it does not apply.
    """
    if stage == "build":
        return None
    version = str(ov or "").strip()
    if not version:
        raise InvalidRunRequest("a benchmark needs an OpenVINO version")
    if not _OV_VERSION_RE.match(version):
        raise InvalidRunRequest(f"invalid OpenVINO version: {version!r} (expected X.Y.Z)")
    return version


def normalize_request(
    models: Sequence[dict], stage: str, devices: Optional[Sequence[str]] = None,
    ov: Optional[str] = None,
) -> Tuple[List[dict], str, List[str], Optional[str]]:
    """Validate and canonicalise a run request.

    Returns the cleaned model entries, the stage, the devices and the OpenVINO
    version, or raises InvalidRunRequest with a message meant for the operator.
    """
    if stage not in VALID_STAGES:
        raise InvalidRunRequest(f"stage must be one of {list(VALID_STAGES)}")
    picked_devices = normalize_devices(devices, stage)
    picked_ov = normalize_ov(ov, stage)
    if not isinstance(models, (list, tuple)) or not models:
        raise InvalidRunRequest("models must be a non-empty list")
    if len(models) > _MAX_MODELS:
        raise InvalidRunRequest(f"at most {_MAX_MODELS} models per run")

    entries: List[dict] = []
    for item in models:
        if not isinstance(item, dict):
            raise InvalidRunRequest("each model entry must be an object")
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            raise InvalidRunRequest("each model entry needs a non-empty id")
        if not _MODEL_ID_RE.match(model_id):
            raise InvalidRunRequest(f"invalid model id: {model_id!r}")

        entry = {"id": model_id, "type": "model_id"}
        requested = item.get("build") or []
        if isinstance(requested, str):
            requested = [p.strip() for p in requested.split(",")]
        unknown = [p for p in requested if p not in VALID_PRECISIONS]
        if unknown:
            raise InvalidRunRequest(
                f"unknown precision(s) {unknown} for {model_id}; "
                f"expected any of {list(VALID_PRECISIONS)}"
            )
        if requested:
            # Deduplicate but keep the canonical fp16 -> int8 -> int4 order, so two
            # requests that differ only in checkbox order render the same script.
            ordered = [p for p in VALID_PRECISIONS if p in requested]
            entry["build"] = ",".join(ordered)

        # Optional free-form extra arguments, appended to every benchmark run_case
        # for this model (see gen_wrapper.py). Kept on the model input JSON entry
        # so route.py can carry it onto the benchmark route; ignored by the build
        # stage, which never runs a case.
        extra = str(item.get("args") or "").strip()
        if extra:
            if len(extra) > _MAX_ARGS_LEN:
                raise InvalidRunRequest(
                    f"args for {model_id} too long ({len(extra)} > {_MAX_ARGS_LEN} chars)"
                )
            try:
                tokens = shlex.split(extra)
            except ValueError as e:
                raise InvalidRunRequest(
                    f"could not parse args for {model_id}: {e} "
                    f"(check for an unbalanced quote)"
                )
            if tokens:
                entry["args"] = tokens
        entries.append(entry)

    if stage in ("build", "all") and not any("build" in e for e in entries):
        raise InvalidRunRequest(
            "the build stage needs at least one precision selected"
        )
    return entries, stage, picked_devices, picked_ov


def render_script(entries: Sequence[dict], stage: str, run_id: str) -> Tuple[Path, str]:
    """Write the rendered run script into the runtime tree.

    Returns (script_path, models_json). The script is written 0700: it is executed
    as root and lives under a directory the pipeline also fills with logs.
    """
    models_json = json.dumps({"models": list(entries)}, indent=2)
    template = env.RUN_TEMPLATE.read_text()
    rendered = (template
                .replace("__MODELS_INPUT_JSON__", models_json)
                .replace("__OPT__", stage))

    runs_dir = env.paths()["runs"]
    runs_dir.mkdir(parents=True, exist_ok=True)
    script_path = runs_dir / f"run_{run_id}.sh"
    script_path.write_text(rendered)
    script_path.chmod(0o700)
    return script_path, models_json


def start_run(
    models: Sequence[dict], stage: str, devices: Optional[Sequence[str]] = None,
    ov: Optional[str] = None,
) -> jobs.Job:
    """Validate, render and launch a pipeline run. Raises InvalidRunRequest,
    jobs.BenchBusy, or RuntimeError when the environment is not ready."""
    # Validate the request before probing the environment: a malformed request is
    # malformed regardless of environment state, and reporting *that* is more
    # useful than telling the user to spend an hour on setup first.
    entries, stage, devices, ov = normalize_request(models, stage, devices, ov)

    status = env.probe()
    if not status["enabled"]:
        raise RuntimeError("the benchmark feature is disabled")
    if not status["ready"]:
        # Failing here (rather than launching a script that dies deep inside
        # optimum-cli) is what makes the "install the environment first" message
        # reach the user.
        raise RuntimeError(
            "the benchmark environment is not ready; run the environment setup first"
        )

    # Reserve the slot before rendering, so two simultaneous requests cannot both
    # write a script and then have one of them rejected with a stale file left
    # behind. jobs.manager.start does the real reservation; this pre-check just
    # gives the caller the busy error before any file is written.
    busy = jobs.manager.current()
    if busy is not None:
        raise jobs.BenchBusy(busy)

    run_id = uuid.uuid4().hex[:12]
    script_path, models_json = render_script(entries, stage, run_id)
    log_path = env.paths()["runs"] / f"run_{run_id}.log"
    header = (
        f"=== benchmark run ===\n"
        f"stage  : {stage}\n"
        f"devices: {' '.join(devices) or '-'}\n"
        f"ov     : {ov or '-'}\n"
        f"script : {script_path}\n"
        f"models :\n{models_json}\n\n"
    )
    logger.info(f"Starting benchmark run: stage={stage}, devices={devices}, "
                f"models={[e['id'] for e in entries]}")

    # Sample the platform for the whole run, and hand the pipeline the path so its
    # aggregation step can slice each case's window out of it. A build-only run
    # measures nothing, so it is not worth the perf descriptors.
    metrics_csv: Optional[Path] = None
    run_sampler: Optional[sampler.RunSampler] = None
    if stage in ("benchmark", "all"):
        candidate = env.paths()["runs"] / f"run_{run_id}_metrics.csv"
        run_sampler = sampler.RunSampler(candidate)
        if run_sampler.start():
            metrics_csv = candidate
        else:
            # start() logged why. The run proceeds unmetered: KPIs still come out.
            run_sampler = None

    # One run name per batch, shared by every model/device wrapper this run spawns
    # (the commons default RUN_NAME to $BENCH_RUN_NAME). Timestamp for readable,
    # time-sorted result directories; run_id appended so two runs in the same
    # second cannot collide and the directory ties back to run_<run_id>.log.
    # Inherited down route -> gen_wrapper -> execute_wrapper -> each case wrapper
    # via the environment, so all cases land in one benchmarks/<backend>/<name>_<DEVICE>/.
    run_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id}"
    extra_env = {"BENCH_RUN_NAME": run_name}
    # Read by gen_wrapper.py when it emits each case wrapper's device loop. Only
    # the benchmark stage has devices to sweep; a build fetches weights, which
    # are the same file whatever runs them.
    if stage in ("benchmark", "all"):
        extra_env["BENCH_DEVICES"] = " ".join(devices)
        # run_template.sh reads this to build (on demand) and activate the chosen
        # OpenVINO column for this run's process. normalize_ov guaranteed it is a
        # valid X.Y.Z for these stages.
        extra_env["BENCH_OV_VERSION"] = ov
    if metrics_csv:
        extra_env["BENCH_METRICS_CSV"] = str(metrics_csv)

    def _stop_sampler(job: jobs.Job) -> None:
        run_sampler.stop()

    try:
        return jobs.manager.start(
            kind="run",
            argv=["bash", str(script_path)],
            cwd=str(env.SRC_ROOT),
            env=env.build_subprocess_env(extra_env),
            log_path=log_path,
            header=header,
            meta={"stage": stage, "models": entries, "devices": devices,
                  "ov": ov, "run_name": run_name,
                  "metrics_csv": str(metrics_csv) if metrics_csv else None},
            on_finish=_stop_sampler if run_sampler is not None else None,
        )
    except Exception:
        # Nothing is going to reap this job, so release the sampler's descriptors
        # here. Lost the race against another request (BenchBusy) or failed to
        # spawn: either way drop the script we just rendered so the runs directory
        # does not accumulate orphans.
        if run_sampler is not None:
            run_sampler.stop()
        script_path.unlink(missing_ok=True)
        raise


def job_view(job: Optional[jobs.Job], offset: Optional[int] = None) -> Optional[dict]:
    """Job status, optionally with the log tail from ``offset``."""
    if job is None:
        return None
    view = job.to_dict()
    if offset is not None:
        view.update(jobs.read_log(job.log_path, offset))
    return view
