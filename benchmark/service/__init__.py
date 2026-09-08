# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmarTune's integration layer for the model-benchmark toolchain vendored in the
# parent directory.
#
# The rest of ``benchmark/`` is a read-only vendor drop of an upstream project (a
# shell pipeline plus Python generators that convert, quantize and benchmark
# models on Intel XPUs). Nothing in this package edits it: everything generated at
# runtime -- venvs, downloaded models, IR, logs, rendered run scripts -- lands in
# the runtime root resolved by :mod:`benchmark.service.env`, so re-syncing from
# upstream stays a plain file copy (never a mirror-with-delete, which would take
# this package with it).
#
# Layout:
#   env.py      path resolution, subprocess environment, venv probing, setup_env.sh
#   runner.py   render run_template.sh, execute it, stream its log, cancel it
#   events.py   SSE broker: pushes job/log/env/model/result changes to the tab
#   models.py   model-list cache (wraps search_models.py)
#   search_models.py  enumerate benchmarkable models on HuggingFace via the `hf` CLI
#   results.py  parse the pipeline's report artifacts into API JSON
#   bench_api.py  Flask blueprint mounted at /bench

from benchmark.service.bench_api import bench_bp, benchmark_enabled

__all__ = ["bench_bp", "benchmark_enabled"]
