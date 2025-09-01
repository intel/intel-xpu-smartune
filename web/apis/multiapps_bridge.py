import os
from enum import IntEnum
import requests


class HS_retcode(IntEnum):
    SUCCESS = 0
    NOT_EFFECTIVE = 10
    EXCEPTION_ERROR = 100
    ARGUMENT_ERROR = 101
    DATA_ERROR = 102
    OPERATING_ERROR = 103
    CONNECTION_ERROR = 105
    RUNNING = 106
    PERMISSION_ERROR = 108
    AUTHENTICATION_ERROR = 109
    UNAUTHORIZED = 401
    NOT_EXISTING = 404
    SERVER_ERROR = 500


class MABridge:

    def get_controlled_apps(self, url):
        """ Get controlled apps from multi-apps service.

        :return: list of controlled apps
        """
        data = {}
        try:
            response = requests.post(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == HS_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.RequestException as e:
            print('get_controlled_apps request error: ', e)
            return []
