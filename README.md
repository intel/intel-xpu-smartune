# Multitask Resource Balancing Service (MRB)
This is multitask resource balancing service (MRB) for AI NAS, which is designed to dynamically adjust resource allocation
for various Apps(AI) based on their priority and system pressure.
It uses cgroups v2 to manage resources like CPU, memory, and I/O.

# Architecture:

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
    Build libcgroup wheel from source:
    1.  pip install Cython
    2.  sudo apt install libpam-dev flex bison libsystemd-dev
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

    conda create -n mt_py312 python=3.12.7
    conda activate mt_py312
    pip install -r requirements.txt
    pip install dist/libcgroup-3.2.0-cp310-cp310-linux_x86_64.whl (Refer above "Build libcgroup wheel from source")


# Start:
    python BalanceService.py
    python test/test_bservice.py(testing API.)



