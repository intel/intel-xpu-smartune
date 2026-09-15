# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Hardware sampling for the duration of a benchmark run.
#
# The vendored pipeline used to shell out to metrics/metrics_collect.sh, which
# launched five `sudo` background samplers (turbostat, its own copy of
# gpu_monitor.py, intel-npu-smi, a memory monitor, and an IGT perf recorder) and
# tore them down with `ps aux | grep <pattern> | xargs sudo kill -9`. That pattern
# kill would have reached SmarTune's own processes, and four of the five samplers
# duplicated collectors this repo already has. So the sampling moved in here: one
# thread, no subprocesses, no sudo, reusing monitor/metrics.
#
# Output is ONE run-level CSV -- not the per-case files upstream wrote -- because
# the analysis step slices it by timestamp anyway (see
# benchmark/scripts/analysis/export_windowed_metric_medians.py). Timestamps are
# epoch seconds to match the `PIPELINE TIME: ... begin:/end:` markers the pipeline
# writes into each case's detail.log; the upstream memory monitor recorded
# perf_counter values instead, which never fell inside a window and so silently
# contributed nothing.
#
# Rows are flushed as they are written: the pipeline runs the aggregation step at
# the end of the same script this sampler is timing, so the file is read while the
# thread is still appending to it.

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from monitor.metrics import cpu as cpu_metrics
from monitor.metrics import gpu_perf, membw, npu as npu_metrics
from utils import quiet_mode
from utils.logger import logger

from benchmark.service import privilege

# 2 Hz. npu._collect_npu_smi_once() sleeps 200ms internally to difference the
# energy counter, so a tick costs ~250ms; going faster would just chain NPU reads
# back to back. Cases run for tens of seconds, so a median over 2 Hz samples is
# plenty, and the load the sampler itself adds stays negligible.
DEFAULT_PERIOD_S = 0.5

# CSV schema. Kept flat and explicit so the analysis script's column map can be
# read against it, and so a missing collector shows up as an empty cell rather
# than shifting every column after it.
COLUMNS = (
    "timestamp_s",
    # CPU
    "cpu_usage_pct",
    "cpu_p_core_usage_pct",
    "cpu_e_core_usage_pct",
    "cpu_p_core_freq_mhz",
    "cpu_e_core_freq_mhz",
    "cpu_package_power_w",
    "cpu_package_temp_c",
    "cpu_package_tjmax_c",
    # Memory
    "memory_used_gb",
    "memory_available_gb",
    "memory_used_pct",
    "memory_bandwidth_gb_s",
    "memory_bandwidth_pct",
    # GPU
    "gpu_power_w",
    "gpu_freq_mhz",
    "gpu_render_busy_pct",
    "gpu_compute_busy_pct",
    "gpu_video_busy_pct",
    # NPU
    "npu_utilization_pct",
    "npu_power_w",
    "npu_frequency_mhz",
    "npu_bandwidth_mib_s",
    "npu_temperature_c",
)


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


class RunSampler:
    """Samples the platform into a CSV until stopped.

    One instance per run. It owns a private GPUMonitor and a private
    CpuUsageSampler rather than going through `gpu_perf.get_gpu_usage_output()` /
    `cpu.get_cpu_dynamic()`: both of those read module-level instances holding
    delta state for the dashboard's pollers, and a second caller sampling at 2 Hz
    would consume the baselines the dashboard is about to read (and vice versa).
    Utilisation would merely get noisy; GPU power, which is an energy counter
    differenced over dt, would blow up into thousands of watts on a microsecond
    gap. Everything stateless (frequency, temperature, memory capacity, NPU) goes
    straight through monitor.metrics' module functions.
    """

    def __init__(self, csv_path: Path, period_s: float = DEFAULT_PERIOD_S,
                 quiet_owner: Optional[str] = None):
        self.csv_path = Path(csv_path)
        self.period_s = max(0.1, float(period_s))
        self.rows_written = 0
        # Whose quiet-mode lease this thread renews, if any. The lease is what
        # stops the gate from outliving the run: if this thread dies without its
        # job reaching a terminal status, nothing renews and the watchdog
        # restores normal monitoring.
        self.quiet_owner = quiet_owner
        # Last row sampled, served to the dashboard by latest(). Quiet mode has
        # the background collectors stood down, so this thread's 2 Hz data is the
        # only live reading in the process -- and it costs nothing extra to keep,
        # since _sample() already produced it for the CSV.
        self._last_row: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fh = None
        self._writer: Optional[csv.DictWriter] = None
        self._gpu = None
        self._membw: Optional[membw.Sampler] = None
        self._membw_max_gb_s: Optional[float] = None
        self._cpu: Optional[cpu_metrics.CpuUsageSampler] = None

    # --- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Open the CSV and the collectors, then start sampling. False if the CSV
        cannot be written -- a run should not be aborted for that, only unmetered."""
        try:
            privilege.ensure_dir(self.csv_path.parent)
            self._fh = open(self.csv_path, "w", newline="", encoding="utf-8")
            # This thread stays in the root parent -- it needs perf and sysfs --
            # so the CSV it writes lands root-owned in a directory the pipeline
            # otherwise owns. The pipeline only reads it, but handing it over
            # keeps the runtime tree uniformly the child's to manage.
            privilege.chown(self.csv_path)
        except OSError as exc:
            logger.warning("Benchmark sampler disabled; cannot write %s: %s",
                           self.csv_path, exc)
            return False

        self._writer = csv.DictWriter(self._fh, fieldnames=list(COLUMNS),
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()

        gpu = gpu_perf.create_independent_sampler()
        self._gpu = gpu if gpu.start() else None

        bandwidth = membw.Sampler()
        if bandwidth.start():
            self._membw = bandwidth
            self._membw_max_gb_s = bandwidth.max_bandwidth_gb_s
        else:
            # start() already warned once with the reason.
            self._membw = None

        # Constructing it takes the baseline, so the first row is a real interval
        # and not "usage since boot".
        self._cpu = cpu_metrics.CpuUsageSampler()

        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="bench-sampler")
        self._thread.start()
        logger.info("Benchmark sampler started: %s (period=%.2fs, gpu=%s, membw=%s)",
                    self.csv_path, self.period_s,
                    self._gpu is not None, self._membw is not None)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop sampling and release every collector. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # The loop only ever blocks in a bounded sleep or a collector
                # call, so this means a collector wedged. Leak the thread rather
                # than hold up the caller; it is a daemon.
                logger.warning("Benchmark sampler thread did not stop within %.1fs",
                               timeout)

        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        if self._membw is not None:
            self._membw.close()
            self._membw = None
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self._writer = None
        # Only the first call reports: runner.py's error path and the job's
        # on_finish hook can both land here for the same run.
        if thread is not None:
            logger.info("Benchmark sampler stopped: %d row(s) in %s",
                        self.rows_written, self.csv_path)

    # --- sampling loop ----------------------------------------------------
    def latest(self) -> Optional[Dict[str, Any]]:
        """The most recent sampled row, or None before the first tick.

        A plain attribute read: dict assignment in _loop is atomic under the GIL,
        so a reader either sees the previous row or the new one, never a
        half-populated one -- and no lock is taken on the sampling thread's hot
        path just to serve a 3 s dashboard poll.
        """
        return self._last_row

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            if self.quiet_owner:
                # Renewed unconditionally, including while the user has the gate
                # manually down: the lease exists to stop the hold from leaking,
                # not to record whether the gate is up.
                quiet_mode.heartbeat(self.quiet_owner)
            try:
                row = self._sample()
                self._last_row = row
                if self._writer is not None:
                    self._writer.writerow(row)
                    self._fh.flush()
                    self.rows_written += 1
            except Exception as exc:
                # Never let a collector fault kill the thread: the run is still
                # valid, it just loses a sample.
                logger.debug("Benchmark sampler tick failed: %s", exc)
            # Subtract the work we just did, so the row rate stays at the
            # requested period instead of period + collection time.
            self._stop.wait(max(0.0, self.period_s - (time.monotonic() - started)))

    def _sample(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {"timestamp_s": round(time.time(), 6)}
        row.update(self._sample_cpu())
        row.update(self._sample_memory())
        row.update(self._sample_gpu())
        row.update(self._sample_npu())
        return row

    def _sample_cpu(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        # Our own CpuUsageSampler, not the module-level one get_cpu_dynamic()
        # polls for the dashboard: one baseline shared between a 2 Hz loop and a
        # 2 s poller means each side measures the sliver the other left behind.
        per_core_usage = self._cpu.sample() if self._cpu is not None else []
        out["cpu_usage_pct"] = _mean([v for v in per_core_usage if v is not None])

        # Frequency is a plain sysfs read -- no shared state to protect -- but it
        # still goes through monitor.metrics rather than psutil, so that what
        # "the clock of a core" means is decided in one place for the dashboard
        # and for this sampler alike.
        per_core_freq, per_core_max = cpu_metrics.get_per_core_freq_mhz()

        groups = cpu_metrics.classify_cores(per_core_max)
        for prefix, key in (("p", "p_cores"), ("e", "e_cores")):
            indices = groups.get(key) or []
            out[f"cpu_{prefix}_core_usage_pct"] = _mean([
                per_core_usage[i] for i in indices
                if i < len(per_core_usage) and per_core_usage[i] is not None
            ])
            out[f"cpu_{prefix}_core_freq_mhz"] = _mean([
                per_core_freq[i] for i in indices
                if i < len(per_core_freq) and per_core_freq[i] is not None
            ])

        # Both come off the same coretemp entry, so one call answers both. tjmax
        # is a constant of the part rather than a reading, but the analysis step
        # only ever sees the CSV, so it is repeated on every row to keep the
        # schema uniform -- without it there is no thermal-headroom signal.
        try:
            temps = cpu_metrics.get_cpu_temperatures()
        except Exception:
            temps = {}
        out["cpu_package_temp_c"] = temps.get("package_c")
        out["cpu_package_tjmax_c"] = temps.get("package_tjmax_c")

        return out

    def _sample_memory(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            mem = cpu_metrics.get_memory_dynamic()
            total = mem.get("total_gb")
            available = mem.get("available_gb")
            out["memory_used_gb"] = (
                _round(total - available) if total is not None and available is not None
                else None
            )
            out["memory_available_gb"] = available
            out["memory_used_pct"] = mem.get("usage_percent")
        except Exception:
            out.update({"memory_used_gb": None, "memory_available_gb": None,
                        "memory_used_pct": None})

        gb_s = self._membw.sample() if self._membw is not None else None
        out["memory_bandwidth_gb_s"] = _round(gb_s)
        out["memory_bandwidth_pct"] = (
            _round(100.0 * gb_s / self._membw_max_gb_s)
            if gb_s is not None and self._membw_max_gb_s else None
        )
        return out

    def _sample_gpu(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "gpu_power_w": None, "gpu_freq_mhz": None,
            "gpu_render_busy_pct": None, "gpu_compute_busy_pct": None,
            "gpu_video_busy_pct": None, "cpu_package_power_w": None,
        }
        parsed = self._gpu.sample() if self._gpu is not None else None
        if not parsed:
            return out

        # Summed / maxed across devices. On the single-Intel-GPU client platforms
        # this targets that is the one device; on a host with both an iGPU and a
        # dGPU it reports the busiest, which is the one running the model.
        gpu_w: List[float] = []
        pkg_w: List[float] = []
        freqs: List[float] = []
        busy: Dict[str, List[float]] = {}
        for device in parsed.get("devices") or []:
            power = device.get("power_w") or {}
            if power.get("gpu") is not None:
                gpu_w.append(power["gpu"])
            # The "package" domain is the CPU package RAPL counter, which the GPU
            # hwmon node happens to expose on integrated parts -- hence a cpu_*
            # column name for a value read here.
            if power.get("pkg") is not None:
                pkg_w.append(power["pkg"])
            for freq in device.get("freqs") or []:
                value = freq.get("act_mhz") or freq.get("cur_mhz")
                if value is not None:
                    freqs.append(value)
            for short, column in (("rcs", "gpu_render_busy_pct"),
                                  ("ccs", "gpu_compute_busy_pct"),
                                  ("vcs", "gpu_video_busy_pct")):
                value = (device.get("engine_util") or {}).get(short)
                if value is not None:
                    busy.setdefault(column, []).append(value)

        out["gpu_power_w"] = _round(sum(gpu_w)) if gpu_w else None
        out["cpu_package_power_w"] = _round(max(pkg_w)) if pkg_w else None
        out["gpu_freq_mhz"] = _round(max(freqs), 1) if freqs else None
        for column, values in busy.items():
            out[column] = _round(max(values))
        return out

    def _sample_npu(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "npu_utilization_pct": None, "npu_power_w": None,
            "npu_frequency_mhz": None, "npu_bandwidth_mib_s": None,
            "npu_temperature_c": None,
        }
        try:
            result = npu_metrics.get_intel_npu_smi_output()
        except Exception as exc:
            logger.debug("NPU sample failed: %s", exc)
            return out
        if not result.get("available") or not result.get("raw"):
            return out
        try:
            payload = json.loads(result["raw"])
        except (TypeError, ValueError):
            return out

        out["npu_utilization_pct"] = _round(payload.get("utilization_percent"))
        out["npu_power_w"] = _round(payload.get("power_w"), 3)
        out["npu_frequency_mhz"] = _round(payload.get("frequency_mhz"), 1)
        out["npu_bandwidth_mib_s"] = _round(payload.get("noc_bandwidth_mib_per_s"))
        out["npu_temperature_c"] = _round(payload.get("temperature_c"), 1)
        return out


def has_samples(csv_path: Path) -> bool:
    """Whether a metrics CSV holds at least one data row (not just the header).

    Used to report `metrics_available` honestly: the file is created as soon as a
    run starts, so its existence says nothing.
    """
    try:
        with open(csv_path, "r", encoding="utf-8", errors="replace") as fh:
            next(fh, None)          # header
            return next(fh, None) is not None
    except OSError:
        return False
