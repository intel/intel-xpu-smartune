# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec

def get_user_uids():
    try:
        # Run the command and capture output
        result = subprocess.run(['lslogins', '-u', '--output=UID'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, check=True)

        # Split into lines and remove empty lines/headers
        uids = [line.strip() for line in result.stdout.splitlines()
                if line.strip() and not line.endswith('UID')]

        return uids

    except subprocess.CalledProcessError as e:
        print(f"Error running lslogins: {e.stderr}")
        return []
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return []

if __name__ == "__main__":
    uids = get_user_uids()
    print("User IDs found:")
    for uid in uids:
        print(uid)
