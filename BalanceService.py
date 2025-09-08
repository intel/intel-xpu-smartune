import signal
import requests
from balancer.balancer import DynamicBalancer
from utils.logger import logger
from utils.app_utils import callback_manager
from flask import Flask, request
from threading import Lock
from gi.repository import Gio
from datetime import datetime
from utils.http_utils import RetCode, construct_response
from db.DatabaseModel import AIAppPriority, DBStatus, init_database

app = Flask(__name__)
_service_lock = Lock()
_service = None  # 单例服务实例


class DynamicService:
    """将核心逻辑封装在服务类中"""

    def __init__(self):
        self.balancer = DynamicBalancer()

    def start(self):
        self.balancer.start()

    def add_workload(self, priority, payload):
        """直接代理到balancer"""
        self.balancer.add_workload(priority, payload)

    def add_control(self, app_name):
        self.balancer.bpf_monitor.add_to_monitorlist(app_name)

    def remove_control(self, app_name):
        self.balancer.bpf_monitor.remove_from_monitorlist(app_name)

    def shutdown(self):
        self.balancer.shutdown()


def start_service():
    """初始化服务并设置信号处理"""
    global _service
    with _service_lock:
        if _service is None:
            _service = DynamicService()
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            _service.start()
    return _service

def _handle_signal(signum, frame):
    if _service:
        _service.shutdown()
    raise SystemExit(0)


@app.route('/task/add_workload', methods=['POST'])
def add_workload():
    """优化后的API接口"""
    try:
        data = request.json
        _service.add_workload(
            priority=data['priority'],
            payload=data['payload']
        )
        return construct_response(
            retmsg="Workload added successfully",
            data={"status": "success"}
        )
    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg=f"Invalid request: {str(e)}"
        )


@app.route('/app/get_apps', methods=['GET', 'POST'])
def get_apps():
    """获取系统所有应用列表并同步到数据库"""
    try:
        data = request.get_json()
        store = data.get('store', False)

        apps = Gio.AppInfo.get_all()
        app_list = []

        for app in apps:
            app_data = {
                "name": app.get_name(),  # Calculator
                "app_id": app.get_id(),  # org.gnome.Calculator.desktop
                "cmdline": app.get_commandline() or ""  # gnome-calculator
            }
            app_list.append(app_data)

            if store:
                # 检查应用是否已存在
                app_id = app.get_id()
                existing_app = None

                try:
                    existing_app = AIAppPriority.query().where(AIAppPriority.app_id == app_id).get()
                except Exception as e:
                    print(f"App - {app_id} will be managed.")

                if not existing_app:
                    # 仅当应用不存在时才插入
                    AIAppPriority.insert_record(
                        id=app_id.replace('.desktop', ''),
                        app_id=app_id,
                        name=app.get_name(),
                        priority=0,  # 默认优先级
                        remark="",
                        cmdline=app.get_commandline(),
                        status="stopped",
                        last_update_time=datetime.now()
                    )

        return construct_response(
            data=app_list,
            retmsg="Successfully retrieved app list"
        )
    except Exception as e:
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e),
            data={}
        )


@app.route('/app/set_priority', methods=['POST'])
def set_priority():
    """设置应用优先级（使用新的数据库操作方法）"""
    try:
        data = request.get_json()
        app_id = data.get('app_id')
        priority = data.get('priority')

        if not all([app_id, priority]):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Missing required parameters"
            )

        app_info = Gio.DesktopAppInfo.new(app_id)
        if not app_info:
            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg="Application not found"
            )

        # 使用新的update_record方法
        result = AIAppPriority.update_record(
            id=app_id.replace('.desktop', ''),
            priority=priority,
            up_time=datetime.now()
        )

        logger.info(f"result : {result}")
        if result == DBStatus.NOT_FOUND:
            # 如果记录不存在则创建
            AIAppPriority.insert_record(
                id=app_id.replace('.desktop', ''),
                app_id=app_id,
                name=app_info.get_name(),
                priority=priority,
                cgroup="user",
                remark="",
                cmdline=app_info.get_commandline(),
                up_time=datetime.now()
            )

        return construct_response(
            data={},
            retmsg="Priority updated successfully"
        )
    except Exception as e:
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/get_priority_data', methods=['POST'])
def get_priority_data():
    """根据 app_id 或 name 获取应用的优先级设置（支持同时查询）"""

    try:
        data = request.get_json()
        app_id = data.get('app_id', "")
        name = data.get('app_name', "")

        # 构建 OR 查询条件
        query = AIAppPriority.query()
        conditions = []
        if app_id:
            conditions.append(AIAppPriority.app_id == app_id)
        if name:
            conditions.append(AIAppPriority.name == name)

        query = query.where(conditions[0])
        record = query.first()

        if not record:
            # 生成更友好的错误提示
            not_found_msg = "未找到匹配的应用"
            if app_id and name:
                not_found_msg = f"未找到 app_id={app_id} 或 name={name} 的应用"
            elif app_id:
                not_found_msg = f"未找到 app_id={app_id} 的应用"
            elif name:
                not_found_msg = f"未找到 name={name} 的应用"

            return construct_response(
                data={},
                retcode=RetCode.NOT_EXISTING,
                retmsg=not_found_msg
            )


        # 返回标准化数据结构
        priority_data = {
            "id": record.id,
            "app_id": record.app_id,
            "name": record.name,
            "priority": record.priority,
            "cgroup": record.cgroup,
            "remark": record.remark,
            "cmdline": record.cmdline,
            "up_time": record.up_time.isoformat() if record.up_time else None,
            "status": record.status
        }

        return construct_response(
            data=priority_data,
            retmsg="Successfully retrieved priority data"
        )
    except Exception as e:
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/set_to_control', methods=['POST'])
def set_to_control():
    """设置应用管控状态并添加到监控列表"""
    try:
        data = request.get_json()
        app_name = data.get('app_name', "")
        app_id = data.get('app_id', "")
        controlled = data.get('controlled', True)  # 默认为True（启用管控）
        cgroup = data.get('cgroup', '')
        priority = data.get('priority', 0)
        remark = data.get('remark', '')

        _service.add_control(app_name)

        # 更新或创建数据库记录
        result = AIAppPriority.update_record(
            id=app_id.replace('.desktop', ''),
            controlled=controlled,
            priority=priority,
            cgroup=cgroup,
            remark=remark,
        )

        if result == DBStatus.NOT_FOUND:
            AIAppPriority.insert_record(
                id=app_id.replace('.desktop', ''),
                app_id=app_id,
                name=app_name,
                priority=priority,  # 默认优先级
                controlled=controlled,
                cgroup=cgroup,
                remark=remark,
                cmdline="",
                status="stopped",
                last_update_time=datetime.now()
            )

        return construct_response(
            data={
                "app_name": app_name,
                "controlled": controlled,
            },
            retmsg=f"App control {'enabled' if controlled else 'disabled'} and added to monitor"
        )
    except Exception as e:
        logger.error(f"Control set failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/remove_from_control', methods=['POST'])
def remove_from_control():
    """从管控列表中移除应用"""
    try:
        data = request.get_json()
        app_id = data.get('app_id', "")
        app_name = data.get('app_name', "")

        # 验证必要参数
        if not app_id and not app_name:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Either app_id or app_name must be provided"
            )

        # 从监控服务中移除
        _service.remove_control(app_name if app_name else "")

        # 更新数据库记录（将controlled设为False）
        AIAppPriority.update_record(
            id=app_id.replace('.desktop', '') if app_id else "",
            controlled=False
        )

        return construct_response(
            data={
                "app_id": app_id,
                "app_name": app_name,
                "controlled": False
            },
            retmsg="App removed from control successfully"
        )
    except Exception as e:
        logger.error(f"Remove control failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/get_controlled_app', methods=['POST'])
def get_controlled_app():
    """获取所有受管控应用并添加到服务监控列表"""
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)

        if not controlled_apps:
            return construct_response(
                retcode=RetCode.NOT_EXISTING,
                retmsg="No controlled apps found",
                data=[]
            )

        # 处理结果并添加到服务监控
        result_data = []
        for app in controlled_apps:
            result_data.append({
                "app_id": app.app_id,
                "app_name": app.name,
                "controlled": app.controlled,
                "priority": app.priority,
                "cgroup": app.cgroup,
                "remark": app.remark,
            })

        return construct_response(
            data=result_data,
            retmsg=f"Found {len(result_data)} controlled apps"
        )
    except Exception as e:
        logger.error(f"Get controlled apps failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@app.route('/app/register_callback', methods=['POST'])
def register_callback():
    """注册全局回调地址"""
    try:
        data = request.get_json()
        callback_url = data.get('callback_url')

        if not callback_url:
            return construct_response(
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="callback_url is required"
            )

        callback_manager.register_callback_url(callback_url)
        return construct_response(
            data={"callback_url": callback_url},
            retmsg="Global callback URL registered"
        )

    except Exception as e:
        return construct_response(
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


def main():
    init_database()
    # 预初始化服务
    start_service()
    app.run(host="0.0.0.0", port=9001, debug=False)


if __name__ == "__main__":
    main()
