# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Per-cgroup disk I/O accounting from cgroup v2 ``io.stat``.

``io.stat`` is the only per-cgroup source whose numbers match what the device
actually did. The alternative -- ``/proc/<pid>/io`` via ``psutil.io_counters()`` --
reports ``syscr``/``syscw`` in the fields psutil calls ``read_count``/``write_count``.
Those are **system-call counts, not device requests**: one 1 MB buffered ``write()``
becomes ~8 requests of 128 KB at the block layer, and a single ``io_submit()`` can
carry a whole batch of async O_DIRECT IOs. Measured on cgroup v2 (reproduce with
``balancer/test/probe_cgroup_io.py``):

    dd bs=4k oflag=direct count=20000  ->  wios = 20000   syscw = 20014   (agree)
    dd bs=1M conv=fsync  count=300     ->  wios =  2400   syscw =   309   (7.8x low)

Bytes agree between the two sources; only the operation counts diverge, and they
diverge in the direction that matters -- a large-block or async writer looks an order
of magnitude lighter on IOPS than it is. Anything ranking or capping by IOPS has to
read ``io.stat``.

Two further properties this buys us:

* **Correct attribution of writeback.** Buffered writes leave the dirtying process
  long before they reach the device; the kernel charges the resulting IO back to the
  cgroup that owns the page (memcg + io on the same v2 hierarchy), which per-PID
  counters cannot do.
* **Per-device breakdown.** Every line is keyed by ``maj:min``, so a limit can target
  the one disk an app is hammering instead of every disk on the box (see
  ``IOController.set_disk_io_throttle``, which already accepts a per-disk map).

cgroup v1 is not supported and there is no fallback: the whole limit path writes
``io.max``, which is v2-only, so a v1 host could not be throttled anyway.
"""

import os
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from utils.logger import logger

CGROUP_MOUNT = "/sys/fs/cgroup"

# The io.stat fields we consume. ``dbytes``/``dios`` (discard) are deliberately left
# out: a discard is a hint to the device about blocks it may forget, not work an app
# asked for, so counting it would make a TRIM look like a write burst.
IO_STAT_FIELDS: Tuple[str, ...] = ("rbytes", "wbytes", "rios", "wios")

# One snapshot: {cgroup_path: {"maj:min": {field: cumulative_value}}}
IOSnapshot = Dict[str, Dict[str, Dict[str, int]]]


def read_cgroup_io_stat(cgroup_path: str,
                        mount_point: str = CGROUP_MOUNT) -> Optional[Dict[str, Dict[str, int]]]:
    """Return ``{"maj:min": {rbytes, wbytes, rios, wios}}`` for a single cgroup.

    :param cgroup_path: the path as it appears in ``/proc/<pid>/cgroup`` (cgroup v2
        writes ``0::<path>``), e.g. ``/user.slice/.../app-foo.scope``. Joined onto
        *mount_point*; a leading slash is tolerated.
    :param mount_point: cgroup v2 mount, overridable for tests.

    ``None`` and ``{}`` mean different things and the difference matters to
    :func:`io_stat_deltas`:

    * ``None`` -- the file could not be read, so the cgroup did not exist (or ``io`` is
      not enabled in its subtree). There is no baseline to difference against.
    * ``{}`` -- the cgroup exists but has no I/O charged to any device yet. Its
      counters are therefore zero, which IS a usable baseline: a device appearing in
      the next snapshot accumulated everything inside the window.

    Collapsing the two is what makes a freshly-launched app invisible for its first
    sampling window -- the exact app the disk-IO path most needs to see.

    A device line with no fields (the kernel prints a bare ``8:0`` for devices the
    cgroup never touched) is skipped.
    """
    path = os.path.join(mount_point, cgroup_path.lstrip('/'), "io.stat")
    result: Dict[str, Dict[str, int]] = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue  # bare "8:0" line: device present, no I/O charged
                fields: Dict[str, int] = {}
                for token in parts[1:]:
                    key, sep, raw = token.partition('=')
                    if sep and key in IO_STAT_FIELDS:
                        try:
                            fields[key] = int(raw)
                        except ValueError:
                            continue
                if fields:
                    result[parts[0]] = {k: fields.get(k, 0) for k in IO_STAT_FIELDS}
    except FileNotFoundError:
        # The cgroup died between enumeration and this read, or `io` is not enabled in
        # its subtree. Both are normal and transient.
        return None
    except OSError as e:
        logger.debug("io.stat read failed for %s: %s", cgroup_path, e)
        return None
    return result


def snapshot_cgroup_io(cgroup_paths: Iterable[str],
                       mount_point: str = CGROUP_MOUNT) -> Tuple[float, IOSnapshot]:
    """Read ``io.stat`` for every cgroup in one pass.

    :return: ``(monotonic_timestamp, {cgroup_path: {device: {field: value}}})``

    One timestamp is taken for the whole pass rather than one per cgroup. Per-cgroup
    stamps would give the cgroup read last a longer window than the one read first,
    which shows up as the busiest app looking slower than it is -- exactly backwards
    for a ranking whose job is to find the busiest app. The pass is cheap enough for
    that to be accurate: one file read per cgroup, no subprocess, no sleep.

    Cgroups whose ``io.stat`` could not be read are omitted entirely, so membership in
    the returned dict means "this cgroup existed at *ts*" -- which is what
    :func:`io_stat_deltas` needs to tell a zero baseline from no baseline.
    """
    ts = time.monotonic()
    snapshot: IOSnapshot = {}
    for cgroup in cgroup_paths:
        devices = read_cgroup_io_stat(cgroup, mount_point)
        if devices is not None:
            snapshot[cgroup] = devices
    return ts, snapshot


def io_stat_deltas(t0: float, snap0: IOSnapshot,
                   t1: float, snap1: IOSnapshot) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    """Difference two snapshots into per-cgroup I/O counts for the window.

    Returns ``(elapsed_seconds, {cgroup_path: {rbytes, wbytes, rios, wios,
    per_device: {"maj:min": {rbytes, wbytes, rios, wios}}}})``.

    Counts, not rates: the caller divides by *elapsed*. Keeping this function in
    absolute counts is what lets the caller sum several cgroups into one multi-process
    app before dividing -- summing rates and summing counts agree only when every
    cgroup shares the same window, and relying on that is a trap waiting for the first
    caller who does not.

    A cgroup absent from *snap0* is skipped: it did not exist at the start of the
    window, so charging its counters to that window would report its whole lifetime's
    I/O as an instantaneous rate. A cgroup that WAS present but had no line for a given
    device gets a zero baseline instead of being skipped -- its counters for that device
    started at zero and every byte in *snap1* was accumulated inside the window. That
    distinction is what lets a just-launched app be measured on its first window.

    Only cgroups with non-zero I/O appear in the result, so an idle box yields ``{}``.
    """
    elapsed = t1 - t0
    if elapsed <= 0:
        return 0.0, {}

    zero = {k: 0 for k in IO_STAT_FIELDS}
    out: Dict[str, Dict[str, Any]] = {}
    for cgroup, devs1 in snap1.items():
        devs0 = snap0.get(cgroup)
        if devs0 is None:
            continue  # cgroup did not exist at t0 -- no baseline at all
        per_device: Dict[str, Dict[str, int]] = {}
        totals = {k: 0 for k in IO_STAT_FIELDS}
        for dev, f1 in devs1.items():
            f0 = devs0.get(dev, zero)
            # max(0, ...) guards the one case the kernel counters can go backwards:
            # the cgroup was recreated under the same path between snapshots.
            delta = {k: max(0, f1[k] - f0[k]) for k in IO_STAT_FIELDS}
            if not any(delta.values()):
                continue
            per_device[dev] = delta
            for k in IO_STAT_FIELDS:
                totals[k] += delta[k]
        if any(totals.values()):
            out[cgroup] = {**totals, 'per_device': per_device}
    return elapsed, out


def as_io_rates(counts: Dict[str, int], elapsed: float) -> Dict[str, float]:
    """Convert one ``io_stat_deltas`` count bundle into MB/s and true device IOPS.

    ``read_iops``/``write_iops`` are ``rios``/``wios`` per second -- requests the
    device saw, which is what the module docstring is about. Do not substitute a
    syscall count here.
    """
    if elapsed <= 0:
        return {'read_mb_s': 0.0, 'write_mb_s': 0.0, 'read_iops': 0.0, 'write_iops': 0.0}
    return {
        'read_mb_s': counts.get('rbytes', 0) / elapsed / (1024 ** 2),
        'write_mb_s': counts.get('wbytes', 0) / elapsed / (1024 ** 2),
        'read_iops': counts.get('rios', 0) / elapsed,
        'write_iops': counts.get('wios', 0) / elapsed,
    }
