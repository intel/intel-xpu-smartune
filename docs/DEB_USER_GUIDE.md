# Intel XPU SmarTune Debian Package User Guide

This guide explains how to install, launch, and uninstall Intel XPU SmarTune on Ubuntu 26.04 using the Debian package.

## System Requirements

- Ubuntu 26.04 (amd64)
- A local administrator account
- A GNOME desktop session or another desktop environment with a Polkit authentication agent

> The Ubuntu 26.04 package bundles Python 3.14 dependencies. Do not install it on Ubuntu 24.04 or an older Ubuntu release.

## Package variants

There are two mutually exclusive packages — install **one** of them:

| Package | Contents | Service runs |
| --- | --- | --- |
| `smartune-monitor` | Monitor only (telemetry + system-pressure dashboard) | `smartune.py -m` |
| `smartune` | **Full** product: balancer **and** monitor combined | `smartune.py -a` |

`smartune` supersedes `smartune-monitor` (it declares `Conflicts/Replaces/Provides`), so installing one automatically replaces the other. Both install to `/opt/intel/smartune`, use the same `smartune-monitor.service`, and serve the dashboard at `https://localhost:9001`.

The full `smartune` package additionally needs the balancer's system dependencies, which `apt install` pulls automatically: `python3-bpfcc` (eBPF/BCC Python binding), `cpupower`, `iproute2`, `iptables`, `iotop`, `kmod`. The balancer compiles its eBPF programs **at runtime**, so a matching `linux-headers-<version>` package for your running kernel must also be present (`linux-headers-generic` is recommended). If `cpupower` is unavailable on your release, install `linux-tools-generic` instead.

## Install SmarTune

Open a terminal in the directory containing the Debian package, then run **one** of:

```bash
# Monitor only:
sudo apt install ./build/smartune-monitor_1.5.0+ubuntu26.04_amd64.deb

# Full (balancer + monitor):
sudo apt install ./build/smartune_1.5.0+ubuntu26.04_amd64.deb
```

Using `apt install` is recommended because it installs any required system dependencies automatically. The full package needs apt repository access at install time to pull `python3-bpfcc` and the other balancer dependencies (they are **not** bundled offline).

SmarTune is installed under:

```text
/opt/intel/smartune
```

The installation also adds:

- Application launcher: `Intel XPU SmarTune Monitor`
- systemd service: `smartune-monitor.service`
- Desktop shortcut when the desktop environment supports it

## Launch SmarTune

### 1. Find the application

Open the application overview and search for `Intel XPU` or `SmarTune`. Select **Intel XPU SmarTune Monitor**.

![Search for Intel XPU SmarTune](images/deb-search-smartune.png)

You can also open the **Intel XPU SmarTune Monitor** shortcut from the desktop when it is available.

### 2. Authenticate

SmarTune requires administrator privileges to start the monitor service and read its API token. Enter your administrator password in the Polkit dialog, then select **Authenticate**.

![Authenticate to start SmarTune](images/deb-authentication.png)

If you select **Cancel**, the monitor service will not start and the dashboard will not open.

### 3. Use the dashboard

After authentication succeeds, SmarTune starts `smartune-monitor.service` and opens the dashboard in the default browser:

```text
https://localhost:9001
```

![Intel XPU SmarTune dashboard](images/deb-dashboard.png)

The browser may display the site as **Not Secure** because the local dashboard uses a self-signed TLS certificate. Confirm that the address is `localhost:9001` before continuing.

## Check Service Status

To check whether the monitor service is running:

```bash
systemctl status smartune-monitor.service
```

To inspect its recent logs:

```bash
sudo journalctl -u smartune-monitor.service -b --no-pager
```

To start or stop the service manually:

```bash
sudo systemctl start smartune-monitor.service
sudo systemctl stop smartune-monitor.service
```

## Troubleshooting

If the authentication dialog does not appear, log out of the desktop session and log in again. Closing the application window or locking the screen does not restart the desktop Polkit authentication agent.

If the dashboard does not open, check the service and local endpoint:

```bash
systemctl status smartune-monitor.service
curl -k -I https://localhost:9001/
```

If installation reports a Python version mismatch, verify the operating system version:

```bash
cat /etc/os-release
python3 --version
```

The Ubuntu 26.04 package expects Python 3.14.

## Uninstall SmarTune

Run (use the package name you installed — `smartune-monitor` or `smartune`):

```bash
sudo apt remove smartune-monitor
```

To remove package-managed configuration files as well, use:

```bash
sudo apt purge smartune-monitor
```

After uninstalling, verify that the service is no longer installed:

```bash
systemctl status smartune-monitor.service
```
