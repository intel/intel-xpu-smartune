# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

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

    def register_callback(self, register_url: str, callback_url: str, session) -> bool:
        """向Multi-Apps服务注册回调地址（业务逻辑层）"""
        try:
            response = session.post(
                register_url,
                json={"callback_url": callback_url},
                timeout=5
            )
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except Exception as e:
            print(f"Callback registration failed: {e}")
            return False

    def get_controlled_apps(self, url, session):
        """ Get controlled apps from multi-apps service.

        :return: list of controlled apps
        """
        try:
            response = session.post(url, json={}, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return []
        except requests.exceptions.RequestException as e:
            print('get_controlled_apps request error: ', e)
            return []

    def set_controlled_apps(self, url, app_data, session):
        """ Set controlled app in multi-apps service.

        :param app_data: dict with app control data
        :return: response from the service
        """
        try:
            response = session.post(url, json=app_data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return {}
        except requests.exceptions.RequestException as e:
            print('set_controlled_app request error: ', e)
            return {}

    def remove_controlled_apps(self, url, app_data, session):
        """ Remove controlled app from multi-apps service.

        :param app_data: dict with app control data
        :return: response from the service
        """
        try:
            response = session.post(url, json=app_data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return {}
        except requests.exceptions.RequestException as e:
            print('remove_controlled_app request error: ', e)
            return {}

    def get_priority_data(self, url, query_data, session):
        """ Get priority data for a specific app.

        :param query_data: dict with app_id or app_name
        :return: priority data dict
        """
        try:
            response = session.post(url, json=query_data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return {}
        except requests.exceptions.RequestException as e:
            print('get_priority_data request error: ', e)
            return {}

    def get_pending_apps(self, url, session):
        """ Get pending apps from multi-apps service.

        :return: list of pending apps
        """
        try:
            response = session.post(url, json={}, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return []
        except requests.exceptions.RequestException as e:
            print('get_controlled_apps request error: ', e)
            return []

    def cancel_relaunch(self, url, app_id, session):
        """ Cancel relaunch for a specific app.

        :param app_id: condition
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = session.post(url, json=data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return False
        except requests.exceptions.RequestException as e:
            print('cancel_relaunch request error: ', e)
            return False

    def resource_limit(self, url, app_id, app_name, priority, session):
        """ Resource limit for a specific app.

        :param app_id:
        :return:
        """
        data = {"app_id": app_id, "app_name": app_name, "priority": priority}
        try:
            response = session.post(url, json=data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return False
        except requests.exceptions.RequestException as e:
            print('resource_limit request error: ', e)
            return False

    def restore_resource(self, url, app_id, session):
        """ Restore resource for a specific app.

        :param app_id:
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = session.post(url, json=data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return False
        except requests.exceptions.RequestException as e:
            print('restore_resource request error: ', e)
            return False

    def set_priority(self, url, priority_data, session):
        """ Set priority for a specific app.

        :param priority_data: dict with app_id, priority, and optional cgroup
        :return: response from the service
        """
        try:
            response = session.post(url, json=priority_data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return {}
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return {}
        except requests.exceptions.RequestException as e:
            print('set_priority request error: ', e)
            return {}

    def keep_alive_app(self, url, app_id, session):
        """
        :param app_id: used to find the app to keep alive.
        :return:
        """
        data = {"app_id": app_id}
        try:
            response = session.post(url, json=data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return True
            return False
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return False
        except requests.exceptions.RequestException as e:
            print('set_priority request error: ', e)
            return False

    def get_apps(self, url, store, session):
        """ Get list of all apps from multi-apps service.

        :return: list of apps
        """
        data = {"store": store}
        try:
            response = session.get(url, json=data, timeout=5)
            response.raise_for_status()

            response_data = response.json()
            if "retcode" in response_data and response_data["retcode"] == BAL_retcode.SUCCESS:
                return response_data["data"]
            return []
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
            return []
        except requests.exceptions.RequestException as e:
            print('get_apps request error: ', e)
            return []
