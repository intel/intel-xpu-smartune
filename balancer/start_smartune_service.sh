#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# systemd 启动入口：由 balancer.service 调用，已经是 root 权限运行，
# 所以脚本内不再使用 sudo，也不安装 trap（生命周期交给 systemd）。

set -e
cd "$(dirname "$(readlink -f "$0")")"

export CERT_FILE="./b_server.crt"
export KEY_FILE="./b_server.key"

if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Certificate and key already exist. Skipping generation."
else
    echo "Generating certificate..."
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 365 -nodes -subj "/CN=localhost" \
        -addext "subjectAltName=IP:127.0.0.1"
    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
    echo "Certificate generated successfully"
fi

PYTHON_BIN="$(command -v python3)"
exec "$PYTHON_BIN" BalanceService.py
