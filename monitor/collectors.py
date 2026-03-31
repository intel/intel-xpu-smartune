# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import time
import json
import tempfile
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # optional dependency; fall back to /proc
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

from .types import MetricSample, err, ok
from .util import clamp, module_loaded, read_float, run_capture, try_parse_json, which

# Precise lookup based on the proc filesystem 
def get_pid_cmdline(pid: int) -> str:
    """Read the full command-line arguments of a process"""
    try:
        # cmdline arguments are typically null-separated (\0); replace with spaces for matching
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        return data.replace(b'\x00', b' ').decode('utf-8').strip()
    except Exception:
        return ""

def check_pid_has_device_handle(pid: int, device_pattern: str) -> bool:
    """
    Check if the process has opened a specific device file (via /proc/pid/fd)
    device_pattern: e.g., 'renderD' or 'accel'
    """
    try:
        fd_dir = Path(f"/proc/{pid}/fd")
        if not fd_dir.exists():
            return False
        
        for fd_link in fd_dir.iterdir():
            try:
                # Use readlink to get the actual path pointed to by the file descriptor
                target = os.readlink(fd_link)
                if device_pattern in target:
                    return True
            except OSError:
                continue
    except Exception:
        pass
    return False

def find_benchmark_process(target_type: str) -> str:
    """
    Accurately locate 'benchmark_app' processes.
    target_type: 'CPU', 'GPU', or 'NPU'
    Logic:
    1. Iterate through all running processes.
    2. Find processes with names containing 'benchmark_app'.
    3. Check if command-line arguments contain the corresponding device flags (e.g., -d GPU / -d NPU).
    4. (Optional) For GPU/NPU, verify if the process holds the specific device handles.
    """

    pids = [int(d) for d in os.listdir('/proc') if d.isdigit()]
    
    for pid in pids:
        cmdline = get_pid_cmdline(pid)
        
        if "benchmark_app" not in cmdline:
            continue
        
        is_match = False
        
        DEVICE_PATTERNS = {
            "GPU": ("-d GPU", "renderD"),
            "NPU": ("-d NPU", "accel")
        }

        if target_type in DEVICE_PATTERNS:
            arg_flag, dev_pattern = DEVICE_PATTERNS[target_type]
            if arg_flag in cmdline:
                is_match = check_pid_has_device_handle(pid, dev_pattern)
        elif target_type == "CPU":
            is_match = "-d CPU" in cmdline or not any(f in cmdline for f in ["-d GPU", "-d NPU"])

        if is_match:
            try:
                short_name = Path(f"/proc/{pid}/comm").read_text().strip()
            except:
                short_name = "benchmark_app"
            return f"{short_name}({pid})"

    return ""


# Collector Implementation
@dataclass(slots=True)
class CPUCollector:
    _last_cpu_total: float = 0.0
    _last_cpu_idle: float = 0.0

    def _read_proc_stat(self) -> Tuple[float, float]:
        line = Path("/proc/stat").read_text().splitlines()[0]
        parts = line.split()
        if len(parts) < 5:
            raise ValueError("unexpected /proc/stat format")
        values = [float(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        total = sum(values)
        return total, idle

    def get_cpu_usage(self) -> float:
        if psutil is not None:
            return psutil.cpu_percent(interval=None)

        # Fallback to /proc/stat
        try:
            total, idle = self._read_proc_stat()

            if self._last_cpu_total <= 0:
                self._last_cpu_total, self._last_cpu_idle = total, idle
                time.sleep(0.05)
                # re-read
                total, idle = self._read_proc_stat()

            total_diff = max(1e-6, total - self._last_cpu_total)
            idle_diff = max(0.0, idle - self._last_cpu_idle)
            self._last_cpu_total, self._last_cpu_idle = total, idle
            return clamp((1.0 - (idle_diff / total_diff)) * 100.0, 0.0, 100.0)
        except:
            return 0.0

    def get_load(self) -> Tuple[float, float, float]:
        try:
            if psutil is not None:
                return psutil.getloadavg()
            return os.getloadavg()
        except:
            return (0.0, 0.0, 0.0)

    def sample(self) -> MetricSample:
        try:
            top_p = find_benchmark_process("CPU")

            return ok(
                "CPU",
                cpu_percent=round(float(self.get_cpu_usage()), 2),
                loadavg=[round(x, 2) for x in self.get_load()],
                top_proc=top_p 
            )
        except Exception as e:
            return err("CPU", str(e))


@dataclass(slots=True)
class NPUCollector:
    base_path: Path = Path("/sys/devices/pci0000:00/0000:00:0b.0")

    def _runtime_active_time_path(self) -> Path:
        return self.base_path / "power/runtime_active_time"

    def sample(self) -> MetricSample:
        try:
            runtime_path = self._runtime_active_time_path()
            if not runtime_path.exists():
                return err(
                    "NPU",
                    f"missing sysfs path: {runtime_path}",
                    detected=False,
                )

            # Usage estimation: delta(runtime_active_time_ms) / delta(time_ms) * 100
            t0 = time.time()
            r0 = read_float(runtime_path)
            time.sleep(0.10)
            t1 = time.time()
            r1 = read_float(runtime_path)

            runtime_diff = max(0.0, r1 - r0)
            time_diff_ms = max(1e-6, (t1 - t0) * 1000.0)
            usage = clamp((runtime_diff / time_diff_ms) * 100.0, 0.0, 100.0)

            # Frequency (best-effort)
            freq = {}
            for fname in ("npu_current_frequency_mhz", "npu_max_frequency_mhz"):
                p = self.base_path / fname
                if p.exists():
                    key = fname.replace("_mhz", "")
                    try:
                        freq[key] = float(read_float(p))
                    except Exception:
                        pass

            top_p = find_benchmark_process("NPU")

            return ok(
                "NPU",
                usage_percent=round(float(usage), 2),
                top_proc=top_p,
                **freq,
            )
        except Exception as e:
            return err("NPU", str(e))

def _parse_intel_gpu_top_line(line: str) -> Dict[str, float]:
    # Matches the Go parser: expects >= 20 columns.
    fields = (line or "").split()
    if len(fields) < 20:
        return {}

    def f(idx: int) -> float:
        try:
            return float(fields[idx])
        except Exception:
            return 0.0

    return {
        "GPU_Freq_req": f(0),
        "GPU_Freq_act": f(1),
        "IRQ": f(2),
        "RC6": f(3),
        "Power_gpu": f(4),
        "Power_pkg": f(5),
        "RCS": f(6),
        "BCS": f(9),
        "VCS": f(12),
        "VECS": f(15),
        "CCS": f(18),
    }

@dataclass(slots=True)
class IGPUCollector:
    """Collect iGPU metrics.

    - If i915 is loaded, prefers sysfs freq + `intel_gpu_top` for engine utilization.
    - If xe is loaded, prefers `qmassa` (best-effort parsing).

    This is intentionally best-effort: the tools may not be installed, and sudo may
    not be configured for intel_gpu_top.
    """

    drm_card: Path = Path("/sys/class/drm/card0")

    def _read_i915_freq(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for fname in ("gt_cur_freq_mhz", "gt_RP0_freq_mhz"):
            p = self.drm_card / fname
            if p.exists():
                key = fname.replace("_mhz", "")
                try:
                    out[key] = float(read_float(p))
                except Exception:
                    continue
        return out

    def _read_intel_gpu_top(self) -> Tuple[Dict[str, float], Optional[str]]:
        if not which("intel_gpu_top"):
            return {}, "intel_gpu_top not found"

        attempts: List[List[str]] = [
            ["sudo", "-n", "intel_gpu_top", "-o", "-"],
            ["intel_gpu_top", "-o", "-"],
        ]

        for argv in attempts:
            proc: Optional[subprocess.Popen[str]] = None
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                if proc.stdout is None:
                    continue
                deadline = time.time() + 1.0
                while time.time() < deadline:
                    line = proc.stdout.readline()
                    if not line:
                        time.sleep(0.03)
                        continue
                    if "Freq MHz" in line or "req  act" in line:
                        continue
                    metrics = _parse_intel_gpu_top_line(line)
                    if metrics:
                        return metrics, None
                return {}, "timed out reading intel_gpu_top output"
            except Exception:
                continue
            finally:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass

        return {}, "failed to execute intel_gpu_top (sudo may be required)"

    def _read_qmassa(self) -> Tuple[Dict[str, Any], Optional[str]]:
        if not which("qmassa"):
            return {}, "qmassa not found"

        fd, temp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

        try:
            # -n 2: Sample 2 times
            # -m 50: 50ms per sample
            cmd = ["qmassa", "-n", "2", "-m", "50", "-x", "-t", temp_path]
            
            if os.geteuid() != 0:
                argv = ["sudo", "-n"] + cmd
            else:
                argv = cmd
            
            code, out, err_text = run_capture(argv, timeout_s=5.0)

            if code != 0:
                err_msg = (out + err_text).strip()
                if "Permission denied" in err_msg or "permission" in err_msg.lower():
                    return {}, "permission denied (check user groups: video, render)"
                return {}, f"qmassa failed (code {code}): {err_msg or 'unknown error'}"

            try:
                content = Path(temp_path).read_text().strip()
            except Exception:
                return {}, "failed to read qmassa output file"

            if not content:
                return {}, f"qmassa output empty. Stderr: {err_text}"

            metrics: Dict[str, Any] = {}
            for line in reversed(content.splitlines()):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "devs_state" in obj:
                        devs = obj.get("devs_state", [])
                        if devs:
                            stats = devs[0].get("dev_stats", {})
                            
                            # Freq
                            freqs = stats.get("freqs", [])
                            if freqs and freqs[0]:
                                tile_freq = freqs[0][0]
                                f = tile_freq.get("act_freq") or tile_freq.get("cur_freq")
                                if f is not None: 
                                    metrics["frequency_mhz"] = float(f)
                            
                            # Util
                            eng_usage = stats.get("eng_usage", {})
                            max_util = 0.0
                            for eng_name, vals in eng_usage.items():
                                if vals and isinstance(vals, list):
                                # Get the last array element (most recent sample)
                                    v = float(vals[-1])
                                    if v > max_util: 
                                        max_util = v
                            metrics["utilization"] = max_util

                            # Mem
                            mem_info_list = stats.get("mem_info", [])
                            if mem_info_list:
                                mem = mem_info_list[0]
                                metrics["_raw_mem_used"] = mem.get("smem_used", 0)
                                metrics["_raw_mem_total"] = mem.get("smem_total", 0)

                            break
                except Exception:
                    continue
            return metrics, None
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    def sample(self) -> MetricSample:
        try:
            using_i915 = module_loaded("i915")
            using_xe = module_loaded("xe")
            
            if using_xe and not using_i915:
                metrics, error = self._read_qmassa()
                if error:
                    return err("iGPU", error, driver="xe")

                top_p = find_benchmark_process("GPU")
                
                display_metrics = {k: v for k, v in metrics.items() if not k.startswith("_")}
                return ok("iGPU", driver="xe", top_proc=top_p, **display_metrics)

            return err("iGPU", "Unsupported driver (only Xe supported in this mode)")

            # # Default to i915 behavior.
            # freq = self._read_i915_freq()
            # usage, usage_err = self._read_intel_gpu_top()
            # merged: Dict[str, Any] = {"driver": "i915" if using_i915 else "unknown"}
            # merged.update(freq)
            # merged.update(usage)
            # if usage_err and not usage:
            #     return err("iGPU", usage_err, **merged)
            # return ok("iGPU", **merged)

        except Exception as e:
            return err("iGPU", str(e))

@dataclass(slots=True)
class MemCollector:
    cpu_coll: CPUCollector
    gpu_coll: IGPUCollector

    def _get_sys_mem(self) -> Tuple[float, float]:
        if psutil is not None:
            vm = psutil.virtual_memory()
            return vm.used / (1024**2), vm.total / (1024**2)
        # Fallback to /proc/meminfo
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k] = float(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", total)
        return (total - avail) / 1024, total / 1024

    def sample(self) -> MetricSample:
        try:
            sys_used, sys_total = self._get_sys_mem()
            gpu_m, _ = self.gpu_coll._read_qmassa()
            gpu_used = gpu_m.get("_raw_mem_used", 0) / (1024**2)
            
            return ok(
                "MEM",
                system_used_mb=round(sys_used, 2),
                igpu_used_mb=round(gpu_used, 2),
                total_mb=round(sys_total, 2)
            )
        except Exception as e:
            return err("MEM", str(e))

def collect_all() -> List[MetricSample]:
    cc = CPUCollector()
    gc = IGPUCollector()
    nc = NPUCollector()
    mc = MemCollector(cc, gc)

    return [cc.sample(), gc.sample(), nc.sample(), mc.sample()]
    