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


import os
import subprocess
from utils.logger import logger
from config.config import b_config

class GovernorController:
    def __init__(self):
        self.config = b_config

    def __get_governor(self):
        try:
            base_cmd = ["cpupower", "frequency-info", "-p"]
            cmd = ["sudo", *base_cmd] if getattr(self.config, "vendor", "") == "generic" else base_cmd
            result = subprocess.run(
                cmd,
                capture_output = True,
                text = True
            )

            for line in result.stdout.split('\n'):
                if 'governor' in line.lower():
                    return line.split('"')[1]
        except subprocess.CalledProcessError as e:
            logger.warning("Install cpupower first: sudo apt install linux-tools-common")

        return None

    def __set_governor(self, governor="performance"):
        curr_governor = self.__get_governor()
        if curr_governor == governor:
            logger.info(f"CPU governor already set to {governor}")
            return

        logger.info(f"Setting CPU governor to {governor}...")
        try:
            subprocess.run(
                ["cpupower", "frequency-set", "-g", governor],
                check = True,
                text = True,
                stdout = subprocess.DEVNULL
            )
            #print(f"All CPUs set to {governor} mode")
        except subprocess.CalledProcessError as e:
            #print(f"Error: {e}")
            logger.warning(f"Error: {e}")

    def set_performance(self) -> bool:
        """Set CPU governor to performance"""
        try:
            self.__set_governor("performance")
            return True
        except (FileNotFoundError, PermissionError):
            return False

    def set_powersave(self) -> bool:
        """Set CPU governor to powersave"""
        try:
            self.__set_governor("powersave")
            return True
        except (FileNotFoundError, PermissionError):
            return False
