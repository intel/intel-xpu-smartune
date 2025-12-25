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
from enum import IntEnum
import requests


class BAL_retcode(IntEnum):
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

    def register_callback(self, register_url: str, callback_url: str) -> bool:
        """向Multi-Apps服务注册回调地址（业务逻辑层）"""
        try:
            response = requests.post(
                register_url,
                json={"callback_url": callback_url},
                timeout=5
            )
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except Exception as e:
            print(f"Callback registration failed: {e}")
            return False

    def get_controlled_apps(self, url):
        """ Get controlled apps from multi-apps service.

        :return: list of controlled apps
        """
        try:
            response = requests.post(url, json={})
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.RequestException as e:
            print('get_controlled_apps request error: ', e)
            return []

    def set_controlled_apps(self, url, app_data):
        """ Set controlled app in multi-apps service.

        :param app_data: dict with app control data
        :return: response from the service
        """
        try:
            response = requests.post(url, json=app_data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.RequestException as e:
            print('set_controlled_app request error: ', e)
            return {}

    def remove_controlled_apps(self, url, app_data):
        """ Remove controlled app from multi-apps service.

        :param app_data: dict with app control data
        :return: response from the service
        """
        try:
            response = requests.post(url, json=app_data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.RequestException as e:
            print('remove_controlled_app request error: ', e)
            return {}

    def get_priority_data(self, url, query_data):
        """ Get priority data for a specific app.

        :param query_data: dict with app_id or app_name
        :return: priority data dict
        """
        try:
            response = requests.post(url, json=query_data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.RequestException as e:
            print('get_priority_data request error: ', e)
            return {}

    def get_pending_apps(self, url):
        """ Get pending apps from multi-apps service.

        :return: list of pending apps
        """
        try:
            response = requests.post(url, json={})
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.RequestException as e:
            print('get_controlled_apps request error: ', e)
            return []

    def cancel_relaunch(self, url, app_id):
        """ Cancel relaunch for a specific app.

        :param app_id: condition
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = requests.post(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.RequestException as e:
            print('cancel_relaunch request error: ', e)
            return False

    def resource_limit(self, url, app_id, app_name, priority):
        """ Resource limit for a specific app.

        :param app_id:
        :return:
        """
        data = {"app_id": app_id, "app_name": app_name, "priority": priority}
        try:
            response = requests.post(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.RequestException as e:
            print('resource_limit request error: ', e)
            return False

    def restore_resource(self, url, app_id):
        """ Restore resource for a specific app.

        :param app_id:
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = requests.post(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.RequestException as e:
            print('restore_resource request error: ', e)
            return False

    def set_priority(self, url, priority_data):
        """ Set priority for a specific app.

        :param priority_data: dict with app_id, priority, and optional cgroup
        :return: response from the service
        """
        try:
            response = requests.post(url, json=priority_data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.RequestException as e:
            print('set_priority request error: ', e)
            return {}

    def keep_alive_app(self, url, app_id):
        """
        :param app_id: used to find the app to keep alive.
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = requests.post(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.RequestException as e:
            print('set_priority request error: ', e)
            return False

    def get_apps(self, url, store):
        """ Get list of all apps from multi-apps service.

        :return: list of apps
        """
        data = {"store": store}
        try:
            response = requests.get(url, json=data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.RequestException as e:
            print('get_apps request error: ', e)
            return []

    def add_workload(self, url, workload_data):
        """ Add workload to the multi-apps service.

        :param workload_data: dict with workload details
        :return: response from the service
        """
        try:
            response = requests.post(url, json=workload_data)
            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.RequestException as e:
            print('add_workload request error: ', e)
            return {}
