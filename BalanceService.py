import signal
from time import sleep
from config import Config
from balancer.balancer import DynamicBalancer
from utils.logger import logger
from flask import Flask, request, jsonify
from threading import Lock

app = Flask(__name__)
_service_lock = Lock()
_service = None  # 单例服务实例

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

# 预初始化服务
start_service()

def _handle_signal(signum, frame):
    """信号处理函数"""
    if _service:
        _service.shutdown()
    raise SystemExit(0)


@app.route('/add_workload', methods=['POST'])
def api_add_workload():
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


def main():
    app.run(host="0.0.0.0", port=9001, debug=False)


if __name__ == "__main__":
    main()
