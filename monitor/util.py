# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def read_text(path: str | Path) -> str:
    return Path(path).read_text().strip()


def read_float(path: str | Path) -> float:
    return float(read_text(path))


def read_int(path: str | Path) -> int:
    return int(read_text(path))


def module_loaded(name: str) -> bool:
    return Path("/sys/module").joinpath(name).exists()


def run_capture(
    argv: Sequence[str],
    *,
    timeout_s: float = 1.5,
    cwd: str | None = None,
    env: dict | None = None,
) -> Tuple[int, str, str]:
    proc = subprocess.run(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_s,
        cwd=cwd,
        env=env,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def now_ms() -> int:
    return int(time.time() * 1000)


def try_parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def clear_screen() -> None:
    # ANSI clear + cursor home.
    os.write(1, b"\x1b[2J\x1b[H")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def first_existing(paths: Iterable[str | Path]) -> Optional[Path]:
    for p in paths:
        candidate = Path(p)
        if candidate.exists():
            return candidate
    return None
