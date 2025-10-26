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
    server:
        Start a terminal w/o any virtual(like conda) env, then run:
            sudo apt install python3-pip (optional)
            sudo pip install psutil>=5.5.1 --break-system-packages
            sudo pip install peewee==3.17.8 --break-system-packages
            sudo pip install flask --break-system-packages
            # sudo pip install flask --break-system-packages --ignore-installed blinker(err with "Cannot uninstall blinker...")
    
    client: 
        1.  Start a new terminal to run:
            bash Miniforge3-Linux-x86_64.sh (Prepare the package)
            conda create -n mt_py312 python=3.12.7
            conda activate mt_py312
            pip install -r requirements.txt

        2. pip install dist/libcgroup-3.2.0-cp312-cp312-linux_x86_64.whl(Probably no need, 
                but if need, please refer to "Other" below to generate whl)

    Other:
        Build libcgroup wheel from source:
        0.  
            # Go into "base" env, then check python version and upgrade to python3.12.7 with: 
            # conda install -n base python=3.12.7
            # pip install --upgrade pip # if need, currently is 25.2
        1.  pip install Cython
        2.  sudo apt install libpam-dev flex bison libsystemd-dev cmake build-essential autoconf automake libtool m4
            sudo apt install linux-tools-common cpufrequtils -y
        3.  git clone https://github.com/libcgroup/libcgroup.git
        4.  cd libcgroup
        5.  git checkout v3.2.0 -b v3.2.0
        6.  ./bootstrap.sh(sudo apt-get --reinstall install gcc g++ // issue: /usr/include/c++/14/mutex:768:23: internal compiler error: Segmentation fault)
        7.  make
        8.  cd libcgroup/src/python
        9.  export VERSION_RELEASE="3.2.0"
        10. python setup.py bdist_wheel
        11. pip install dist/libcgroup-3.2.0-cp312-cp312-linux_x86_64.whl
 
# Start:
    1. server:
        sudo python3 BalanceService.py
    2. client:
        cd web
        ./start_webui.sh mt_py312
        
        



