# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run from the repo root: python -m pytest monitor/test/test_pmt_telemetry.py"""

import time

from monitor.pmt_telemetry import (
    _GUARD_BAD_SAMPLES,
    PLATFORMS,
    PMT_GUID_ARL,
    PMT_GUID_LNL,
    PMT_GUID_MTL,
    PMT_GUID_PTL,
    PMT_GUID_WCL,
    CpuGen,
    PmtBandwidthSampler,
    TelemNode,
    compute_bandwidth,
    read_bits,
)

WCL = PLATFORMS[PMT_GUID_WCL]
LNL = PLATFORMS[PMT_GUID_LNL]
PTL = PLATFORMS[PMT_GUID_PTL]
MTL = PLATFORMS[PMT_GUID_MTL]
SIZE = 4096


def _buf(values):
    b = bytearray(SIZE)
    for off, v in values.items():
        b[off:off + 8] = v.to_bytes(8, "little")
    return bytes(b)


def _counts(gb_s, unit):
    return int(gb_s * 1e9 / unit)


def test_wcl_attribution_math():
    cur = _buf({
        0x6c0: _counts(3.0, 32),            # SANTA_RD
        0x6d0: _counts(1.0, 32),            # IDIBR_RD
        0x6e0: _counts(30.0, 32),           # MEMSW_RD
        0x6e8: _counts(4.0, 32),            # MEMSW_WR
        0xc18: _counts(10.0, 1024),         # VPU_MEMORY_BW
        0x818: _counts(40.0, 32),           # TOTAL_READ
    })
    bw = compute_bandwidth(WCL, _buf({}), cur, 1.0)
    assert abs(bw.cpu_gb_s - 4.0) < 0.01
    assert abs(bw.npu_gb_s - 10.0) < 0.01
    assert abs(bw.gpu_gb_s - (34.0 - 10.0 - WCL.io_floor_gb_s)) < 0.01
    assert abs(bw.sys_gb_s - 40.0) < 0.01


def test_dt_scales_rate():
    cur = _buf({0x6c0: _counts(4.0, 32)})
    bw = compute_bandwidth(WCL, _buf({}), cur, 2.0)
    assert abs(bw.cpu_gb_s - 2.0) < 0.01


def test_gpu_clamped_at_zero():
    cur = _buf({0x6e0: _counts(1.0, 32), 0xc18: _counts(5.0, 1024)})
    bw = compute_bandwidth(WCL, _buf({}), cur, 1.0)
    assert bw.gpu_gb_s == 0.0


def test_npu_32bit_wrap():
    prev = _buf({0xc18: 0xFFFFFFF0})
    cur = _buf({0xc18: 0x10})                 # wrapped: +0x20 counts
    bw = compute_bandwidth(WCL, prev, cur, 1.0)
    assert abs(bw.npu_gb_s - 0x20 * 1024 / 1e9) < 1e-12


def test_npu_ignores_upper_word_bits():
    prev = _buf({0xc18: (0xABCD << 32) | 100})
    cur = _buf({0xc18: (0x1234 << 32) | 200})
    bw = compute_bandwidth(WCL, prev, cur, 1.0)
    assert abs(bw.npu_gb_s - 100 * 1024 / 1e9) < 1e-12


def test_ptl_has_only_npu_and_sys():
    cur = _buf({0xc18: _counts(8.0, 1024), 0x818: _counts(20.0, 32)})
    bw = compute_bandwidth(PTL, _buf({}), cur, 1.0)
    assert bw.cpu_gb_s is None and bw.gpu_gb_s is None
    assert abs(bw.npu_gb_s - 8.0) < 0.01
    assert abs(bw.sys_gb_s - 20.0) < 0.01


def test_mtl_npu_unit_is_1000_bytes():
    cur = _buf({0x0: 1_000_000})
    bw = compute_bandwidth(MTL, _buf({}), cur, 1.0)
    assert abs(bw.npu_gb_s - 1.0) < 1e-9      # 1e6 counts * 1000 B = 1 GB


def test_lnl_uses_its_own_offsets():
    cur = _buf({0x620: _counts(2.0, 32), 0x638: _counts(1.0, 32), 0x6c0: _counts(99.0, 32)})
    bw = compute_bandwidth(LNL, _buf({}), cur, 1.0)
    assert abs(bw.cpu_gb_s - 3.0) < 0.01      # WCL's 0x6c0 must not leak in


def test_known_guids_registered():
    assert PMT_GUID_WCL in PLATFORMS and PLATFORMS[PMT_GUID_WCL].verified
    assert "0xdeadbeef" not in PLATFORMS


def test_guard_disables_implausible_layout():
    # CPU counters claim far more than the system total: wrong offsets for this platform.
    sampler = PmtBandwidthSampler()
    sampler._node = TelemNode("unused", "unused", PMT_GUID_LNL, LNL, None)
    state = {"n": 0}

    def fake_read():
        state["n"] += 1
        n = state["n"]
        return _buf({0x620: n * 1_000_000, 0x778: n * 100_000})

    sampler._read = fake_read
    results = []
    for _ in range(_GUARD_BAD_SAMPLES + 2):
        time.sleep(0.001)
        results.append(sampler.sample())
    assert results[0] is None                 # first call has no baseline
    assert sampler._disabled
    assert results[-1] is None


def test_arl_shares_mtl_register_map_but_not_cpu_gen():
    assert PLATFORMS[PMT_GUID_ARL].regs == MTL.regs
    assert PLATFORMS[PMT_GUID_ARL].cpu_gen == CpuGen.ARL
    assert MTL.cpu_gen == CpuGen.MTL


def test_read_bits_slices_and_bounds():
    buf = _buf({0x10: 0xAABBCCDD11223344})
    assert read_bits(buf, 0x10, 31, 0) == 0x11223344
    assert read_bits(buf, 0x10, 15, 8) == 0x33
    try:
        read_bits(buf, SIZE - 4, 7, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("short buffer must raise")


def test_noc_bandwidth_units():
    assert WCL.noc_bw_unit_bytes == 1024      # KiB counts -> 2**32 counts = 4194304 MiB
    assert MTL.noc_bw_unit_bytes == 1000
