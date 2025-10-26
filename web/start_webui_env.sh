#!/bin/bash
export LANG=en_US.UTF-8
current_dir=$(dirname "$(realpath "$0")")
cd "$current_dir"

cleanup() {
    echo "Cleaning up..." >&2

    # 杀死 Streamlit 及其所有子进程
    pkill -9 -P $(pgrep -f "streamlit run.*webui.py")
    pkill -9 -f "streamlit run.*webui.py"

    # 杀死 Flask 端口进程
    lsof -t -i :8656 -s TCP:LISTEN | xargs -r kill -9

    exit 1
}

trap 'cleanup' SIGINT SIGTERM

echo "Starting Streamlit..."

bash -c "http_proxy= all_proxy= streamlit run --server.enableStaticServing true --server.port 8655 webui.py" &
streamlit_pid=$!

wait "$streamlit_pid"