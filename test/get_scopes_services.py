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
    # command used to get active user slices
    __slices_cmd = "systemctl list-units user-*.slice | grep -oE 'user-[^ ]*.slice' || [ $? = 1 ]"

    active_user = check_output(__slices_cmd, shell=True, universal_newlines=True).splitlines()
    if active_user:
        uid = active_user[0].strip('user-').strip('.slice')

    scopes = get_user_scopes(uid)
    for scope in scopes:
        print(scope)

    apps = get_app_services(uid)
    for app in apps:
        print(app)
