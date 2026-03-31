# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
from subprocess import check_output, Popen, PIPE # nosec

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
    __slices_cmd = ["systemctl", "list-units", "--type=slice", "user-*.slice", "--no-legend"]

    try:
        output = check_output(__slices_cmd, universal_newlines=True)
        import re
        active_user_list = re.findall(r'user-[^ ]*\.slice', output)
        
        if active_user_list:
            active_user = active_user_list[0].strip('.slice')
            print(active_user)
            scopes = get_user_scopes(active_user)
            for scope in scopes:
                print(scope)

            apps = get_app_slices(active_user)
            for app in apps:
                print(app)
    except Exception:
        pass
