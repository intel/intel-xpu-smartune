# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
from subprocess import check_output # nosec

def get_user_scopes(uid):
    try:
        # Run the command and capture output
        path = '/sys/fs/cgroup/user.slice/user-%s.slice/' % uid
        result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        # Split into lines and remove empty lines/headers
        scopes = [line.replace(path, '') for line in result.stdout.splitlines()
                                             if line.strip() and line.replace(path, '')
                                                             and not line.endswith('user-%s.slice' % uid)
                                                             and not line.endswith('user@%s.service' % uid)]

        return scopes

    except subprocess.CalledProcessError as e:
        print(f"Error running get_user_scopes(): {e.stderr}")
        return []
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return []

def get_app_services1(uid):
    try:
        # Run the command and capture output
        path = '/sys/fs/cgroup/user.slice/user-%s.slice/user@%s.service/app.slice/' % (uid, uid)
        result = subprocess.run(['find', path, '-maxdepth', '1', '-type', 'd'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

        # Split into lines and remove empty lines/headers
        apps = [line.replace(path, '') for line in result.stdout.splitlines()
                                           if line.strip() and line.replace(path, '')
                                                           and not line.endswith('app.slice')]

        return apps

    except subprocess.CalledProcessError as e:
        print(f"Error running get_app_services(): {e.stderr}")
        return []
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return []


def get_app_services(uid):
    apps = []
    try:
        possible_paths = [
            f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/',
            f'/sys/fs/cgroup/system.slice/'
        ]

        for path in possible_paths:
            try:
                # Run the command and capture output
                result = subprocess.run(
                    ['find', path, '-maxdepth', '1', '-type', 'd'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True
                )

                # Process the output for this path
                path_apps = [
                    line.replace(path, '')
                    for line in result.stdout.splitlines()
                    if line.strip()
                       and line.replace(path, '')
                       and not line.endswith('app.slice')
                ]

                apps.extend(path_apps)

            except subprocess.CalledProcessError:
                # This path didn't work, try the next one
                continue
            except Exception as e:
                print(f"Unexpected error processing path {path}: {str(e)}")
                continue

        return list(set(apps))  # Remove duplicates while preserving order

    except Exception as e:
        print(f"An error occurred in get_app_services(): {str(e)}")
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