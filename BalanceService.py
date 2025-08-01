import signal
from time import sleep
from config import Config
from balancer.balancer import DynamicBalancer
from utils.logger import logger
from flask import Flask, request, jsonify
from threading import Lock
from gi.repository import Gio
from datetime import datetime
from enum import Enum
from db.DatabaseModel import AIAppPriority, DBStatus, init_database

app = Flask(__name__)
_service_lock = Lock()
_service = None  # 单例服务实例

class APIStatus(Enum):
    SUCCESS = 0
    FAILED = 1
    INVALID_PARAM = 2
    NOT_FOUND = 3


class DynamicService:
    """将核心逻辑封装在服务类中"""

    def __init__(self, config_path="config.yaml"):
        self.balancer = DynamicBalancer(config_path)

    def start(self):
        """启动服务线程"""
        self.balancer.start()

    def add_workload(self, priority, payload):
        """直接代理到balancer"""
        self.balancer.add_workload(priority, payload)

    def shutdown(self):
        """关闭服务"""
        self.balancer.shutdown()


def start_service(config_path="config.yaml"):
    """初始化服务并设置信号处理"""
    global _service
    with _service_lock:
        if _service is None:
            _service = DynamicService(config_path)

            # 信号处理
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

            _service.start()
    return _service

def _handle_signal(signum, frame):
    """信号处理函数"""
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
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return jsonify({"error": "invalid request"}), 400


@app.route('/app/get_apps', methods=['GET', 'POST'])
def get_apps():
    """获取系统所有应用列表并同步到数据库"""
    try:
        apps = Gio.AppInfo.get_all()
        app_list = []

        for app in apps:
            app_data = {
                "name": app.get_name(),
                "id": app.get_id(),
                "cmdline": app.get_commandline() or ""
            }
            app_list.append(app_data)

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
                    priority='low',  # 默认优先级
                    cmdline=app.get_commandline(),
                    last_update_time=datetime.now()
                )


        return jsonify({
            "code": APIStatus.SUCCESS.value,
            "data": app_list,
            "message": "success"
        })

    except Exception as e:
        return jsonify({
            "code": APIStatus.FAILED.value,
            "data": [],
            "message": str(e)
        }), 500


@app.route('/app/set_priority', methods=['POST'])
def set_priority():
    """设置应用优先级（使用新的数据库操作方法）"""
    try:
        data = request.get_json()
        app_id = data.get('app_id')
        priority = data.get('priority')
        cgroup = data.get('cgroup', '')

        if not all([app_id, priority]):
            return jsonify({
                "code": APIStatus.INVALID_PARAM.value,
                "message": "Missing required parameters"
            }), 400

        app_info = Gio.DesktopAppInfo.new(app_id)
        if not app_info:
            return jsonify({
                "code": APIStatus.NOT_FOUND.value,
                "message": "Application not found"
            }), 404

        # 使用新的update_record方法
        result = AIAppPriority.update_record(
            id=app_id.replace('.desktop', ''),
            priority=priority,
            cgroup=cgroup,
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
                cgroup=cgroup,
                cmdline=app_info.get_commandline(),
                up_time=datetime.now()
            )

        return jsonify({
            "code": APIStatus.SUCCESS.value,
            "message": "Priority updated successfully"
        })

    except Exception as e:
        return jsonify({
            "code": APIStatus.FAILED.value,
            "message": str(e)
        }), 500


@app.route('/app/get_priority', methods=['POST'])
def get_priority_by_app_id():
    """根据app_id获取单个应用的优先级设置"""

    try:
        record = None
        data = request.get_json()
        app_id = data.get('app_id')

        try:
            record = AIAppPriority.query().where(AIAppPriority.app_id == app_id).first()
        except Exception as e:
            print(f"App - {app_id} not found.")

        if not record:
            return jsonify({
                "code": APIStatus.NOT_FOUND.value,
                "data": None,
                "message": f"App with id {app_id} not found"
            }), 404

        priority_data = {
            "id": record.id,
            "app_id": record.app_id,
            "name": record.name,
            "priority": record.priority,
            "cgroup": record.cgroup,
            "cmdline": record.cmdline,
            "up_time": record.up_time.isoformat() if record.up_time else None,
            "status": record.status
        }

        return jsonify({
            "code": APIStatus.SUCCESS.value,
            "data": priority_data,
            "message": "success"
        })

    except Exception as e:
        return jsonify({
            "code": APIStatus.FAILED.value,
            "data": None,
            "message": str(e)
        }), 500




def main():
    init_database()
    # 预初始化服务
    start_service()
    app.run(host="0.0.0.0", port=9001, debug=False)


if __name__ == "__main__":
    main()
