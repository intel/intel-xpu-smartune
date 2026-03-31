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


import logging
import os

class Logger:
    def __init__(self, log_file=None, log_level=logging.INFO):
        """
        初始化日志器。

        :param log_file: 日志文件路径，如果为 None，则输出到控制台。
        :param log_level: 日志级别，默认为 logging.INFO。
        """
        self.log_file = log_file
        self.log_level = log_level
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level)

        # 创建日志格式
        
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # 如果指定了日志文件，则输出到文件，否则输出到控制台
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            file_handler.stream.reconfigure(encoding="utf-8")
            self.logger.addHandler(file_handler)
            # Set UTF-8 encoding for the stream (Windows-specific fix)

        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.stream.reconfigure(encoding="utf-8")
        self.logger.addHandler(console_handler)

    def get_logger(self):
        return self.logger
    def info(self, message):
        self.logger.info(message)

    def debug(self, message):
        self.logger.debug(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)

log_file_path = "./multi_tasks.log"
logger = Logger(log_file=log_file_path, log_level=logging.DEBUG).get_logger()
# 测试日志类
def test_logger():
    
    logger.info("This is an info message.")
    logger.debug("This is a debug message.")
    logger.error("This is an error message.")
    logger.critical("This is a critical message.")

if __name__ == "__main__":
    test_logger()
