mrb/
├── main.py               # Main entry point
├── config.py             # Configuration loader
├── monitor/              # Monitoring components
│   ├── psi.py              # PSI monitoring
│   ├── cgroup.py           # Cgroup metrics
│   └── system.py           # System-wide metrics
├── balancer/             # balancer logic
│   ├── priority.py         # Priority queue
│   └── balancer.py         # Workload balancing
├── controller/           # Adjustment components
│   ├── cpu.py              # CPU controller
│   ├── pressure.py         # Pressure scoring
│   ├── memory.py           # Memory controller
│   └── io.py               # I/O controller
├── tests/                # Unit tests
└── requirements.txt      # Dependencies


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


Build libcgroup wheel from source:
    1.  pip install Cython
    2.  sudo apt install libpam-dev
    3.  git clone https://github.com/libcgroup/libcgroup.git
    4.  cd libcgroup
    5.  git checkout v3.2.0 -b v3.2.0
    6.  ./bootstrap.sh
    7.  make
    8.  cd libcgroup/src/python
    9.  export VERSION_RELEASE="3.2.0"
    10. python setup.py bdist_wheel
    11. pip install dist/libcgroup-3.2.0-cp310-cp310-linux_x86_64.whl


sudo apt update && sudo apt install linux-tools-common cpufrequtils -y



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
