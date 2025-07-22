import os
import subprocess
from utils.logger import logger

class GovernorController:
    def __init__(self, cgroup_mount: str):
        self.cgroup_mount = cgroup_mount
        #self.cpu = cpuFreq()

    def __get_governor(self):
        try:
            result = subprocess.run(
                ["sudo", "cpupower", "frequency-info", "-p"],
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
            # logger.info("current gov: %s, target gov: %s", curr_governor, governor)
            return

        try:
            subprocess.run(
                ["sudo", "cpupower", "frequency-set", "-g", governor],
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
