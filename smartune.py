#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Single entry point for the whole SmarTune product. It selects what to run:
#
#   python3 smartune.py        balancer + monitor (combined, port 9001)  [default]
#   python3 smartune.py -m     monitor only (standalone telemetry, port 9001)
#

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def run_all():
    """Start the combined balancer + monitor service."""
    balancer_dir = os.path.join(ROOT, "balancer")
    # balancer/ ahead of ROOT so `balancer` resolves to the inner package;
    # chdir so controller/bpf_event.c (a CWD-relative path) resolves.
    sys.path.insert(0, balancer_dir)
    os.chdir(balancer_dir)
    import balance_service
    balance_service.main()


def run_monitor():
    """Start the standalone monitor service."""
    from monitor import monitor_service
    monitor_service.main()


def main():
    parser = argparse.ArgumentParser(description="Start the SmarTune services.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-a", "--all", action="store_true",
                       help="start balancer + monitor together (default)")
    group.add_argument("-m", "--monitor", action="store_true",
                       help="start the monitor only (standalone)")
    args = parser.parse_args()

    run_monitor() if args.monitor else run_all()


if __name__ == "__main__":
    main()
