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

from utils import quiet_mode
from utils.logger import logger

from benchmark.service import env, jobs, privilege, sampler

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


class QuietModeBlocked(RuntimeError):
    """Quiet mode cannot be entered, so the run is refused.

    Carries the blockers verbatim so the REST layer can name what is in the way
    (which apps, and where to resolve them) instead of relaying a bare string.
    """

    def __init__(self, blockers: list):
        self.blockers = blockers
        reasons = "; ".join(
            str(b.get("reason") or b.get("name")) for b in blockers
        ) or "unknown"
        super().__init__(
            "cannot enter quiet mode, so the run would not be measurable: " + reasons
        )


# Stages that are actually measured. A build fetches and converts weights -- the
# result is the same file whatever the machine was doing at the time -- so it is
# not worth suspending the operator's monitoring for. Kept identical to the
# stages that get a RunSampler, since quiet mode's lease depends on that thread.
_MEASURED_STAGES = ("benchmark", "all")

# The running run's sampler, or None. Published so the dashboard can read the
# 2 Hz rows it is already collecting: under quiet mode the background collectors
# are stood down, so this is the only live hardware reading in the process, and
# serving it is a dict lookup rather than a second set of hardware queries.
# Single-slot like the job manager -- there is never more than one.
_live_sampler: Optional[sampler.RunSampler] = None


def live_sampler() -> Optional[sampler.RunSampler]:
    """The current run's sampler, or None when no measured run is in flight."""
    return _live_sampler


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

    Required for a benchmark (that stage runs inside the column installed for
    that version); ignored for a pure build/download, which only fetches weights
    with the `hf` CLI and never touches OpenVINO. Returns None when it does not
    apply. Whether that column exists is checked in start_run.
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

    A model entry may carry its own `devices` and `ov`, which is what lets one
    press of Run benchmark two models against two different runtimes. They are
    validated exactly like the request-level ones and default to them, so a
    caller that names neither behaves as it always has. They are kept on the
    entry under underscore-prefixed keys: everything else here is written into
    models_input.json for the pipeline to read, and these two are routing -- how
    to split the run -- rather than input to it.
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

        # This model's own devices and OpenVINO version, defaulting to the
        # request's. Only the benchmark stage has either: a build fetches
        # weights, which are the same file whatever runs them, so both
        # normalizers return the request-level answer (all devices / None) for
        # it and the grouping below collapses to one group.
        entry["_devices"] = (
            normalize_devices(item.get("devices"), stage)
            if item.get("devices") is not None else list(picked_devices)
        )
        entry["_ov"] = (
            normalize_ov(item.get("ov"), stage)
            if item.get("ov") is not None else picked_ov
        )
        entries.append(entry)

    if stage in ("build", "all") and not any("build" in e for e in entries):
        raise InvalidRunRequest(
            "the build stage needs at least one precision selected"
        )
    return entries, stage, picked_devices, picked_ov


def run_groups(entries: Sequence[dict], stage: str) -> List[dict]:
    """Split a request into runs that can share one process.

    The OpenVINO version and the device sweep are process-wide in the pipeline
    -- run_template.sh activates one OV venv for the whole script, and
    gen_wrapper.py reads one BENCH_DEVICES -- so models that disagree about
    either cannot be measured by the same invocation. They are grouped by the
    pair instead, in first-appearance order (so the tile order on the page is
    the order the machine works through), and each group becomes its own
    rendering of the same template.

    A build has neither: it fetches weights with the `hf` CLI and never touches
    OpenVINO or a device, so it is always one group however the entries were
    labelled -- splitting it would only download in several passes.
    """
    groups: List[dict] = []
    index: dict = {}
    for entry in entries:
        devices = list(entry.get("_devices") or [])
        ov = entry.get("_ov")
        key = None if stage == "build" else (ov, tuple(devices))
        group = index.get(key)
        if group is None:
            group = {"ov": ov, "devices": devices, "entries": []}
            index[key] = group
            groups.append(group)
        group["entries"].append(entry)
    return groups


def _pipeline_entry(entry: dict) -> dict:
    """One model as the pipeline reads it, without our routing keys."""
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _group_env(group: dict, stage: str, run_name: str) -> str:
    """The exports that make one rendered script run one group's settings.

    Rendered into the template's __GROUP_ENV__, which sits above everything that
    reads them, so they override whatever the job's own environment carries. The
    values are already validated -- ov against _OV_VERSION_RE, devices against
    VALID_DEVICES, run_name built here -- and quoted on the way out anyway.
    """
    lines = [f"export BENCH_RUN_NAME={shlex.quote(run_name)}"]
    if stage in ("benchmark", "all"):
        lines.append(f"export BENCH_DEVICES={shlex.quote(' '.join(group['devices']))}")
        lines.append(f"export BENCH_OV_VERSION={shlex.quote(group['ov'] or '')}")
        # Read back by benchmark/service/results.py as each row's `ov`. Written
        # for the measured stages only: a build produces no run directory to
        # describe.
        meta = json.dumps({
            "ov": group["ov"],
            "devices": list(group["devices"]),
            "stage": stage,
        }, separators=(",", ":"))
        lines.append(f"export BENCH_RUN_META={shlex.quote(meta)}")
    return "\n".join(lines)


def render_script(
    entries: Sequence[dict], stage: str, run_id: str,
    group_env: str = "", suffix: str = "",
) -> Tuple[Path, str]:
    """Write the rendered run script into the runtime tree.

    Returns (script_path, models_json). The script is written 0700 and handed to
    the user the pipeline runs as: it lives in a directory the pipeline also
    fills with logs, and 0700 owned by root would be a script that user cannot
    read -- which is the whole job.

    `group_env` is the shell prologue pinning this copy's run name, devices and
    OpenVINO version (see _group_env); `suffix` distinguishes the script files
    of a run that has more than one group. Both empty for a single-group run,
    which then renders exactly the script it always did.
    """
    models_json = json.dumps(
        {"models": [_pipeline_entry(entry) for entry in entries]}, indent=2,
    )
    template = env.RUN_TEMPLATE.read_text()
    rendered = (template
                .replace("__MODELS_INPUT_JSON__", models_json)
                .replace("__OPT__", stage)
                .replace("__GROUP_ENV__", group_env))

    runs_dir = privilege.ensure_dir(env.paths()["runs"])
    script_path = runs_dir / f"run_{run_id}{suffix}.sh"
    script_path.write_text(rendered)
    script_path.chmod(0o700)
    privilege.chown(script_path)
    return script_path, models_json


def render_driver(scripts: Sequence[Tuple[Path, dict]], run_id: str) -> Path:
    """Write the script that runs a multi-group run's groups in sequence.

    One job, one log, one quiet-mode window and one sampler over the whole
    sequence -- the alternative was a job per group, and the service has a single
    execution slot and no queue, so that would have been a run the browser had
    to babysit. Each group is a child `bash`, which is what lets it activate its
    own OpenVINO venv without anything having to be switched back.

    A failed group does not stop the ones after it: they are separate
    measurements that happen to have been asked for together, and the operator
    would rather have three of four than one. The last failing group's exit code
    is what the driver returns, so the job is still reported as failed and the
    log says which groups got that far.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by benchmark/service/runner.py -- runs one benchmark job's",
        "# groups in sequence. Each group has its own OpenVINO version and/or",
        "# device sweep, both of which are process-wide in the pipeline.",
        "_rc=0",
    ]
    total = len(scripts)
    for position, (script, group) in enumerate(scripts, start=1):
        devices = " ".join(group["devices"]) or "-"
        models = ", ".join(entry["id"] for entry in group["entries"])
        lines += [
            "",
            'echo ""',
            f'echo "=== group {position}/{total}: OpenVINO {group["ov"] or "-"} '
            f'on {devices} ==="',
            f'echo "=== models: {models}"',
            f"bash {shlex.quote(str(script))} || _rc=$?",
        ]
    lines += [
        "",
        'echo ""',
        f'echo "=== all {total} group(s) finished (exit $_rc) ==="',
        "exit $_rc",
        "",
    ]

    runs_dir = privilege.ensure_dir(env.paths()["runs"])
    driver_path = runs_dir / f"run_{run_id}.sh"
    driver_path.write_text("\n".join(lines))
    driver_path.chmod(0o700)
    privilege.chown(driver_path)
    return driver_path


def start_run(
    models: Sequence[dict], stage: str, devices: Optional[Sequence[str]] = None,
    ov: Optional[str] = None,
) -> jobs.Job:
    """Validate, render and launch a pipeline run. Raises InvalidRunRequest,
    jobs.BenchBusy, QuietModeBlocked, or RuntimeError when the environment is
    not ready."""
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
    # How this request splits: one group per (OpenVINO version, device set), run
    # in sequence inside one job. Decided before the runtime check below, so
    # that check can cover every version the run will actually activate.
    groups = run_groups(entries, stage)

    # The column is installed by setup, not by the run: building it here is a
    # multi-GB pip install inside quiet mode with the sampler already recording.
    # The UI keeps Run disabled until it exists; this is the same rule for
    # anything calling the API directly. Every group's version is checked, not
    # just the request's: a run that would stop halfway through to fail on the
    # second model's runtime should not start.
    for missing in sorted({
        group["ov"] for group in groups
        if group["ov"] is not None and group["ov"] not in status["ov_versions"]
    }):
        raise RuntimeError(
            f"OpenVINO {missing} is not installed; install it from the Benchmark tab's "
            "Environment drawer (or POST /bench/env/setup with that version) "
            "before benchmarking against it"
        )

    # Reserve the slot before rendering, so two simultaneous requests cannot both
    # write a script and then have one of them rejected with a stale file left
    # behind. jobs.manager.start does the real reservation; this pre-check just
    # gives the caller the busy error before any file is written.
    busy = jobs.manager.current()
    if busy is not None:
        raise jobs.BenchBusy(busy)

    # A measured run must start in a quiet environment or not at all: results
    # taken while the balancer might drop a cgroup cap on the process are not
    # comparable with anything. Checked again here even though the UI calls
    # /bench/preflight first -- that check only tells the user in advance, it
    # cannot hold the state still until they click Run.
    if stage in _MEASURED_STAGES:
        blockers = quiet_mode.check_blockers()
        if blockers:
            raise QuietModeBlocked(blockers)

    run_id = uuid.uuid4().hex[:12]

    # One run name per group, shared by every model/device wrapper that group
    # spawns (the commons default RUN_NAME to $BENCH_RUN_NAME). Timestamp for
    # readable, time-sorted result directories; run_id appended so two runs in
    # the same second cannot collide and the directory ties back to
    # run_<run_id>.log. Inherited down route -> gen_wrapper -> execute_wrapper ->
    # each case wrapper via the environment, so a group's cases land in one
    # benchmarks/<backend>/<name>_<DEVICE>/.
    #
    # A run with one group keeps the bare name, which is what every run before
    # groups existed was called. Several groups have to be told apart -- two
    # OpenVINO versions writing into one directory would be two measurements
    # with nothing to distinguish them -- so each takes a "_g<n>" suffix, and
    # the Results tab lists them as the separate jobs they are.
    #
    # Two places read the run id back out of a directory name to find the run's
    # sampling CSV -- results.timeline(), for the per-case curve, and
    # run_template.sh's "restore the hardware medians of an older run" pass --
    # and both have to allow for the suffix. All the groups of one run share a
    # run id, and so a single CSV: they run in sequence under one sampler thread,
    # and each case is cut out of it by timestamp.
    run_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id}"
    single = len(groups) == 1

    # One rendering of the template per group. Nothing is pinned inside the
    # script of a single-group run: it inherits from the job's environment
    # below, exactly as it did before.
    rendered: List[Tuple[Path, dict]] = []
    for position, group in enumerate(groups, start=1):
        group["run_name"] = run_name if single else f"{run_name}_g{position}"
        group["models"] = [entry["id"] for entry in group["entries"]]
        path, models_json = render_script(
            group["entries"], stage, run_id,
            group_env="" if single else _group_env(group, stage, group["run_name"]),
            suffix="" if single else f"_g{position}",
        )
        group["script"] = str(path)
        group["models_json"] = models_json
        rendered.append((path, group))

    script_path = rendered[0][0] if single else render_driver(rendered, run_id)
    log_path = env.paths()["runs"] / f"run_{run_id}.log"
    # privilege.describe() goes into the log the Benchmark tab is already
    # showing: a permission error deep in a vendored script is only quick to
    # read if the paths and the account are stated right above it.
    #
    # The groups are spelled out one by one: with per-model devices and runtimes
    # the request's own devices/ov are only a default, so "what is this run
    # actually going to do" is no longer one line.
    header = (
        f"=== benchmark run ===\n"
        f"stage  : {stage}\n"
        f"script : {script_path}\n"
        + "".join(f"{line}\n" for line in privilege.describe())
        + "".join(
            f"group {position}/{len(groups)}: ov={group['ov'] or '-'} "
            f"devices={' '.join(group['devices']) or '-'} "
            f"name={group['run_name']}\n"
            f"models :\n{group['models_json']}\n\n"
            for position, group in enumerate(groups, start=1)
        )
    )
    logger.info(
        f"Starting benchmark run: stage={stage}, groups="
        f"{[(g['ov'], g['devices'], g['models']) for g in groups]}"
    )

    # Sample the platform for the whole run, and hand the pipeline the path so its
    # aggregation step can slice each case's window out of it. A build-only run
    # measures nothing, so it is not worth the perf descriptors.
    # Quiet mode goes up before the sampler opens its descriptors, so the very
    # first row already describes the environment the pipeline will run in. The
    # owner is the run id rather than the job id because the job does not exist
    # yet -- and the sampler, which renews the lease, is created here too.
    quiet_owner: Optional[str] = None
    if stage in _MEASURED_STAGES:
        quiet_owner = run_id
        quiet_mode.enter(quiet_owner)

    metrics_csv: Optional[Path] = None
    run_sampler: Optional[sampler.RunSampler] = None
    if stage in _MEASURED_STAGES:
        candidate = env.paths()["runs"] / f"run_{run_id}_metrics.csv"
        run_sampler = sampler.RunSampler(candidate, quiet_owner=quiet_owner)
        if run_sampler.start():
            metrics_csv = candidate
        else:
            # start() logged why. The run proceeds unmetered: KPIs still come out.
            run_sampler = None
            # With no sampler thread, nothing renews the lease -- and letting it
            # lapse would lift the gate a couple of minutes into a run that is
            # still being measured, which is worse than not having the lease at
            # all. So this hold gets no expiry and leans on the other two safety
            # nets: the job listener below, and the flag being memory-only.
            quiet_mode.enter(quiet_owner, ttl=float("inf"))
            logger.warning(
                "Benchmark run %s has no metrics sampler; quiet mode is held for "
                "the job's lifetime with no lease backstop.", run_id
            )

    # The job's own environment: what a single-group run reads, and what a
    # multi-group run's group scripts override with their own exports. Written
    # both ways rather than only for the single case, so the values a group does
    # not pin are still the request's rather than absent.
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
        # Recorded into each run directory as run_meta.json; see _group_env,
        # which is where a multi-group run gets its own.
        extra_env["BENCH_RUN_META"] = json.dumps(
            {"ov": ov, "devices": list(devices), "stage": stage},
            separators=(",", ":"),
        )
    if metrics_csv:
        extra_env["BENCH_METRICS_CSV"] = str(metrics_csv)

    global _live_sampler
    _live_sampler = run_sampler

    def _stop_sampler(job: jobs.Job) -> None:
        global _live_sampler
        _live_sampler = None
        run_sampler.stop()

    try:
        return jobs.manager.start(
            kind="run",
            argv=["bash", str(script_path)],
            cwd=str(env.SRC_ROOT),
            env=env.build_subprocess_env(extra_env),
            log_path=log_path,
            header=header,
            meta={"stage": stage,
                  "models": [_pipeline_entry(entry) for entry in entries],
                  "devices": devices,
                  "ov": ov, "run_name": run_name,
                  # What the run actually does, group by group. `devices`/`ov`
                  # above are the request's defaults, which with per-model
                  # settings may be what no group uses.
                  "groups": [
                      {"ov": group["ov"], "devices": group["devices"],
                       "models": group["models"], "run_name": group["run_name"]}
                      for group in groups
                  ],
                  "metrics_csv": str(metrics_csv) if metrics_csv else None,
                  # Which run this job's quiet-mode hold belongs to, so the
                  # listener below can release exactly that hold and not one
                  # taken by a later run.
                  "quiet_owner": quiet_owner,
                  # Whether the hold survived the run untouched. Set on the
                  # terminal event; True here so a run that ends before the
                  # listener fires does not read as dirtied. One bool buys the
                  # ability to answer, afterwards, whether a given result was
                  # measured in a clean environment.
                  "quiet_held": quiet_owner is not None},
            on_finish=_stop_sampler if run_sampler is not None else None,
        )
    except Exception:
        # Nothing is going to reap this job, so release the sampler's descriptors
        # here. Lost the race against another request (BenchBusy) or failed to
        # spawn: either way drop the script we just rendered so the runs directory
        # does not accumulate orphans. Same for the gate -- no job means no
        # terminal event, so this is the only thing that would ever lift it.
        _live_sampler = None
        if run_sampler is not None:
            run_sampler.stop()
        if quiet_owner is not None:
            quiet_mode.exit(quiet_owner)
        # Every script this request wrote, driver included -- for a single group
        # those are the same file, which unlink(missing_ok) handles.
        for path, _ in rendered:
            path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        raise


def _release_quiet_mode(job: jobs.Job) -> None:
    """Lift the gate once a measured run reaches a terminal status.

    Registered as a job listener rather than hooked into ``on_finish`` because
    the listener is called for *every* terminal status by ``jobs._reap`` --
    normal exit, failure and cancel alike -- which is what makes "the gate never
    outlives the run" a property of the job lifecycle instead of something each
    exit path has to remember. ``on_finish`` stays dedicated to the sampler's
    descriptors, which is the one thing that must be released in that specific
    order.
    """
    if job.kind != "run" or job.status == jobs.STATUS_RUNNING:
        return
    owner = (job.meta or {}).get("quiet_owner")
    if not owner:
        return
    # Read before releasing: exit() clears user_exited along with the hold.
    job.meta["quiet_held"] = quiet_mode.held_clean()
    quiet_mode.exit(owner)


jobs.manager.add_listener(_release_quiet_mode)


def job_view(job: Optional[jobs.Job], offset: Optional[int] = None) -> Optional[dict]:
    """Job status, optionally with the log tail from ``offset``."""
    if job is None:
        return None
    view = job.to_dict()
    if offset is not None:
        view.update(jobs.read_log(job.log_path, offset))
    return view
