# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# REST API for the Benchmark tab, mounted at /bench.
#
# Registering this blueprint on a SmarTune app is all it takes to inherit the
# product's access control: smartune_api.auth_bp installs an app-wide
# before_app_request token gate, so every route here is behind X-Auth-Token and
# TLS without any per-route work. That is the whole reason the upstream project's
# own web server (an unauthenticated stdlib HTTP server on :8001 that would
# Popen("bash", ...) for any caller) is not carried over.

from flask import Blueprint, Response, request, stream_with_context

from utils import quiet_mode
from utils.http_utils import RetCode, construct_response
from utils.logger import logger

from benchmark.service import env, events, jobs, models, results, runner

bench_bp = Blueprint('bench', __name__, url_prefix='/bench')


def benchmark_enabled() -> bool:
    """Whether this deployment serves the benchmark feature. Used by the services
    to decide whether to register the blueprint, and by /smartune/capabilities."""
    try:
        return env.enabled()
    except Exception:
        logger.exception("Failed to resolve benchmark availability")
        return False


def _busy_response(exc: jobs.BenchBusy):
    """409 with the job that holds the slot, so the UI can offer to watch it."""
    return construct_response(
        data={"current": exc.current.to_dict()},
        retcode=RetCode.CONFLICT,
        retmsg=(f"A benchmark {exc.current.kind} is already running. "
                f"Wait for it to finish or cancel it first."),
    )


def _offset_arg():
    """?offset= for incremental log tailing; absent/invalid means "no log wanted"."""
    raw = request.args.get("offset")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


# --- event stream ---------------------------------------------------------

@bench_bp.route('/events', methods=['GET'])
def get_events():
    """Server-sent events for the Benchmark tab: job, log, env, models, results.

    This is what replaces the tab's polling loop. The stream opens with a
    `snapshot` event carrying the whole current state, so a client never needs a
    separate "what is going on" request -- on first load or after a reconnect.

    ``?logs=1`` additionally streams the running job's output as incremental
    [start, end) deltas. The dashboard sets it only while the Benchmark tab is on
    screen: the small events still reach a backgrounded tab (that is how it
    reports a finished run), but a build writes megabytes nobody is reading.

    ``?client=`` identifies the browser tab, so that toggling the above -- which
    reconnects -- replaces that tab's stream instead of stacking a second one on
    top of a connection the server cannot yet tell is dead.

    Note this route is exempt from the X-Auth-Token *header* requirement and
    authenticates via ?token= instead -- EventSource cannot set headers. See
    smartune_api._SSE_TOKEN_PATHS, which must list this path for that to work.
    """
    want_logs = request.args.get("logs") in ("1", "true", "yes")
    # Subscribing here, not inside the generator: a generator body does not run
    # until the WSGI server iterates it, by which point the status line is gone
    # and a refusal could no longer be expressed as one.
    try:
        q = events.subscribe(request.args.get("client") or None)
    except events.TooManyClients as exc:
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR, retmsg=str(exc),
        )

    response = Response(stream_with_context(events.stream(q, with_log=want_logs)),
                        content_type='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    # Tells an intermediary (nginx and friends) not to buffer the stream, which
    # would hold every event until the response ended -- i.e. never.
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    # Belt and braces on the client slot. The generator releases it in its own
    # finally, but that block only runs if the body was ever entered -- a client
    # that vanishes between the response being built and the server iterating it
    # would otherwise hold a slot for the life of the process.
    response.call_on_close(lambda: events.unsubscribe(q))
    return response


# --- environment ----------------------------------------------------------

@bench_bp.route('/env', methods=['GET'])
def get_env():
    """Environment status: venv, model count, whether a run is possible at all."""
    try:
        data = env.probe()
        setup_job = jobs.manager.latest(kind="setup")
        data["setup_job"] = setup_job.to_dict() if setup_job else None
        current = jobs.manager.current()
        data["busy"] = current.to_dict() if current else None
        return construct_response(data=data, retmsg="Successfully retrieved benchmark environment")
    except Exception:
        logger.exception("Failed to probe the benchmark environment")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="Failed to probe the benchmark environment",
        )


@bench_bp.route('/env/setup', methods=['POST'])
def post_env_setup():
    """Install (or, with force, rebuild) the benchmark Python environment.

    A usable environment plus force=false answers CONFLICT rather than silently
    reusing or silently rebuilding: rebuilding costs an hour and tens of GB, so
    the choice belongs to the operator, who gets the existing environment's
    details back to decide with.
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))
    try:
        job = env.start_setup(force=force)
    except env.SetupAlreadyDone as exc:
        return construct_response(
            data={"status": exc.status, "already_installed": True},
            retcode=RetCode.CONFLICT,
            retmsg=("A usable benchmark environment already exists. "
                    "Re-run with force to rebuild it."),
        )
    except jobs.BenchBusy as exc:
        return _busy_response(exc)
    except Exception as e:
        logger.exception("Failed to start the benchmark environment setup")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=f"Failed to start the environment setup: {e}",
        )
    return construct_response(data=job.to_dict(), retmsg="Environment setup started")


@bench_bp.route('/env/ov', methods=['POST'])
def post_env_ov():
    """Switch the active OpenVINO version of the benchmark environment.

    Only relinks the pre-built pool, so it answers inline with the fresh status
    rather than starting a job. A busy slot is a CONFLICT (the same 409 the UI
    already handles for setup/runs), an unknown version an argument error.
    """
    body = request.get_json(silent=True) or {}
    version = str(body.get("version") or "").strip()
    if not version:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="version is required")
    try:
        status = env.switch_ov(version)
    except ValueError as e:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg=str(e))
    except jobs.BenchBusy as exc:
        return _busy_response(exc)
    except Exception as e:
        logger.exception("Failed to switch the OpenVINO version")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=f"Failed to switch the OpenVINO version: {e}",
        )
    return construct_response(data=status, retmsg=f"Switched to OpenVINO {version}")


@bench_bp.route('/env/setup/log', methods=['GET'])
def get_env_setup_log():
    """Incremental log of the most recent setup job."""
    job = jobs.manager.latest(kind="setup")
    if job is None:
        return construct_response(
            data=None, retcode=RetCode.NOT_EXISTING,
            retmsg="No environment setup has been run in this session",
        )
    return construct_response(
        data=runner.job_view(job, offset=_offset_arg() or 0),
        retmsg="Successfully retrieved setup log",
    )


# --- model list -----------------------------------------------------------

@bench_bp.route('/models', methods=['GET'])
def get_models():
    limit_raw = request.args.get("limit", "0")
    try:
        limit = max(0, int(limit_raw))
    except ValueError:
        limit = 0
    data = models.load(search=request.args.get("search"), limit=limit)
    return construct_response(data=data, retmsg="Successfully retrieved model list")


@bench_bp.route('/models/refresh', methods=['POST'])
def post_models_refresh():
    """Rebuild the cached model list.

    A refusal is still a 200: not being able to search is a normal state of the
    machine (no benchmark environment, or a search already in flight), and the
    reason is what the tab shows. Carried in the body as well as the message so
    the UI does not have to parse prose to know which it was.
    """
    reason = models.refresh_async()
    return construct_response(
        data={"started": reason is None, "reason": reason},
        retmsg=reason or "Model list refresh started",
    )


# --- runs -----------------------------------------------------------------

@bench_bp.route('/run', methods=['POST'])
def post_run():
    """Start a pipeline run over the selected models."""
    body = request.get_json(silent=True) or {}
    stage = str(body.get("opt") or body.get("stage") or "all")
    # Absent devices means "every device", which is what this ran before the
    # request could carry a choice -- an older client keeps working unchanged.
    devices = body.get("devices")
    # The OpenVINO version to build/run against, chosen per-run on the Models tab.
    # Required for a benchmark (validated in runner); ignored for a build/download.
    ov = body.get("ov")
    try:
        job = runner.start_run(body.get("models") or [], stage, devices, ov)
    except runner.InvalidRunRequest as e:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg=str(e))
    except jobs.BenchBusy as exc:
        return _busy_response(exc)
    except runner.QuietModeBlocked as exc:
        # Must precede the RuntimeError arm below (it is a subclass). CONFLICT,
        # not ARGUMENT_ERROR: the request is fine, the machine's state is not,
        # and the blockers say exactly which state and where to resolve it.
        return construct_response(
            data={"blocked": True, "blockers": exc.blockers},
            retcode=RetCode.CONFLICT,
            retmsg=str(exc),
        )
    except RuntimeError as e:
        # Environment not ready / feature disabled: actionable, not a server fault.
        return construct_response(retcode=RetCode.NOT_EFFECTIVE, retmsg=str(e))
    except Exception as e:
        logger.exception("Failed to start the benchmark run")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=f"Failed to start the benchmark run: {e}",
        )
    return construct_response(data=job.to_dict(), retmsg="Benchmark run started")


@bench_bp.route('/run', methods=['GET'])
def get_runs():
    """Recent runs plus whichever job currently holds the slot."""
    current = jobs.manager.current()
    return construct_response(
        data={
            "current": current.to_dict() if current else None,
            "recent": jobs.manager.recent(),
        },
        retmsg="Successfully retrieved benchmark runs",
    )


@bench_bp.route('/run/<run_id>', methods=['GET'])
def get_run(run_id):
    """Status of one run, with the log tail when ?offset= is supplied."""
    job = jobs.manager.get(run_id)
    if job is None:
        return construct_response(
            data=None, retcode=RetCode.NOT_EXISTING,
            retmsg=f"Unknown benchmark job: {run_id}",
        )
    return construct_response(
        data=runner.job_view(job, offset=_offset_arg()),
        retmsg="Successfully retrieved benchmark run",
    )


@bench_bp.route('/preflight', methods=['GET'])
def get_preflight():
    """Whether a measured run can start right now, and what is in the way.

    Lets the Models tab say "resolve these two apps first" before the user
    commits to a run, instead of taking the click and returning an error. It is
    advisory only -- ``start_run`` re-checks, because nothing holds the machine's
    state still between this call and that one.
    """
    blockers = quiet_mode.check_blockers()
    return construct_response(
        data={
            "blocked": bool(blockers),
            "blockers": blockers,
            "quiet_mode": quiet_mode.state(),
        },
        retmsg="Successfully retrieved benchmark preflight state",
    )


@bench_bp.route('/quiet_mode', methods=['GET'])
def get_quiet_mode():
    """Current quiet-mode state (whether a run holds it, and whether it is up)."""
    return construct_response(
        data=quiet_mode.state(),
        retmsg="Successfully retrieved quiet mode state",
    )


@bench_bp.route('/quiet_mode', methods=['POST'])
def post_quiet_mode():
    """Raise or drop the gate for the run that currently holds it.

    Dropping it is a deliberate choice with a cost the UI spells out first: full
    monitoring comes back, including the balancer's automatic control, and the
    run's numbers stop being comparable with other runs. Refused when no run
    holds quiet mode -- there is nothing to toggle outside a run, and silently
    accepting would let the UI believe it had changed something.
    """
    body = request.get_json(silent=True) or {}
    if "active" not in body:
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="'active' (bool) is required")
    want = bool(body["active"])

    if not quiet_mode.state().get("held"):
        return construct_response(
            data=quiet_mode.state(), retcode=RetCode.NOT_EFFECTIVE,
            retmsg="No benchmark run currently holds quiet mode",
        )

    quiet_mode.set_gate(want)
    state = quiet_mode.state()
    logger.info(
        "Quiet mode gate %s by user request (owner=%s)",
        "raised" if state.get("active") else "dropped", state.get("owner"),
    )
    return construct_response(
        data=state,
        retmsg=("Quiet mode restored" if state.get("active")
                else "Quiet mode exited; full monitoring resumed"),
    )


@bench_bp.route('/run/metrics/latest', methods=['GET'])
def get_run_metrics_latest():
    """The running run's most recent sampled row.

    A dict lookup, no collection: the row was already produced for metrics.csv.
    This is what the Live tiles read under quiet mode, so that showing them costs
    one read-only poll rather than bringing the 2 s hardware collector back.

    Only CPU / memory / GPU / NPU are present -- the sampler collects no disk or
    network columns, so those tiles have no source here and the UI hides them.
    """
    live = runner.live_sampler()
    row = live.latest() if live is not None else None
    return construct_response(
        data={
            "row": row,
            "sampling": live is not None,
            "period_s": live.period_s if live is not None else None,
            "quiet_mode": quiet_mode.state(),
        },
        retmsg="Successfully retrieved the latest run sample",
    )


@bench_bp.route('/run/<run_id>/cancel', methods=['POST'])
def post_run_cancel(run_id):
    if jobs.manager.cancel(run_id):
        return construct_response(data={"cancelled": True}, retmsg="Cancellation requested")
    return construct_response(
        data={"cancelled": False}, retcode=RetCode.NOT_EFFECTIVE,
        retmsg="That job is unknown or has already finished",
    )


# --- results --------------------------------------------------------------

def _results_view(read, what):
    """Serve one reading of the results tree, validating the backend filter.

    Both views walk the same directories and fail the same ways; only the shape
    of what they return differs.
    """
    backend = request.args.get("backend")
    if backend and backend not in results.BACKENDS:
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg=f"backend must be one of {list(results.BACKENDS)}",
        )
    try:
        data = read(backend)
    except Exception:
        logger.exception(f"Failed to collect {what}")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=f"Failed to collect {what}",
        )
    return construct_response(data=data, retmsg=f"Successfully retrieved {what}")


@bench_bp.route('/results', methods=['GET'])
def get_results():
    return _results_view(results.collect, "benchmark results")


@bench_bp.route('/results/matrix', methods=['GET'])
def get_results_matrix():
    """The same cases as /results, flattened across run directories.

    A run directory holds one device, so comparing devices means reading several
    of them; this view does that join server-side and ships the axes and metric
    descriptors with it.
    """
    return _results_view(results.matrix, "the benchmark comparison matrix")


@bench_bp.route('/results', methods=['DELETE'])
def delete_results():
    """Remove the named cases from the results tree.

    Used by the Benchmark tab when a configuration that has already been
    measured is re-run and the operator says the old measurement should not be
    kept. The cases are named by their directories, which the browser has from
    the matrix it is displaying; results.delete_cases confines every one of them
    to the benchmarks tree before removing anything.
    """
    body = request.get_json(silent=True) or {}
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases:
        return construct_response(
            retcode=RetCode.ARGUMENT_ERROR, retmsg="cases must be a non-empty list",
        )
    try:
        data = results.delete_cases(cases)
    except Exception:
        logger.exception("Failed to delete benchmark results")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR, retmsg="Failed to delete benchmark results",
        )
    # Every open tab is showing the rows that just went away.
    events.publish_results()
    return construct_response(
        data=data,
        retmsg=f"Removed {data['removed']} benchmark case(s)",
    )


@bench_bp.route('/results/log', methods=['GET'])
def get_result_log():
    """Tail of a per-case benchmark.log named by a summary row."""
    path = request.args.get("path")
    if not path:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="path is required")
    content = results.read_case_log(path)
    if content is None:
        return construct_response(
            data=None, retcode=RetCode.NOT_EXISTING,
            retmsg="No such benchmark log",
        )
    return construct_response(data={"path": path, "content": content},
                              retmsg="Successfully retrieved benchmark log")


@bench_bp.route('/results/timeline', methods=['GET'])
def get_result_timeline():
    """The hardware samples taken while one case was being measured.

    The tables report a median per case; this is what that median was taken
    over. NOT_EXISTING covers every honest absence at once -- a case with no
    measurement window, a run whose sampling CSV is gone, an old tree that
    predates in-process sampling -- and the tab says so rather than drawing an
    empty chart.
    """
    case = request.args.get("case")
    if not case:
        return construct_response(retcode=RetCode.ARGUMENT_ERROR, retmsg="case is required")
    try:
        data = results.timeline(case)
    except Exception:
        logger.exception("Failed to read the benchmark case timeline")
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="Failed to read the benchmark case timeline",
        )
    if data is None:
        return construct_response(
            data=None, retcode=RetCode.NOT_EXISTING,
            retmsg="No hardware samples were recorded for this case",
        )
    return construct_response(data=data, retmsg="Successfully retrieved the case timeline")
