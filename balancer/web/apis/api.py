# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import requests
import threading
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify

from apis.multiapps_bridge import MABridge
from apis.systools import SingletonMeta

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

MULTIAPPS_URL = "https://127.0.0.1:9001"
B_CERT_FILE = os.getenv('B_CERT_FILE')
B_CERT_KEY = os.getenv('B_CERT_KEY')

CLIENT_URL = "https://127.0.0.1:8656"

client_app = Flask(__name__)


# 全局共享状态
class CallbackManager(metaclass=SingletonMeta):
    def __init__(self):
        self._last_callback: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._app_handlers = set()

    def add_to_handler(self, handler):
        """注册UI更新函数"""
        with self._lock:
            self._app_handlers.add(handler)

    def handle_callback(self, data: Dict[str, Any]):
        """处理回调并通知UI"""
        with self._lock:
            self._last_callback = data
            for handler in self._app_handlers:
                try:
                    handler(data)  # 触发所有注册的UI更新
                except Exception as e:
                    print(f"UI handler failed: {str(e)}")


callback_manager = CallbackManager()

# 回调处理路由
@client_app.route('/callback', methods=['POST'])
def handle_callback():
    try:
        data = request.get_json()
        print(f"[Client] Received callback: {data}")
        callback_manager.handle_callback(data)
        return jsonify({"status": "ok"}), 200  # 显式返回200
    except Exception as e:
        return jsonify({"status": str(e)}), 500


class Client_multiapps_api(metaclass=SingletonMeta):
    def __init__(self):
        self.ma_bridge = MABridge()
        self._callback_thread = None
        self._port = 8656  # Client回调用端口

        # Multi-Apps Startup
        self.app_get_controlled_url = MULTIAPPS_URL + '/app/get_controlled_app'
        self.app_set_controlled_url = MULTIAPPS_URL + '/app/set_to_control'
        self.app_remove_controlled_url = MULTIAPPS_URL + '/app/remove_from_control'
        self.app_get_priority_url = MULTIAPPS_URL + '/app/get_priority_data'
        self.app_set_priority_url = MULTIAPPS_URL + '/app/set_priority'
        self.app_set_oom_score_url = MULTIAPPS_URL + '/app/set_oom_score'
        self.app_cancel_relaunch_url = MULTIAPPS_URL + '/app/cancel_relaunch'
        self.app_resource_limit_url = MULTIAPPS_URL + '/app/resource_limit'
        self.app_resource_restore_url = MULTIAPPS_URL + '/app/resource_restore'
        self.app_get_pending_url = MULTIAPPS_URL + '/app/get_pending_app'
        self.app_obtain_url = MULTIAPPS_URL + '/app/get_apps'
        self.app_workload_url = MULTIAPPS_URL + '/task/add_workload'
        self.app_register_callback_url = MULTIAPPS_URL + '/app/register_callback'

        self.session = self._create_session()

    def _create_session(self):
        """Create a requests session with retry strategy and SSL configuration."""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)

        if not B_CERT_FILE:
            raise EnvironmentError(
                "B_CERT_FILE environment variable is not set. "
                "TLS certificate verification cannot be enabled."
            )
        if not os.path.exists(B_CERT_FILE):
            raise FileNotFoundError(
                f"Certificate file '{B_CERT_FILE}' not found. "
                "TLS certificate verification cannot be enabled. "
                "Please check 'start_webui_env.sh' to export B_CERT_FILE."
            )
        session.verify = B_CERT_FILE
        print(f"TLS certificate verification enabled using: {B_CERT_FILE}")

        return session

# Multi-apps API:
    def register_callback(self):
        """
        :param app_name:
        :return:
        """
        return self.ma_bridge.register_callback(self.app_register_callback_url, f"{CLIENT_URL}/callback", self.session)

    def get_controlled_apps(self):
        """
        :return: Get all the controlled apps.
        """

        return self.ma_bridge.get_controlled_apps(self.app_get_controlled_url, self.session)

    def set_controlled_apps(self, app_data):
        """
        :param app_data: Dictionary containing app control data.
        :return: Set the control status of an app.
        """
        res_data = self.ma_bridge.set_controlled_apps(self.app_set_controlled_url, app_data, self.session)
        return res_data

    def remove_controlled_apps(self, app_data):
        """
        :param app_data: Dictionary containing app control data.
        :return: Remove the control status of an app.
        """
        return self.ma_bridge.remove_controlled_apps(self.app_remove_controlled_url, app_data, self.session)

    def get_priority_data(self, query_data):
        """
        :param query_data: Dictionary containing app_id or app_name.
        :return: Get priority data for a specific app.
        """
        return self.ma_bridge.get_priority_data(self.app_get_priority_url, query_data, self.session)

    def set_priority(self, priority_data):
        """
        :param priority_data: Dictionary containing app_id, priority, and optional cgroup.
        :return: Set the priority of an app.
        """
        return self.ma_bridge.set_priority(self.app_set_priority_url, priority_data, self.session)

    def keep_alive_app(self, app_id):
        """
        :param app_id: used to find the app to keep alive.
        :return:
        """
        return self.ma_bridge.keep_alive_app(self.app_set_oom_score_url, app_id, self.session)

    def cancel_relaunch(self, app_id):
        """
        :param app_id: according to app_id to cancel relaunch.
        :return: success or not
        """
        return self.ma_bridge.cancel_relaunch(self.app_cancel_relaunch_url, app_id, self.session)

    def resource_limit(self, app_id, app_name, priority):
        """
        :param app_id: according to app_id to do the resource limit.
        :return:
        """
        return self.ma_bridge.resource_limit(self.app_resource_limit_url, app_id, app_name, priority, self.session)

    def restore_resource(self, app_id):
        """
        :param app_id: according to app_id to do the resource restore.
        :return:
        """
        return self.ma_bridge.restore_resource(self.app_resource_restore_url, app_id, self.session)

    def get_pending_apps(self):
        """
        :return: Get all the pending apps.
        """

        return self.ma_bridge.get_pending_apps(self.app_get_pending_url, self.session)

    def get_apps(self, store=False):
        """
        :return: Get the list of all apps.
        """
        return self.ma_bridge.get_apps(self.app_obtain_url, store, self.session)


    def start_client_callback(self) -> bool:
        """启动回调服务（确保线程单例）"""
        # 检查线程是否已存在且存活
        if self._callback_thread is not None and self._callback_thread.is_alive():
            print("[Callback] Server is already running")
            return True

        # 启动线程
        try:
            c_ssl_context = (B_CERT_FILE, B_CERT_KEY)
            self._callback_thread = threading.Thread(
                target=client_app.run,
                kwargs={"host": "127.0.0.1", "port": self._port, "debug": False, "ssl_context": c_ssl_context},
                daemon=True
            )
            self._callback_thread.start()
            print(f"[Callback] Server started on port {self._port}, and registered callback method to server")
            res = self.register_callback()
            return res
        except Exception as e:
            print(f"[Callback] Failed to start server: {str(e)}")
            return False
