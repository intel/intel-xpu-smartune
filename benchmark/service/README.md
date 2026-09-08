# Benchmark integration

SmarTune's integration layer for the model-benchmark toolchain vendored in
[`benchmark/`](../), surfaced as the dashboard's **Benchmark** tab.

## Layout

```
benchmark/            read-only vendor drop from the upstream model-benchmark project
  scripts/ templates/ configs/ requirements/   pipeline sources -- do not restructure
  setup_env.sh multi_version.sh                environment installer
  runtime/            all generated state (gitignored): venv, models, ir, logs, runs

  __init__.py         SmarTune-owned: package marker, exports nothing
  service/            SmarTune-owned: the only SmarTune-side code
    env.py       paths, subprocess environment, venv probing, setup_env.sh execution
    jobs.py      single-slot background job execution: log streaming, cancel
    runner.py    render run_template.sh, validate the request, launch it
    models.py    model-list cache (wraps search_models.py)
    search_models.py  enumerate benchmarkable models on HuggingFace via the `hf` CLI
    sampler.py   hardware sampling thread -> the run's metrics CSV
    results.py   join summary.tsv with the medians CSV into API JSON
    bench_api.py Flask blueprint at /bench
```

The vendored parts are treated as read-only: nothing in `service/` writes into
them. All runtime state goes to the runtime root (`benchmark/runtime` by
default), which the vendored scripts pick up through `DIR_ENV_ROOT` --
`env.py` sets `SMARTUNE_BENCH_ENV_ROOT`, and `benchmark/configs/global_vars.sh`
derives every other path from it.

**Re-syncing from upstream** is a plain file copy, never a mirror-with-delete:
`__init__.py` and `service/` are ours and live inside the drop. `service/` also
holds `search_models.py`, which upstream keeps in `benchmark/webui/` next to a
web UI that the dashboard's Benchmark tab replaces -- a re-sync must not restore
that directory.

## Design notes

**One job at a time.** Setup and pipeline runs share a single slot
(`jobs.py`). A benchmark saturates the GPU/NPU, so a concurrent run would
produce meaningless numbers, and a setup that reinstalls the venv underneath a
running pipeline breaks it outright. A second request gets `RetCode.CONFLICT`
with the job that holds the slot.

**Jobs are process groups.** `start_new_session=True` plus `os.killpg` on cancel,
because `optimum-cli`, `ovms` and `pip` all spawn children that would otherwise
survive as orphans holding the device.

**No credentials on disk.** The HF token and proxy reach the pipeline through the
subprocess environment only (`env.build_subprocess_env`). Rendered run scripts
under `runtime/runs/` contain no secrets.

**Auth comes for free.** Registering `bench_bp` on a SmarTune app puts every route
behind `smartune_api.auth_bp`'s app-wide `X-Auth-Token` gate and the service's TLS.
This is why the upstream project's own web server -- an unauthenticated stdlib
HTTP server on `:8001` that would `Popen("bash", ...)` for any caller -- is not
carried over.

## Configuration

The `benchmark:` block in `config/config.yaml`. Every key is optional; see the
comments there. `enabled: false` (or an absent `benchmark/` directory) leaves the
blueprint unregistered and hides the tab.

Both service entry points mount it through `features.mount_benchmark`,
which imports this package behind a guard: a deployment that ships without
`benchmark/` starts normally with the tab hidden, while any *other* import failure
(a missing third-party package, a typo in here) still propagates rather than
quietly turning into a missing feature.

## Packaging

The full `.deb` (`build_deb.sh --full`) ships `benchmark/` — about 800 KB, since
`runtime/` is excluded at staging time rather than copied and deleted (it is
routinely tens of GB on a machine that has run a benchmark). Installed, the tree
lands at `/opt/intel/smartune/benchmark`, so the env root resolves to
`/opt/intel/smartune/benchmark/runtime` with no configuration: the packaged
layout and a source checkout are identical from `env.py`'s point of view.

`setup_env.sh` clones two repositories and downloads OVMS, hence the `git` and
`curl` dependencies in `control.full`. `apt remove` keeps `runtime/` (hours of
downloads); `apt purge` removes the whole install dir and says how much it freed.

The monitor-only `.deb` does not ship `benchmark/` at all, and the guard above is
what lets that variant start with the tab simply absent.

## API

All routes require `X-Auth-Token`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/bench/env` | environment status, current job |
| POST | `/bench/env/setup` | install the environment; `{"force": true}` to rebuild |
| GET | `/bench/env/setup/log?offset=` | setup log tail |
| GET | `/bench/models?search=&limit=` | cached model list |
| POST | `/bench/models/refresh` | re-run the HuggingFace search |
| POST | `/bench/run` | `{"models":[{"id","build":[...]}],"opt":"build\|benchmark\|all"}` |
| GET | `/bench/run` | current + recent jobs |
| GET | `/bench/run/<id>?offset=` | job status, with log tail when offset is given |
| POST | `/bench/run/<id>/cancel` | cancel |
| GET | `/bench/results?backend=` | `summary.tsv` rows plus each case's KPIs and medians |
| GET | `/bench/results/log?path=` | per-case `benchmark.log` tail |

`GET /smartune/capabilities` reports `benchmark: 1` when the blueprint is mounted.
That says the feature exists, NOT that its environment is installed -- the tab has
to exist in order to offer the install action. Readiness is `GET /bench/env`.`ready`.

## Metrics

Upstream collected metrics with `metrics/metrics_collect.sh`: five `sudo`
background samplers (PTAT, turbostat, a patched `intel-npu-smi`, `gpu_monitor.py`,
IGT's `xe_perf`) started per case and torn down with a pattern-matched `kill -9`.
That tree is not vendored. SmarTune samples the same platform in-process instead,
reusing the collectors the System Overview already runs:

```
runner.py  start_run()
  └─ sampler.RunSampler  ->  runtime/runs/run_<id>_metrics.csv   (one row / 0.5 s)
       monitor/metrics/gpu_perf.create_independent_sampler()  GPU power/freq/engines
       monitor/metrics/cpu.py       P/E core usage+freq, package power/temp, memory
       monitor/metrics/npu.py       NPU utilisation/power/freq/bandwidth/temp
       monitor/metrics/membw.py     system memory bandwidth (uncore IMC, perf_event)
  └─ BENCH_METRICS_CSV -> the run script
        run_with_metrics() in templates/benchmark_*_common.sh
          tees the case output to <case_dir>/detail.log and marks [begin, end]
        scripts/analysis/export_windowed_metric_medians.py --metrics-csv
          KPIs from detail.log + medians of the rows inside each case's window
          -> <run_dir>/windowed_metric_medians.csv -> pivot_report.py
```

The sampler owns a **private** `GPUMonitor` and its own `psutil.cpu_times()`
baseline rather than calling `gpu_perf.get_gpu_usage_output()` /
`cpu.get_cpu_dynamic()`. Those keep process-wide delta state for the dashboard's
pollers; a second caller at 2 Hz would consume the baseline the dashboard is about
to read. Utilisation would merely get noisy, but GPU power is an energy counter
differenced over `dt`, so a microsecond gap turns one counter tick into thousands
of watts.

Two column families upstream had are gone, because their sources are not portable:
`xe_*` (EU-level counters, needs a patched IGT built from Intel-internal sources)
and PTAT's PL1/PL2 and per-core IPC. Package power, P/E core frequency and package
temperature are covered by `monitor/metrics/cpu.py` and RAPL, so the report keeps
its perf/watt and thermal-headroom analysis.

`benchmark/scripts/analysis/export_windowed_metric_medians.py` is the one file in
the vendored tree that has diverged from upstream (it reads one run-level CSV
instead of five per-case ones). **Re-syncing it is a manual merge, not a copy.**
Its KPI parsers track upstream `llm_bench` output formats, so it does need to
follow upstream over time.

## Current limitations

- Benchmark results are not persisted to SmarTune's database or shown in History.
- `benchmark/requirements/download.requirements.txt` is never installed --
  `setup_env.sh:123` has that `pip install` commented out. Left as found; confirm
  with upstream whether that is deliberate.
- The deb packaging does not ship the runtime environment; setup is on-demand.
