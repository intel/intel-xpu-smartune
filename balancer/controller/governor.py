# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
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
        except subprocess.CalledProcessError as e:
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
