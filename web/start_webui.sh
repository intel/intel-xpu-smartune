#!/bin/bash

export LANG=en_US.UTF-8

if [ -z "$1" ]; then
    echo "请提供 Conda 环境名称作为参数，例如：./start_webui.sh ma_manager # ma_manager为conda环境名称"
    exit 1
fi

current_dir=$(dirname "$(realpath "$0")")
echo "Current folder: $current_dir"

conda_env_name=$1

cd "$current_dir"

echo "++++++webui+++++"
conda run -n $conda_env_name --no-capture-output bash -c "http_proxy= all_proxy= streamlit run --server.enableStaticServing true --server.port 8655 webui.py"