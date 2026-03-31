#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

export LANG=en_US.UTF-8

if [ -z "$1" ]; then
    echo "请提供 Conda 环境名称作为参数，例如：./start_webui.sh ma_manager # ma_manager为conda环境名称"
    exit 1
fi

current_dir=$(dirname "$(realpath "$0")")
echo "Current folder: $current_dir"

conda_env_name=$1

cd "$current_dir"

# 定义清理函数
cleanup() {
    echo "Stopping..."
    # pkill -f "streamlit run --server.enableStaticServing true --server.port 8655 webui.py"
    pid=$(pgrep -f "streamlit run.*webui.py" | xargs ps -o pid=,lstart= -p | sort -k2 | head -n1 | awk '{print $1}')

    if [ -n "$pid" ]; then
        kill -9 "$pid" 2>/dev/null
    else
        echo "No matching process found."
    fi
    exit 1
}

# 捕获 Ctrl+C 并执行清理
trap cleanup SIGINT SIGTERM

echo "++++++webui+++++"
conda run -n $conda_env_name --no-capture-output bash -c "http_proxy= all_proxy= streamlit run --server.enableStaticServing true --server.port 8655 webui.py"