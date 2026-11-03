# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run from the repo root: python -m pytest monitor/test/test_pmt_telemetry.py"""

import monitor.pmt_telemetry as pmt
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


def test_ptl_uses_wcl_layout_without_floor():
    # NPU inference as measured on PTL: MemSWITCH ~= NPU, so GPU ~= 0.
    cur = _buf({
        0x6c0: _counts(0.3, 32), 0x6d0: _counts(1.0, 32),
        0x6e0: _counts(7.7, 32), 0xc18: _counts(7.6, 1024),
        0x818: _counts(5.1, 32),
    })
    bw = compute_bandwidth(PTL, _buf({}), cur, 1.0)
    assert abs(bw.cpu_gb_s - 1.3) < 0.01
    assert abs(bw.io_gb_s - 7.7) < 0.01
    assert abs(bw.gpu_gb_s - 0.1) < 0.01        # io_floor 0 on PTL
    assert abs(bw.sys_gb_s - 5.1) < 0.01


def test_sys_split_into_read_and_write():
    # sys_offsets is (TOTAL_READ, TOTAL_WRITE) on every platform.
    cur = _buf({0x818: _counts(21.4, 32), 0x810: _counts(14.1, 32)})
    bw = compute_bandwidth(PTL, _buf({}), cur, 1.0)
    assert abs(bw.sys_read_gb_s - 21.4) < 0.01
    assert abs(bw.sys_write_gb_s - 14.1) < 0.01
    assert abs(bw.sys_gb_s - 35.5) < 0.01
    lnl = compute_bandwidth(LNL, _buf({}), _buf({0x778: _counts(3.0, 32), 0x770: _counts(1.0, 32)}), 1.0)
    assert abs(lnl.sys_read_gb_s - 3.0) < 0.01 and abs(lnl.sys_write_gb_s - 1.0) < 0.01


def test_every_platform_sys_offsets_are_a_read_write_pair():
    for platform in PLATFORMS.values():
        assert len(platform.sys_offsets) in (0, 2), platform.name


def test_cache_counters_are_64_bytes():
    cur = _buf({0x6a0: _counts(4.0, 64), 0x6a8: _counts(12.0, 64)})
    bw = compute_bandwidth(PTL, _buf({}), cur, 1.0)
    assert abs(bw.cache_hit_gb_s - 4.0) < 0.01
    assert abs(bw.cache_miss_gb_s - 12.0) < 0.01
    assert abs(bw.cache_hit_percent - 25.0) < 0.1


def test_no_cache_counters_means_no_hit_ratio():
    bw = compute_bandwidth(LNL, _buf({}), _buf({0x620: 100}), 1.0)
    assert bw.cache_total_gb_s is None and bw.cache_hit_percent is None


def _residency(ratio, dt=1.0):
    return int(ratio * dt * 38.4e6)


def test_gpu_idle_needs_both_gt_and_media_residency():
    idle = _buf({0x4d0: _residency(0.99), 0x4d8: _residency(1.0)})
    media = _buf({0x4d0: _residency(0.98), 0x4d8: _residency(0.0)})   # media load
    compute = _buf({0x4d0: _residency(0.0), 0x4d8: _residency(1.0)})  # GPU compute
    assert compute_bandwidth(PTL, _buf({}), idle, 1.0).gpu_idle is True
    assert compute_bandwidth(PTL, _buf({}), media, 1.0).gpu_idle is False
    assert compute_bandwidth(PTL, _buf({}), compute, 1.0).gpu_idle is False


def test_gpu_idle_unknown_on_insane_tick_rate():
    wrong_clock = _buf({0x4d0: _residency(2.0), 0x4d8: _residency(2.0)})
    assert compute_bandwidth(PTL, _buf({}), wrong_clock, 1.0).gpu_idle is None
    assert compute_bandwidth(LNL, _buf({}), wrong_clock, 1.0).gpu_idle is None


def test_explicit_floor_overrides_platform():
    cur = _buf({0x6e0: _counts(5.0, 32)})
    bw = compute_bandwidth(WCL, _buf({}), cur, 1.0, io_floor_gb_s=0.5)
    assert abs(bw.gpu_gb_s - 4.5) < 0.01


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


def _sampler(platform, guid, step):
    """Sampler fed with snapshots that grow by `step` (offset -> counts) per read."""
    sampler = PmtBandwidthSampler()
    sampler._node = TelemNode("unused", "unused", guid, platform, None)
    sampler.io_floor_gb_s = platform.io_floor_gb_s
    state = {"n": 0}

    def fake_read():
        state["n"] += 1
        return _buf({off: state["n"] * c for off, c in step.items()})

    sampler._read = fake_read
    return sampler


def _run(sampler, n):
    """Sample n times with a fake clock advancing exactly 1 s per read, so step
    counts are per-second rates and residency ratios are exact."""
    clock = {"t": 1000.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    real = pmt.time.monotonic
    pmt.time.monotonic = fake_monotonic
    try:
        return [sampler.sample() for _ in range(n)]
    finally:
        pmt.time.monotonic = real


# Per-second count steps measured on PTL, benchmark_app -d CPU (GB/s):
# egress 7.4, MUFASA hit 4.8 + miss 2.7 = 7.5, DRAM 4.8.
_CPU_INFERENCE = {
    0x6c0: _counts(7.4, 32), 0x6e0: _counts(0.06, 32),
    0x6a0: _counts(4.8, 64), 0x6a8: _counts(2.7, 64), 0x818: _counts(4.8, 32),
}


def test_guard_accepts_cache_hits_that_old_sys_guard_rejected():
    # egress / DRAM = 1.56 (the old 1.3x-of-sys guard tripped here), but
    # egress / MUFASA hit+miss = 0.99.
    sampler = _sampler(PTL, PMT_GUID_PTL, _CPU_INFERENCE)
    results = _run(sampler, _GUARD_BAD_SAMPLES + 2)
    assert results[0] is None                 # first call has no baseline
    assert not sampler._disabled
    assert all(r is not None for r in results[1:])


def test_guard_disables_layout_diverging_from_cache():
    # Egress counters claim 2x the MUFASA total: wrong offsets/units.
    step = {**_CPU_INFERENCE, 0x6c0: _counts(15.0, 32)}
    sampler = _sampler(PTL, PMT_GUID_PTL, step)
    results = _run(sampler, _GUARD_BAD_SAMPLES + 2)
    assert sampler._disabled
    assert results[-1] is None


def test_guard_skipped_without_cache_counters():
    # LNL has no MUFASA offsets configured: no guard even for absurd ratios.
    sampler = _sampler(LNL, PMT_GUID_LNL, {0x620: _counts(40.0, 32), 0x778: _counts(4.0, 32)})
    _run(sampler, _GUARD_BAD_SAMPLES + 2)
    assert not sampler._disabled


def test_io_floor_learned_only_while_gpu_idle():
    # WCL seeds the floor at 1.91; this machine idles at 0.5 GB/s of IO.
    idle = {0x6e0: _counts(0.5, 32), 0x4d0: _residency(0.99), 0x4d8: _residency(1.0)}
    sampler = _sampler(WCL, PMT_GUID_WCL, idle)
    results = _run(sampler, 40)
    assert abs(sampler.io_floor_gb_s - 0.5) < 0.01
    assert all(r.gpu_gb_s == 0.0 for r in results[1:])

    # GPU busy (GT RC6 = 0): floor untouched, gpu = io - seed floor.
    busy = {0x6e0: _counts(5.0, 32), 0x4d0: 0, 0x4d8: _residency(1.0)}
    sampler = _sampler(WCL, PMT_GUID_WCL, busy)
    results = _run(sampler, 10)
    assert sampler.io_floor_gb_s == WCL.io_floor_gb_s
    assert abs(results[-1].gpu_gb_s - (5.0 - WCL.io_floor_gb_s)) < 0.01


def test_arl_shares_mtl_register_map_but_not_cpu_gen():
    assert PLATFORMS[PMT_GUID_ARL].regs == MTL.regs
    assert PLATFORMS[PMT_GUID_ARL].cpu_gen == CpuGen.ARL
    assert MTL.cpu_gen == CpuGen.MTL


def test_mtl_cpu_and_gpu_use_64b_noc_counters():
    cur = _buf({
        0x728: _counts(2.0, 64), 0x730: _counts(1.0, 64),    # NOC_BW_IDI2CMI0/1
        0x698: _counts(3.0, 64), 0x6a0: _counts(1.0, 64),    # NOC_BW_GCD0/1
        0x6e8: _counts(0.5, 64), 0x6f0: _counts(0.5, 64),    # NOC_BW_MEDIA0/1
        0x7e0: _counts(6.0, 32), 0x7d8: _counts(2.0, 32),    # TOTAL_READ/WRITE (32B)
        0x0: _counts(1.0, 1000),                             # VPU_MEMORY_BW
    })
    bw = compute_bandwidth(MTL, _buf({}), cur, 1.0)
    assert abs(bw.cpu_gb_s - 3.0) < 0.01
    assert abs(bw.gpu_gb_s - 5.0) < 0.01          # summed directly, NPU not subtracted
    assert abs(bw.npu_gb_s - 1.0) < 0.01
    assert abs(bw.sys_read_gb_s - 6.0) < 0.01     # sys keeps its own 32B unit
    assert abs(bw.sys_write_gb_s - 2.0) < 0.01
    assert bw.io_gb_s is None                     # nothing derived from MemSWITCH
    assert bw.cache_hit_percent is None           # no MUFASA on MTL


def test_mtl_gpu_is_zero_not_floor_adjusted_when_idle():
    bw = compute_bandwidth(MTL, _buf({}), _buf({}), 1.0)
    assert bw.gpu_gb_s == 0.0 and bw.cpu_gb_s == 0.0


def test_arl_has_the_same_cpu_and_gpu_counters_as_mtl():
    for guid in (PMT_GUID_ARL, "0x1306a0b2", "0x1306a0b4"):
        arl = PLATFORMS[guid]
        assert (arl.cpu_offsets, arl.gpu_offsets, arl.egress_unit_bytes) == (
            MTL.cpu_offsets, MTL.gpu_offsets, MTL.egress_unit_bytes)


def test_mtl_span_covers_its_new_counters():
    assert pmt._bandwidth_span(MTL) == 0x7e0 + 8
    assert max(MTL.cpu_offsets + MTL.gpu_offsets) + 8 <= pmt._bandwidth_span(MTL)


def test_mtl_never_trips_the_guard_or_learns_a_floor():
    cur = _buf({0x728: _counts(5.0, 64), 0x698: _counts(5.0, 64)})
    bw = compute_bandwidth(MTL, _buf({}), cur, 1.0)
    assert pmt._implausible(bw) is False
    sampler = PmtBandwidthSampler()
    assert sampler._learn_io_floor(bw) is bw


def test_other_platforms_do_not_use_the_mtl_only_fields():
    # The egress unit / direct-GPU path is MTL/ARL-only: PTL, WCL and LNL must keep
    # deriving gpu from MemSWITCH at the platform's 32B unit.
    for plat in (PTL, WCL, LNL):
        assert plat.gpu_offsets == () and plat.egress_unit_bytes is None
        assert plat.io_offsets and plat.cpu_offsets


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
