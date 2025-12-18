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
