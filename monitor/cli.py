# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse

from .collectors import collect_all
from .ui import display_loop, render_console


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified XPU Infer monitor")
    parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Print once and exit")
    args = parser.parse_args(argv)

    if args.once:
        print(render_console(collect_all(), interval_s=float(args.interval)))
        return 0

    return display_loop(collect_all, interval_s=float(args.interval))
