# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Optional-component assembly for the SmarTune services.
#
# Not every deployment ships every part of the product: benchmark/ is a
# multi-GB vendor drop that a monitor-only install leaves out entirely. It is
# mounted onto the Flask app the same way from both service entry points, so
# the "is it here, and if so wire it up" logic lives in one module rather than
# being duplicated in balancer/balance_service.py and monitor/monitor_service.py.
#
# This is the root of the tree on purpose. Deciding whether an optional package
# is present has to happen from OUTSIDE that package -- a guard living in
# benchmark/ would have to be imported to answer "can benchmark/ be imported" --
# and it cannot live in smartune.py either, which is only a launcher and never
# sees the Flask app (balance_service importing it back would be a cycle).

from utils.logger import logger

# --- benchmark ------------------------------------------------------------
#
# The benchmark toolchain lives in benchmark/ -- a multi-GB vendor drop plus
# SmarTune's benchmark.service package -- and not every deployment ships it. A
# top-level `from benchmark.service import ...` in the service modules would turn
# that from "the Benchmark tab is hidden" into "the process will not start", so
# the import happens here, once, behind a guard.

# Import errors naming exactly these mean "not deployed here". Anything deeper
# (benchmark.service.foo, or a third-party package the blueprint needs) is a real
# breakage, and silently degrading it to a missing tab would hide the bug.
_BENCHMARK_ABSENT = {"benchmark", "benchmark.service"}


def mount_benchmark(app) -> bool:
    """Register the /bench blueprint on ``app``. Returns whether it is available.

    False means the tab should be hidden: either benchmark/ is not part of this
    deployment, or ``benchmark.enabled`` is off in config.yaml, or the vendored
    pipeline is incomplete (benchmark.service.env.enabled checks for the run
    template). The caller passes the result to smartune_api.set_benchmark_available
    so /smartune/capabilities reports it to the dashboard.
    """
    try:
        from benchmark.service import bench_bp, benchmark_enabled, models
    except ImportError as exc:
        if getattr(exc, "name", None) not in _BENCHMARK_ABSENT:
            raise
        logger.info("Benchmark feature not present in this deployment; /bench not mounted.")
        return False

    if not benchmark_enabled():
        logger.info("Benchmark feature disabled or incomplete; /bench not mounted.")
        return False

    app.register_blueprint(bench_bp)
    # Enumerating the benchmarkable models takes minutes, which is unbearable to
    # discover on first use, so a missing or stale list is rebuilt now, in the
    # background. Returns immediately and declines to do anything when there is no
    # `hf` CLI or the cache is fresh -- see models.maybe_prefetch.
    try:
        models.maybe_prefetch()
    except Exception:
        # The tab works off whatever cache exists (and a manual refresh button);
        # it is not worth failing service startup over.
        logger.exception("Benchmark model prefetch could not be started")
    return True
