# Intel XPU SmarTune

## Overview

Intel XPU SmarTune is a collection of platform tools and services designed to optimize and enhance the system operational efficiency of AI NAS, it comprises several components：SystemOverview(monitor and systemPressure), App Resources, Processes, History, Balancer, About.

- Balancer [User Guide](https://github.com/intel/intel-xpu-smartune/tree/main/balancer#readme) is designed primarily for platform resource governance and application priority management.

## License

Each component of the Edge Infrastructure external is licensed under [Apache 2.0][apache-license].

[apache-license]: https://www.apache.org/licenses/LICENSE-2.0

## Architecture:

### SmarTune 1.5 Overall Architecture
![architecture_15.png](images/architecture_15.png)

## Requirement:
    1.Verified Platforms:
        MTL, PTL and WideCat Lake
        Ubuntu / debian
        Python 3.12
    2. Dependencies:
        - bcc
        - cpupower

## Installation:
    server:
        #ubuntu:
            Start a terminal w/o any virtual(like conda) env, then run:
            sudo apt install python3-pip (optional)
            sudo pip install psutil>=5.5.1 --break-system-packages
            sudo pip install peewee==3.17.8 --break-system-packages
            sudo pip install flask --break-system-packages
            # sudo pip install flask --break-system-packages --ignore-installed blinker(err with "Cannot uninstall blinker...")

        #Tos:
            1. Install above packages w/o "sudo".
            2. Re-compile kernel by enable CONFIG_IKHEADERS=m (FATAL: Module kheaders not found in directory /lib/modules/6.12.41+)
            Or, if you have "kheaders.ko",
                mkdir -p /lib/modules/$(uname -r)/kernel/kernel/
                cp kheaders.ko /lib/modules/$(uname -r)/kernel/kernel/
                depmod -a
                modprobe kheaders

        If need, please refer to "Other" -> 2. Build bcc and 3. Build cpupower to install bcc and cpupower manually.

    Other:
        1. Build libcgroup wheel from source:
            If need:
                # Go into "base" env, then check python version and upgrade to python3.12.7 with:
                # conda install -n base python=3.12.7
                # pip install --upgrade pip # if need, currently is 25.2
            pip install Cython
            sudo apt install libpam-dev flex bison libsystemd-dev cmake build-essential autoconf automake libtool m4
                sudo apt install linux-tools-common cpufrequtils -y
            git clone https://github.com/libcgroup/libcgroup.git
            cd libcgroup
            git checkout v3.2.0 -b v3.2.0
            ./bootstrap.sh(sudo apt-get --reinstall install gcc g++ // issue: /usr/include/c++/14/mutex:768:23: internal compiler error: Segmentation fault)
            make
            cd libcgroup/src/python
            export VERSION_RELEASE="3.2.0"
            python setup.py bdist_wheel
            pip install dist/libcgroup-3.2.0-cp312-cp312-linux_x86_64.whl

        2. Build bcc (Refer to: https://github.com/iovisor/bcc/blob/master/INSTALL.md#ubuntu---binary):

            sudo apt install -y zip bison build-essential cmake flex git libedit-dev \
              libllvm14 llvm-14-dev libclang-14-dev python3 zlib1g-dev libelf-dev libfl-dev python3-setuptools \
              liblzma-dev libdebuginfod-dev arping netperf iperf

            git clone https://github.com/iovisor/bcc.git
            mkdir bcc/build; cd bcc/build
            cmake ..
            make
            sudo make install
            cmake -DPYTHON_CMD=python3 .. # build python3 binding
            pushd src/python/
            make
            sudo make install
            popd

        3. Build cpupower:
            git clone https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git
            cd linux
            git checkout v6.12.41 (align to your kernel version)
            cd tools/power/cpupower
            apt install libpci-dev gettext
            make
            sudo make install

## Start:
    All commands are run from the repository root. TLS certs are auto-generated
    under `key/`, logs are written under `logs/`, and `my_database.db` is created
    at the repository root.

    1. server:
        Option A) Run manually:
            # Adjust config/config.yaml first if needed:
            #   enable_network_control: true  -> default enabled, disable with false
            #   vendor: "generic"             -> use "admin" if running with admin permission
            ./start_smartune.sh            # -a (default): balancer + monitor (single process, port 9001)
            ./start_smartune.sh -m         # monitor only (standalone telemetry, port 9001)
            # Both modes listen on port 9001; the dashboard auto-adapts to a
            # monitor-only server via /smartune/capabilities.

        Option B) Run as a systemd system service (smartune.service):
            # smartune.service uses the placeholder __SMARTUNE_ROOT__; fill it with the
            # repository root at install time so the paths follow wherever the code lives.
            1. Make sure the logs dir exists and the entry script is executable:
                 mkdir -p logs
                 chmod +x start_smartune_service.sh
            2. Install the unit file into systemd (path filled automatically from $(pwd)):
                 sed "s#__SMARTUNE_ROOT__#$(pwd)#g" smartune.service | sudo tee /etc/systemd/system/smartune.service
                 sudo systemctl daemon-reload
            3. Enable on boot and start the service:
                 sudo systemctl enable smartune.service
                 sudo systemctl start smartune.service
            4. Check status / logs:
                 sudo systemctl status smartune.service
                 tail -f logs/smartune.log
            5. Stop / disable / uninstall (if needed):
                 sudo systemctl stop smartune.service
                 sudo systemctl disable smartune.service
                 sudo rm /etc/systemd/system/smartune.service
                 sudo systemctl daemon-reload

    2. client (React dashboard – Grafana-style 6-tab UI):
        # Node.js 20.19+ is required. The script auto-installs/upgrades it on Ubuntu/Debian
        # if missing or outdated. See dashboard/README.md for full setup instructions.
        cd dashboard
        bash start_dashboard.sh
        # Opens http://localhost:39527

## Key Features:

### 1. Resource Control
Dynamically restricts CPU, memory, and disk I/O resource usage for the most resource-intensive applications via cgroups v2 when system resources are strained. Switches power modes according to pressure levels and gradually restores resource quotas as pressure eases.
- cgroups v2 resource control: CPU quota, memory.high, I/O weight (io.weight), and per-disk read/write throughput and IOPS throttling (io.max: rbps/wbps/riops/wiops) per app
- CPU frequency governor switching (powersave/performance) based on pressure level

### 2. Pressure Monitoring
Real-time collection of CPU, memory, and I/O pressure data based on Linux PSI (Pressure Stall Information), computing a composite score and classifying it into four pressure levels (low/medium/high/critical). Intercepts execve system calls via eBPF to detect controlled app launches and exits in real-time, while independently monitoring disk I/O utilization and system iowait.
- PSI-based pressure monitoring (CPU/memory/I/O) with four levels: low/medium/high/critical
- eBPF (via BCC) execve interception for real-time app launch/exit detection
- Disk I/O stress detection and top disk consumer throttling
- 📖 Deep dive: [System Pressure Model](docs/pressure_model.md) — the scoring formulas, sigmoid/EWMA curves, and how each tuning parameter affects behavior

### 3. Priority Queue
When system pressure reaches critical level or disk I/O is busy, new app launch requests are suspended and inserted into a max-priority queue. Once resources recover, queued apps are automatically launched in priority order, with support for manual cancellation of queued launches.
- Max-priority queue for deferred app launches under resource contention
- Auto-launch queued apps in priority order when resources recover
- Manual cancellation support for pending launches

### 4. Keep-Alive
Reduces the likelihood of Critical-priority controlled apps being killed by the system OOM Killer while continuously monitoring critical app processes to ensure stable operation.
- Keep-alive for Critical apps via oom_score_adj tuning
- Continuous monitoring of critical app processes to ensure stability

### 5. Disk I/O Control
Restricts applications that consume excessive disk I/O resources via cgroups v2, allocating read/write bandwidth and IOPS quotas based on priority, and gradually restoring limits as disk I/O pressure subsides.
- Per-disk read/write throughput and IOPS throttling via cgroups v2
- Priority-based bandwidth and IOPS quota allocation
- Progressive restoration as I/O pressure decreases

### 6. Network I/O Control
Implements ingress and egress traffic control for controlled apps via cgroup + iptables + tc/HTB, allocating bandwidth across four priority classes (Critical/High/Low/System). Calculates network pressure levels in real-time based on a moving average window, sequentially limiting bandwidth ceiling for low/high priority classes when pressure reaches critical level, and progressively restoring as pressure drops.
- tc/HTB + iptables + cgroup network traffic shaping with four priority classes (Critical/High/Low/System)
- Real-time network pressure calculation with moving average window
- Dynamic bandwidth ceiling adjustment based on pressure level
- Progressive bandwidth restoration as network pressure decreases

### 7. GPU Real-time Monitoring
Real-time collection of metrics for each GPU card (iGPU/dGPU automatically distinguished): gt0/gt1 frequency (current/actual/max), GPU and package power consumption, per-engine utilization (Render/Compute/Video Encode/Decode/Copy), VRAM usage, and throttle reason detection. Static info includes GPU names, engine list, EU count, PCIe speed/width, and PCI addresses.
- Per-card gt0/gt1 frequency, power, engine utilization (Render/Compute/Video Encode/Decode/Copy)
- VRAM usage and throttle reason detection
- Static info: GPU names, engine list, EU count, PCIe speed/width, PCI addresses
- Automatic iGPU/dGPU distinction

### 8. NPU Real-time Monitoring
Real-time reading of NPU utilization (%), power (W), temperature (°C), operating frequency (MHz), NoC bandwidth (MiB/s), and memory usage. Per-process NPU memory usage tracking via /proc/[pid]/fdinfo for process-level NPU usage monitoring.
- NPU telemetry via Intel PMT: utilization, power, temperature, frequency, NOC bandwidth, memory utilization
- Per-process NPU tracking via fdinfo (intel_vpu driver, drm-resident-memory)
- Supports Intel MTL / ARL / LNL / PTL platforms
- Process-level NPU memory consumption tracking

### 9. System Information Collection
Static collection of complete hardware and software environment information: CPU model, P/E-core topology and frequency ranges; total memory and DDR speeds; disk device list and capacities; NIC count, primary NIC, and peak bandwidth; GPU names, engines, PCIe, frequency ranges, and EU count for each card; NPU PCI ID, driver version, firmware version, and frequency ranges; OS version, BIOS, kernel, and GuC/HuC/NPU firmware, Mesa/OpenCL/Level Zero/Media driver versions.
- Static hardware/software inventory: CPU topology (P/E-core), GPU static config, NPU device info
- OS/BIOS/kernel/driver versions (GuC, HuC, NPU FW, Mesa, OpenCL, Level Zero, Media)
- Complete network interface information with speeds and IP addresses
- Memory channel and slot configuration

### 10. Manual Control
Supports manual operations on controlled apps, including priority adjustment, cancellation of queued launches, resource limit configuration (CPU/memory/I/O), quota restoration, keep-alive settings, and app deletion.
- REST API and Web UI for manual app management (priority, limits, restore, delete)
- React dashboard with 6-tab UI: Performance, App Resources, Process Resources, Balancer, History, About
- Support for manual resource limit adjustment and restoration


## API Documentation

For a complete reference of all backend REST API endpoints, see the [Backend API Guide](docs/API_ENDPOINTS.md).

## Directory Structure:

The monitor and the shared layers (config/utils/db) live at the repository root
so the monitor can run independently of the balancer. `balancer/` holds only the
balancer-specific source and cannot run on its own without the monitor.
```
intel-xpu-smartune/
├── smartune.py                 # Single entry point: dispatches -a (balancer + monitor) / -m (monitor only)
├── start_smartune.sh           # Start script: -a (default, all) / -m (monitor only)
├── start_smartune_service.sh   # systemd entry script (cert-gen + launch, no sudo/trap)
├── smartune.service            # systemd unit template (__SMARTUNE_ROOT__ filled at install)
├── my_database.db              # Peewee SQLite DB (generated at runtime)
├── key/                        # Generated TLS certificate/key (b_server.crt / .key)
├── logs/                       # Runtime logs
├── docs/                       # Docs: API_ENDPOINTS.md, pressure_model.md (+ images/)
├── config/                     # config.yaml (thresholds, weights, app list) and config loader
├── utils/                      # Shared utilities: logger, app_utils, http_utils
├── db/                         # Peewee ORM database model for controlled app records
├── monitor/                    # System monitoring components (runs standalone):
│   ├── monitor_service.py      #   Standalone monitor entry point (own Flask app, port 9001)
│   ├── monitor_api.py          #   Flask Blueprint exposing /monitor/* REST endpoints
│   ├── psi.py                  #   Linux PSI reader
│   ├── pressure.py             #   Pressure scoring and level classification
│   ├── res_monitor.py          #   CPU/memory/disk/network resource usage and top-process finder
│   ├── network.py              #   Network traffic sampling and pressure calculation
│   ├── cgroup.py               #   cgroup path resolution and monitoring
│   ├── app_discovery.py        #   "Add App" wizard: process search and field extraction
│   ├── gpu_monitor.py          #   Intel GPU monitoring (i915/Xe PMU, RAPL, fdinfo)
│   ├── npu_monitor.py          #   Intel NPU monitoring via PMT telemetry and fdinfo
│   ├── system_info.py          #   Static/dynamic hardware & software info collection
│   └── metrics/                #   Per-subsystem metric collectors (cpu, gpu_info, gpu_perf, npu, history, utils)
├── dashboard/                  # React/TypeScript dashboard (6-tab UI: Performance,
│                               #   App Resources, Process Resources, Balancer, History, About)
└── balancer/                   # Balancer-specific source (not standalone-runnable):
    ├── balance_service.py        #   Flask REST API server; resource-balancing entry point
    ├── balancer/                #   Core balancing logic: DynamicBalancer, MaxPriorityQueue,
    │                            #     monitor-resource loop, network controller integration
    ├── controller/              #   Resource controllers:
    │   ├── cpu.py               #     CPU quota (cgroups v2 cpu.max)
    │   ├── memory.py            #     Memory limits (memory.high / memory.max)
    │   ├── io.py                #     Disk I/O throttling (io.max rbps/wbps/riops/wiops)
    │   ├── network.py           #     Network traffic shaping (tc/HTB + iptables + cgroup)
    │   ├── governor.py          #     CPU frequency governor switching
    │   ├── psi.py               #     PSI trigger-based resource reader
    │   ├── app_intercept.py     #     eBPF-based app launch/exit detection (BCC)
    │   └── bpf_event.c          #     eBPF C program for execve/exit tracepoints
    └── test/                    #   Feature test scripts (BPF, PSI, disk I/O, notifications)
```


## Demo Screenshots:

The following screenshots demonstrate SmarTune's real-time monitoring and resource balancing capabilities:

### Performance Overview
![demo1.png](images/demo1.png)
![demo2.png](images/demo2.png)
Real-time system performance monitoring dashboard showing CPU, GPU, NPU, memory, disk, and network metrics.

### App Resources
![demo3.png](images/demo3.png)
Controlled application resource usage - top3.

### Historical Data
![demo4.png](images/demo4.png)
![demo5.png](images/demo5.png)
Historical system pressure and resource utilization trends.

### Balancer Control
![demo6.png](images/demo6.png)
Dynamic resource balancing controls and priority queue management.

### System Information
![demo7.png](images/demo7.png)
Complete hardware and software configuration details.
