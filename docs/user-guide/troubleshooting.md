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
