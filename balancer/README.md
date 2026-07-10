# Multi-task Resource Balancer(MRB)
MRB is a system-level service for AI NAS that combines real-time hardware monitoring
(CPU, GPU, NPU, memory, disk, and network) with dynamic multi-task resource balancing.
It intercepts application launches via eBPF, evaluates system pressure via Linux PSI,
and uses cgroups v2 to control CPU, memory, and I/O resources per app based on priority.

# Resource Balancing Flow:
![balancer_flow.png](images/balancer_flow.png)


# System Control & Resource Balancing:
The system control and monitoring module is designed to balance multiple AI apps running concurrently on NAS.
Its core mechanisms are as follows:
```
1. Control: When system resources are strained, dynamically restrict the top resource-consuming apps'
    CPU quota, memory.high, I/O weight (io.weight), and per-disk read/write throughput and IOPS
    limits (io.max: rbps/wbps/riops/wiops) via cgroups v2, while switching the CPU frequency governor
    (powersave/performance) according to pressure level. Resources are gradually restored once pressure drops.
2. Monitor: Collect CPU, memory, and I/O Pressure Stall Information (PSI) in real time, compute a
    composite pressure score, and map it to four levels (low/medium/high/critical). Intercept controlled
    app launches and exits via eBPF (execve hook). Independently detect disk I/O stress and identify
    the top disk I/O consumers.
3. Priority Queue: When pressure reaches critical level or disk I/O is busy, suspend pending app launches
    and insert them into a max-priority queue. Once resources recover, automatically launch queued apps in
    priority order. Manual cancellation of queued launches is also supported.
4. Keep-Alive: For Critical-priority controlled apps, lower the process oom_score_adj to reduce the
    probability of OOM kill, and continuously monitor their running processes to ensure stable operation.
5. Dashboard & SSE API: Support manual management of controlled apps via the React dashboard or REST/SSE API, including
    priority adjustment, cancellation of queued launches, resource limit configuration (CPU/memory/I/O),
    quota restoration, OOM score setup, and app deletion. Real-time updates delivered via Server-Sent Events (SSE).
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
        systemctl set-property --runtime session-116.scope  CPUQuota=  MemoryHigh= IOWeight=

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
        1. https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/6/html/resource_management_guide/starting_a_process Launch processes in a cgroup by running the cgexec command. For example, this command launches the firefox web browser within the group1 cgroup, subject to the limitations imposed on that group by the cpu subsystem:
        # cgexec -g cpu:group1 firefox http://www.redhat.com

        The syntax for cgexec is:
        # cgexec -g subsystems:path_to_cgroup command arguments

        2. Add a program's executables to cgroups-v2
          https://unix.stackexchange.com/questions/694812/is-there-any-other-way-to-add-program-to-cgroups-v2-instead-of-giving-their-pids
          # pidof firefox > /sys/fs/cgroup/Example/tasks/cgroup.procs


        3. Under Linux, you can use inotifywait to wait for an access or close_nowrite event on the executable, e.g. inotifywait -m -e access,close_nowrite --format=%e /bin/ls. There is an access event whenever the file is executed and a close_nowrite when the process dies. You can't get the process ID that way, so you'll then have to find out which processes have the file open (e.g. with fuser or lsof) and then filter the ones that are executing the file.

        systemctl list-units  -t help
        systemd-cgls  /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/
        ./lscgroup  -g misc://user.slice/user-1000.slice/user@1000.service/app.slice
        systemd-cgls
        lslogins -u
