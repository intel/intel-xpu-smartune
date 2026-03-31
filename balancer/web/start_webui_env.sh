#!/bin/bash
set -euo pipefail
export LANG=en_US.UTF-8

# 获取脚本所在目录
project_root=$(dirname "$(dirname "$(realpath "$0")")")
current_dir=$(dirname "$(realpath "$0")")

cd "$current_dir" || exit 1

# 设置环境变量
export B_CERT_FILE="$project_root/b_server.crt"
export B_CERT_KEY="$project_root/b_server.key"

# 配置参数
STREAMLIT_PORT=8655
FLASK_PORT=8656
STREAMLIT_CMD="streamlit run --server.enableStaticServing true --server.port $STREAMLIT_PORT webui.py"

cleanup() {
    echo "Cleaning up..." >&2

    # 1. 杀死Streamlit进程
    if pgrep -f "$STREAMLIT_CMD" >/dev/null; then
        echo "Killing Client process..."
        pkill -9 -f "$STREAMLIT_CMD" || true
    fi

    # 2. 杀死Flask端口进程
    if lsof -t -i :$FLASK_PORT -s TCP:LISTEN >/dev/null; then
        echo "Killing Flask port $FLASK_PORT..."
        lsof -t -i :$FLASK_PORT -s TCP:LISTEN | xargs -r kill -9 || true
    fi

    exit 1
}

# 设置信号捕获
trap 'cleanup' SIGINT SIGTERM ERR

echo "Starting UI..."

# 启动Streamlit
env http_proxy= all_proxy= \
    bash -c "$STREAMLIT_CMD" &
streamlit_pid=$!

# 等待进程结束
wait $streamlit_pid || {
    echo "Client UI process exited abnormally" >&2
    cleanup
}

exit 0