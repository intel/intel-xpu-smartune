Unified XPU monitoring

Runs a single console page showing CPU, iGPU, and NPU metrics.

Usage (from repo root):
- `python3 -m py_compile $(find monitor -name '*.py' -print)`
- `PYTHONPATH=. python3 -m monitor --interval 1.0`

Notes
- iGPU i915: uses `/sys/class/drm/card0/*` for frequency and tries `sudo -n intel_gpu_top -o -` for engine usage.
- iGPU Xe: tries `qmassa` and expects JSON output (best-effort).
- NPU: reads sysfs paths matching the Go reference implementation.

If a tool/sysfs path is missing, the monitor reports an error but keeps running.
