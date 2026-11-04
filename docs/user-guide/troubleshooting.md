# Troubleshooting & FAQ

[← Back to contents](../README.md)

## The dashboard won't open

1. Confirm the service is running:
   ```bash
   systemctl status smartune-monitor.service
   ```
2. Check the local endpoint directly:
   ```bash
   curl -k -I https://localhost:9001/
   ```
3. Inspect recent logs:
   ```bash
   tail -f logs/smartune.log          # source install
   sudo journalctl -u smartune-monitor.service -b --no-pager   # package install
   ```

## The browser says "Not Secure"

Expected — the local dashboard uses a **self-signed TLS certificate**. Confirm
the address is `localhost:9001` and proceed.

## I can't sign in / "Invalid access token"

The dashboard signs in with an **access token**, not a user name and password.

1. Read the current token on the server:
   ```bash
   sudo cat key/api_token                          # source install
   sudo cat /opt/intel/smartune/key/api_token      # Debian package
   ```
   If the service was started with `BALANCER_API_TOKEN` set, that value is the
   token instead, and it overrides the file.
2. Paste it into the **Access token** field with no extra spaces or line breaks.
3. If the dashboard suddenly returns to the sign-in screen, the token was changed
   on the server. Sign in again with the new one.
4. Forgot or need a new token: stop the service, delete `key/api_token`, start the
   service, and read the newly generated token.

See [Getting started → Log in](getting-started.md#3-log-in) for details.

## The admin/authentication dialog never appears

Log out of the desktop session and log back in. Locking the screen or closing the
window does **not** restart the desktop's Polkit authentication agent.

## Install reports a Python version mismatch

The Debian package is built for a specific OS/Python. Verify:

```bash
cat /etc/os-release
python3 --version
```

Install the package built for your OS release (see the
[Debian package guide](../install.md#debian-package)).

## I don't see the Control center / Models / System diagnostics tab

These are optional capabilities:

- **Control center** appears only on the **full** (`smartune`) install, not the
  monitor-only one.
- **Models** and **System diagnostics** appear only when enabled on the server.

The dashboard adapts automatically to what the server reports. See
[Getting started](getting-started.md).

## GPU / NPU cards are missing or show no data

- Confirm the hardware exists and its driver is loaded (check
  **[About](about.md)** for detected devices and driver versions).
- Make sure the card is enabled under **[Settings → System Overview](settings.md#system-overview)**.
- NPU telemetry requires a supported Intel platform (MTL / ARL / LNL / PTL).
- Some GPU fields are read from kernel debugfs and need the monitor to run as
  root; otherwise they show **N/A**: dGPU VRAM, the Xe iGPU's own memory usage,
  and EU / Xe-core counts.
- On an `xe`-driver GPU, engine utilization and RC6 may briefly show **N/A**
  while the device is runtime-suspended or just resuming — SmarTune avoids
  waking an idle GPU just to sample it.

## Memory bandwidth, CPU/GPU Mem BW or SoC cache shows N/A

- **DRAM bandwidth** is read from Intel PMT telemetry where SmarTune knows the
  platform's layout (Meteor/Arrow Lake, Lunar Lake, Panther Lake, Wildcat Lake),
  and otherwise from the memory-controller counters. If neither is readable it
  shows **N/A**.
- **CPU / GPU Mem BW** and the **SoC cache** figures come from PMT only. PMT's
  telemetry file is readable by root only, so run the monitor as root.
- The first reading after the monitor starts is always blank — a rate needs two
  samples.
- CPU + GPU Mem BW can exceed the DRAM total: they are measured before the SoC
  cache, and part of that traffic never reaches DRAM.

## GPU Mem BW shows 0 or an estimate

On platforms that have no GPU-only bandwidth counter,
the iGPU's **Mem BW** is derived from a shared counter that also carries NPU, disk
and display traffic.

- **0.0 GB/s** — the GPU (graphics and media) is idle, so it is not using memory
  bandwidth, even if disk activity or swapping is heavy at the same time.
- **≈ value** — heavy disk traffic that bypasses the SoC cache (typically
  swapping) is running while the GPU is busy. The figure is then estimated from
  the SoC cache total and is coarse, roughly ±1–2 GB/s. It returns to the normal
  measurement on its own once that traffic stops. CPU Mem BW, DRAM bandwidth and
  the SoC cache figures are unaffected.

## Resource limits aren't being applied

- Resource control requires the **full** install and its native dependencies
  (`python3-bpfcc`/bcc, `cpupower`, `iproute2`, `iptables`) plus matching
  `linux-headers-<version>` for runtime eBPF.
- Confirm the relevant control is enabled under
  **[Settings → SmarTune Control](settings.md#smartune-control)**.
- Check **[System diagnostics](diagnostics.md)** — a **Limit Reason** or an event
  will explain whether a limit was attempted and whether it was clamped.

## Historical data disappears over time

That's the **retention window** working as designed. Extend it under
**[History → Retention settings](history.md#data-retention)**, or export anything
you need to keep from **[Diagnostics → report](diagnostics.md#export-a-report)**.

## FAQ

**Does SmarTune send any data off the machine?**
No — monitoring, logs, and diagnostics stay on the single host. The dashboard is
served locally over `https://localhost:9001`.

**Do I need a client install?**
No. The dashboard is a browser UI served by the service on the same machine.

**Can I run only monitoring, without any control?**
Yes — install the `smartune-monitor` package (or run `bash start_smartune.sh -m`).

---

Still stuck? See the [project README](../../README.md), the
[Backend API reference](../reference/api.md), or the
[Balancer README](../../balancer/README.md).
