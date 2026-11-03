# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run from the repo root: python -m pytest monitor/test/test_npu_fdinfo.py"""

from monitor.npu_monitor import parse_npu_fdinfo

# Real intel_vpu fdinfo (kernel 7.0).
VPU_FDINFO = """pos:\t0
flags:\t02500002
mnt_id:\t30
ino:\t588
drm-driver:\tintel_vpu
drm-client-id:\t153
drm-pdev:\t0000:00:0b.0
drm-total-memory:\t4785740 KiB
drm-shared-memory:\t0
drm-active-memory:\t56 KiB
drm-resident-memory:\t4455892 KiB
"""


def test_parses_real_intel_vpu_fdinfo():
    assert parse_npu_fdinfo(VPU_FDINFO) == (153, 4455892)   # resident, not total


def test_non_vpu_driver_is_ignored():
    assert parse_npu_fdinfo("drm-driver:\txe\ndrm-client-id:\t7\ndrm-resident-memory:\t10 KiB\n") is None
    assert parse_npu_fdinfo("pos:\t0\nflags:\t02\n") is None


def test_units_normalised_to_kib():
    def kib(value):
        return parse_npu_fdinfo(f"drm-driver:\tintel_vpu\ndrm-resident-memory:\t{value}\n")[1]
    assert kib("12345 KiB") == 12345
    assert kib("64 MiB") == 64 * 1024
    assert kib("2 GiB") == 2 * 1024 * 1024
    assert kib("4096") == 4        # bare number is bytes


def test_missing_or_bad_fields_degrade_to_zero():
    assert parse_npu_fdinfo("drm-driver:\tintel_vpu\n") == (None, 0)
    assert parse_npu_fdinfo("drm-driver:\tintel_vpu\ndrm-resident-memory:\tnope KiB\n") == (None, 0)
    assert parse_npu_fdinfo("drm-driver:\tintel_vpu\ndrm-resident-memory:\t5 TiB\n") == (None, 0)
