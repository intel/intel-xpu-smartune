#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import subprocess
from subprocess import check_output, Popen, PIPE

def get_user_scopes(user):
    try:
        # Run the command and capture output
        #result = subprocess.run(['find', '/sys/fs/cgroup/user.slice/user-1000.slice', '-maxdepth', '1', '-type', 'd'],
        path = '/sys/fs/cgroup/user.slice/%s.slice' % user
        result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        # Split into lines and remove empty lines/headers
        scopes = [line.strip() for line in result.stdout.splitlines()
                  if line.strip() and not line.endswith('user-1000.slice')
                                  and not line.endswith('user@1000.service')]

        return scopes

    except subprocess.CalledProcessError as e:
        print(f"Error running lslogins: {e.stderr}")
        return []
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return []

def get_app_slices(user):
    try:
        # Run the command and capture output
        path = '/sys/fs/cgroup/user.slice/%s.slice/%s.service/app.slice' % (user, user)
        result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        # Split into lines and remove empty lines/headers
        apps = [line.strip() for line in result.stdout.splitlines()
                  if line.strip() and not line.endswith('app.slice')]

        return apps

    except subprocess.CalledProcessError as e:
        print(f"Error running lslogins: {e.stderr}")
        return []
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return []

if __name__ == "__main__":
    # command used to get active user slices
    __slices_cmd = "systemctl list-units user-*.slice | grep -oE 'user-[^ ]*.slice' || [ $? = 1 ]"
    # command to get scopes of the activate user slices
    #__scopes_cmd = "find /sys/fs/cgroup/user.slice/%s -maxdepth 1 -type d" % _slices_cmd

    active_user = check_output(__slices_cmd, shell=True, universal_newlines=True).splitlines()
    if active_user:
        active_user = active_user[0].strip('.slice')
    print(active_user)

    scopes = get_user_scopes(active_user)
    for scope in scopes:
        print(scope)

    apps = get_app_slices(active_user)
    for app in apps:
        print(app)
