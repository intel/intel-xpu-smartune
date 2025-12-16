# Multitask Resource Balancing Service (MRB)
This is multitask resource balancing service (MRB) for AI NAS, which is designed to dynamically adjust resource allocation
for various Apps(AI) based on their priority and system pressure.
It uses cgroups v2 to manage resources like CPU, memory, and I/O.

# Solution:

![multitask_balance_architect.png](multitask_balance_architect.png)

# Requirement:
    1.Verified Platforms:
        System Memory >= 32GB
        Ubuntu24.10 + kernel 6.11.0-29-generic
        Python 3.12
    2. Dependencies:
        - bcc


# Key features:
    1. monitor resources
    2. adjust resources dynamically
    3. support cgroups v2
    4. support multiple resource types (CPU, memory, I/O)
    5. priority-based app balancing(priority queue)


# Directory Structure:
```
    mtb/
    ├── BalanceService.py        # Server for managing resource balancing and provide FastApi.
    ├── balancer/                # App interaction and balancing logic and priority queue
    ├── config/                  # Configuration loader
    ├── controller/              # System pressure and Adjustment components
    ├── db/                      # Database of app information
    ├── monitor/                 # App monitoring components
    ├── test/                    # Some feature tests
    ├── utils/                   # Some utility functions
    ├── web/                     # Web interface for monitoring and control
    └── requirements.txt      # Dependencies
```

# System Control:
The system control and monitoring module is designed to balance multiple AI apps running concurrently on NAS.
Its core mechanisms are as follows:
```
1. Control: Restrict uncontrolled or low-priority apps that consume the most resources
    when system resources are strained, freeing up resources to ensure stable operation of critical apps.
2. Monitor: Monitor system resources in real time, generate resource ratings, and
    manage the startup/shutdown status of controlled apps.
3. Priority Queue: Automatically suspend the launch of non-critical apps when resources are limited,
    add their launch requests to a priority queue, and trigger automatic startup
    in priority order once resources are sufficient.
4. Keep-Alive: Critical controlled apps are automatically set to keep-alive mode upon launch
    to guarantee continuous, stable operation.
5. Web UI: Support manual management of controlled apps, including priority adjustment, launch cancellation,
    resource limit configuration, restoration, keep-alive setup and app deletion.
Key Words:
    Balancer, Controlled Apps, Monitoring, Priority-Queue-based App Management, Top Resource-Consuming App Processes,
    System Pressure Calculation, CPU/Memory/Disk and Network IO Usage Status...
```

# Network Control & Monitor Design
The network control and monitoring module is designed as an independent component,
separated from the system resource management logic. The main mechanisms are as below:
```
1. Traffic Control using cgroup + iptable/tc for ingress and egress network.
2. Periodically samples network interface traffic (currently only supports one network interface),
    calculates network pressure, and determines the current network pressure level (low/medium/high/critical)
    based on a moving average window
3. Using tc/htb queues to assign classes for different priorities (low/high/critical/system; medium is treated
    as low priority), setting minimum bandwidth (rate) and maximum bandwidth (ceil) for each.
    Dynamically adjusts ceil to implement rate limiting.
4. Bandwidth limiting and recovery are both triggered by network pressure levels. When pressure reaches
    the critical level, limiting starts from the low-priority class by
    reducing its ceil to either half or the minimum rate, then applies the same strategy to the high-priority class.
    The critical class is never limited. As soon as the pressure drops below critical, the recovery process begins:
    first restoring the high-priority class (either fully or partially, based on usage), then the low-priority class,
    using the same approach. All regulation is based on real-time traffic pressure, not static quotas.
5. Assigns dedicated priority classes for common system ports (e.g., 22, 80, 443) to ensure bandwidth for system services.
6. Automatically allocates marks for controlled apps, binds them to the corresponding class using iptables + tc filter,
    and supports automatic rule cleanup when apps exit. All apps not explicitly included in the control list are
    treated as low-priority by default.
7. All parameters can be configured in config.yaml, including enabling/disabling network control, interface name,
    bandwidth ranges, pressure thresholds, system ports, etc.
```

# Some useful commands and notes:

    systemctl list-units
    systemctl --user list-units

    systemd-cgls --no-page

    systemd-cgls  /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice

    systemctl set-property --runtime
        The systemctl set-property --runtime command is used to dynamically adjust resource control settings for systemd units (like services, slices, or scopes) during their runtime, without making permanent changes that survive a reboot. It allows you to modify properties like CPU usage, memory limits, and other resource allocations immediately, but these changes are not saved to the unit files and will be lost after the next system restart.

        example:
        systemctl set-property --runtime session-3660.scope CPUQuota=10%
        systemctl set-property --runtime my-service.service CPUQuota=50%
        systemctl set-property --runtime user.slice MemoryLimit=512M
        systemctl set-property --runtime session-2.scope MemoryLimit=14G
        systemctl set-property --runtime httpd.service CPUShares=600 MemoryLimit=500M
        systemctl --user set-property --runtime evolution-addressbook-factory.service CPUQuota=50%

    Network related commands:
        # --- TC (Traffic Control) Class & Filter Inspection ---
        tc -s class show dev enp1s0        # Show egress class stats for main NIC
        tc -s class show dev ifb0          # Show ingress class stats for IFB device
        tc -s filter show dev enp1s0       # Show all filters for main NIC

        # --- TC Queue Discipline (qdisc) Cleanup ---
        tc qdisc del dev enp1s0 handle 50: root   # Remove root qdisc for main NIC
        tc qdisc del dev enp1s0 ingress           # Remove ingress qdisc for main NIC

        # --- IPTables Rule Inspection and Cleanup ---
        sudo iptables -t mangle -L OUTPUT -n --line-numbers   # List all mangle OUTPUT rules with line numbers
        sudo iptables -t mangle -F OUTPUT                     # Flush all mangle OUTPUT rules
        sudo iptables -t mangle -D OUTPUT <num>               # Delete specific mangle OUTPUT rule by line number

    Note:
    1. https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/6/html/resource_management_guide/starting_a_process
        Launch processes in a cgroup by running the cgexec command. For example, this command launches the firefox web browser within the group1 cgroup, subject to the limitations imposed on that group by the cpu subsystem:
        # cgexec -g cpu:group1 firefox http://www.redhat.com

        The syntax for cgexec is:
        # cgexec -g subsystems:path_to_cgroup command arguments

       2. Add a program's executables to cgroups-v2
           https://unix.stackexchange.com/questions/694812/is-there-any-other-way-to-add-program-to-cgroups-v2-instead-of-giving-their-pids
           # pidof firefox > /sys/fs/cgroup/Example/tasks/cgroup.procs


    3. Under Linux, you can use inotifywait to wait for an access or close_nowrite event on the executable, e.g. inotifywait -m -e access,close_nowrite --format=%e /bin/ls. There is an access event whenever the file is executed and a close_nowrite when the process dies. You can't get the process ID that way, so you'll then have to find out which processes have the file open (e.g. with fuser or lsof) and then filter the ones that are executing the file.

       4. systemctl list-units  -t help
          systemd-cgls  /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/
          ./lscgroup  -g misc://user.slice/user-1000.slice/user@1000.service/app.slice
          systemd-cgls
          lslogins -u


# Installation:
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
    
    client: 
        1.  Start a new terminal to run:
            bash Miniforge3-Linux-x86_64.sh (Prepare the package)
            conda create -n mt_py312 python=3.12.7
            conda activate mt_py312
            pip install -r requirements.txt

            OR:
            Create a env w/o conda:
            cd web/
            pip install virtualenv
            python -m virtualenv balancer
            source balancer/bin/activate
            pip install -r ../requirements.txt

        2. pip install dist/libcgroup-3.2.0-cp312-cp312-linux_x86_64.whl(Probably no need, 
                but if need, please refer to "Other" below to generate whl)

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
 
# Start:
    1. server:
        config/config.yaml
            enable_network_control: true -> default is enabled, disable with false
            network_interface default use enp1s0, change to your specific interface
            vendor: "generic" -> sudo python3 BalanceService.py
            If you are in "admin" permission, config/config.yaml # vendor: "admin" -> python BalanceService.py

    2. client:
        cd web
        ./start_webui.sh mt_py312 OR:
        ./start_webui_env.sh (w/o conda)
        
        



