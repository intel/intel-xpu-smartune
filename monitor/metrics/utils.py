# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared low-level helpers used across system_info sub-modules."""

import os
import subprocess # nosec
from typing import Any, Dict, List, Optional

from utils.logger import logger


def safe_read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as exc:
        logger.debug("Read failed for %s: %s", path, exc)
        return None


def run_cmd(cmd: List[str], timeout: int = 3) -> Optional[str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            stderr = res.stderr.strip() or res.stdout.strip()
            logger.debug("Command failed (%s): %s", " ".join(cmd), stderr)
            return None
        return res.stdout.strip()
    except Exception as exc:
        logger.debug("Command error (%s): %s", " ".join(cmd), exc)
        return None


def read_first_existing(paths: List[str]) -> Optional[str]:
    for path in paths:
        if os.path.exists(path):
            return safe_read(path)
    return None


def parse_freq_val(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
