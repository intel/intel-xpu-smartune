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

def get_app_services(uid):
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
