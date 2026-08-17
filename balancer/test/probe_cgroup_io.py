#!/usr/bin/env python3
# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Side-by-side check of the two per-app I/O accounting sources.

SmarTune reports per-app disk I/O from cgroup v2 ``io.stat``. The obvious
alternative -- ``/proc/<pid>/io`` via ``psutil.io_counters()`` -- agrees on bytes but
NOT on operation counts, because psutil's ``read_count``/``write_count`` are the
``syscr``/``syscw`` syscall counters, not device requests. This script runs workloads
that make the two disagree by known amounts, so the choice can be re-verified after
any change to the sampling path (``monitor/cgroup.py``, ``ResourceMonitor._get_top_processes``).

Run it whenever the I/O numbers in the dashboard look wrong, or before/after touching
the sampler. It needs no root and no service running -- each workload runs in its own
transient user scope so it gets a private cgroup to measure.

    python3 balancer/test/probe_cgroup_io.py
    python3 balancer/test/probe_cgroup_io.py --keep   # leave the temp files behind

Expected shape of the result (absolute numbers vary by device):

  * ``bytes``: io.stat and /proc/pid/io agree to within a few percent. A large gap
    here means something is wrong with the sampler, not with the choice of source.
  * ``ops``: they agree only when one syscall produces exactly one device request
    (small O_DIRECT writes). For large buffered writes the syscall count is several
    times LOW, because the block layer splits each write into max_sectors_kb chunks.
    That gap is the whole reason the product reads io.stat.
"""

import argparse
import os
import shutil
import subprocess  # nosec - fixed argv lists, shell=False, no untrusted input
import sys
import tempfile

# Import the production readers, so this script verifies the code that actually runs
# rather than a re-implementation that could drift from it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from monitor.cgroup import as_io_rates, io_stat_deltas, snapshot_cgroup_io  # noqa: E402

# Each case: (label, dd args after of=, what it exercises)
# The pairing is the point: same total bytes, wildly different request counts.
CASES = (
    (
        "4k O_DIRECT writes",
        ["bs=4k", "count=20000", "oflag=direct"],
        "one write() == one 4k device request -> syscw should MATCH wios",
    ),
    (
        "1M buffered writes",
        ["bs=1M", "count=300", "conv=fsync"],
        "each write() splits into max_sectors_kb chunks -> syscw should be MUCH LOWER than wios",
    ),
)


def _own_cgroup() -> str:
    """This process's cgroup v2 path as it appears under /sys/fs/cgroup."""
    with open("/proc/self/cgroup") as f:
        for line in f:
            parts = line.strip().split(":")
            if len(parts) == 3 and parts[0] == "0":
                return parts[2]
    raise RuntimeError("no cgroup v2 (0::) line in /proc/self/cgroup -- v1 host?")


def _proc_io() -> dict:
    """The /proc/self/io counters psutil exposes as io_counters()."""
    out = {}
    with open("/proc/self/io") as f:
        for line in f:
            key, _, raw = line.partition(":")
            try:
                out[key.strip()] = int(raw)
            except ValueError:
                continue
    return out


def run_child(target: str, dd_args: list) -> int:
    """Inner half: measure one dd run from inside its own scope. Prints one result line."""
    cgroup = _own_cgroup()
    t0, snap0 = snapshot_cgroup_io([cgroup])
    proc0 = _proc_io()

    subprocess.run(  # nosec - fixed argv, shell=False
        ["dd", "if=/dev/zero", f"of={target}"] + dd_args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )

    proc1 = _proc_io()
    elapsed, deltas = io_stat_deltas(t0, snap0, *snapshot_cgroup_io([cgroup]))
    counts = deltas.get(cgroup, {})
    if not counts:
        # No io.stat delta at all: either `io` is not enabled in this subtree, or the
        # write never reached the device (all of it still dirty in page cache).
        print("RESULT\tNO_IO_STAT\t0\t0\t0\t0\t0\t0")
        return 1

    rates = as_io_rates(counts, elapsed)
    print("RESULT\t{}\t{}\t{}\t{}\t{}\t{:.2f}\t{:.1f}".format(
        cgroup.rsplit('/', 1)[-1],
        counts.get('wbytes', 0), proc1['write_bytes'] - proc0['write_bytes'],
        counts.get('wios', 0), proc1['syscw'] - proc0['syscw'],
        rates['write_mb_s'], rates['write_iops'],
    ))
    return 0


def run_parent(keep: bool) -> int:
    """Outer half: launch each case in its own transient scope and tabulate."""
    if not os.path.isdir("/sys/fs/cgroup/cgroup.controllers".rsplit('/', 1)[0]):
        print("no cgroup v2 mount at /sys/fs/cgroup", file=sys.stderr)
        return 2

    workdir = tempfile.mkdtemp(prefix="smartune-io-probe-")
    script = os.path.abspath(__file__)
    rows = []
    try:
        for idx, (label, dd_args, expectation) in enumerate(CASES):
            target = os.path.join(workdir, f"probe{idx}.bin")
            # A transient scope is what gives this run a private cgroup; without one the
            # measurement would include every other process in the caller's cgroup.
            cmd = [
                "systemd-run", "--user", "--scope", "--quiet",
                "--expand-environment=no", f"--unit=smartune-io-probe-{idx}.scope",
                sys.executable, script, "--child", target, "--dd", *dd_args,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)  # nosec - fixed argv
            line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT\t")), None)
            if line is None:
                print(f"  {label}: FAILED to measure\n{proc.stderr.strip()}", file=sys.stderr)
                continue
            rows.append((label, expectation, line.split("\t")[1:]))
    finally:
        if keep:
            print(f"(temp files kept in {workdir})")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if not rows:
        return 1

    print()
    print("Per-app write accounting: cgroup io.stat vs /proc/pid/io")
    print("=" * 88)
    print(f"{'workload':22}{'io.stat B':>14}{'procio B':>14}{'io.stat ops':>13}"
          f"{'syscw':>9}{'ops ratio':>11}")
    print("-" * 88)
    for label, _, f in rows:
        _, sbytes, pbytes, sops, pops, mbps, iops = f
        sops_i, pops_i = int(sops), int(pops)
        ratio = f"{sops_i / pops_i:.1f}x" if pops_i else "n/a"
        print(f"{label:22}{int(sbytes):>14}{int(pbytes):>14}{sops_i:>13}{pops_i:>9}{ratio:>11}")
    print("-" * 88)
    for label, expectation, _ in rows:
        print(f"  {label}: {expectation}")
    print()
    print("  'ops ratio' is io.stat ops / syscall count. ~1.0x means the two sources happen")
    print("  to agree for that pattern; anything well above 1.0 is how much a syscall-count")
    print("  IOPS figure would under-report. Async O_DIRECT (fio --ioengine=libaio with a")
    print("  deep iodepth) skews it further -- one io_submit() carries a whole batch. To see")
    print("  that case, run balancer/test/testing_io.sh and compare its io.stat-derived")
    print("  IOPS column against the dashboard.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="keep the temporary data files")
    # --child/--dd are the re-entrant half; not meant to be invoked by hand.
    parser.add_argument("--child", metavar="TARGET", help=argparse.SUPPRESS)
    parser.add_argument("--dd", nargs=argparse.REMAINDER, default=[], help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        return run_child(args.child, args.dd)
    return run_parent(args.keep)


if __name__ == "__main__":
    sys.exit(main())
