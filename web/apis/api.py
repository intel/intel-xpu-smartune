from apis.multiapps_bridge import MABridge
from apis.systools import SingletonMeta

MULTIAPPS_URL = "http://localhost:9001"


class Client_multiapps_api(metaclass=SingletonMeta):
    def __init__(self):
        self.ma_bridge = MABridge()

        # Multi-Apps Startup
        self.app_get_controlled_url = MULTIAPPS_URL + '/app/get_controlled_app'
        self.app_set_controlled_url = MULTIAPPS_URL + '/app/set_to_control'
        self.app_get_priority_url = MULTIAPPS_URL + '/app/get_priority_data'
        self.app_set_priority_url = MULTIAPPS_URL + '/app/set_priority'
        self.app_obtain_url = MULTIAPPS_URL + '/app/get_apps'
        self.app_workload_url = MULTIAPPS_URL + '/task/add_workload'


# Multi-apps API:
    def get_controlled_apps(self):

        return self.ma_bridge.get_controlled_apps(self.app_get_controlled_url)
