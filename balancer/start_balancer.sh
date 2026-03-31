#!/bin/bash

# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

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

    if [ $? -eq 0 ]; then
        echo "Certificate generated successfully"
        chmod 644 "$CERT_FILE"
        chmod 600 "$KEY_FILE"
    else
        echo "Certificate generation failed" >&2
        exit 1
    fi
fi

sudo -E python3 BalanceService.py
