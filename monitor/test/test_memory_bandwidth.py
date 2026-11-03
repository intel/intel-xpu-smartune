# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Run from the repo root: python -m pytest monitor/test/test_memory_bandwidth.py

`MemoryBandwidthSampler`: PMT DRAM counters first, IMC PMU as the fallback, and
`get_memory_capacity()` staying clear of the bandwidth samplers. Both sources are
stubbed; no hardware access.
"""

import pytest

from monitor.metrics import memory
from monitor.metrics.memory import BandwidthSample, MemoryBandwidthSampler
from monitor.pmt_telemetry import DeviceBandwidth


class _FakePmt:
    def __init__(self, reading):
        self.reading = reading
        self.calls = 0
        self.last = None

    def sample(self):
        self.calls += 1
        return self.reading


class _FakeImc:
    def __init__(self, reading, max_gb_s=45.0):
        self.reading = reading
        self.max_bandwidth_gb_s = max_gb_s
        self.calls = 0

    def sample(self):
        self.calls += 1
        return self.reading

    def close(self):
        pass


def _sampler(pmt, imc, max_gb_s=45.0):
    s = MemoryBandwidthSampler()
    s._pmt, s._imc, s.max_bandwidth_gb_s = pmt, imc, max_gb_s
    return s


def _imc(read, write):
    return _FakeImc(BandwidthSample(total_gb_s=read + write, read_gb_s=read, write_gb_s=write))


def _pmt(read, write, **kw):
    return _FakePmt(DeviceBandwidth(cpu_gb_s=kw.get("cpu"), gpu_gb_s=kw.get("gpu"),
                                    npu_gb_s=0.0, sys_read_gb_s=read, sys_write_gb_s=write,
                                    cache_hit_gb_s=kw.get("hit"), cache_miss_gb_s=kw.get("miss")))


def test_pmt_preferred_over_imc_and_imc_still_sampled():
    imc = _imc(21.0, 14.0)
    s = _sampler(_pmt(21.414, 14.056, cpu=37.0, hit=10.0, miss=30.0), imc)
    bw = s.sample()
    assert bw["source"] == "pmt"
    assert (bw["read_gb_s"], bw["write_gb_s"], bw["total_gb_s"]) == (21.41, 14.06, 35.47)
    assert bw["usage_percent"] == round(100 * 35.47 / 45.0, 2)
    assert bw["devices"]["cpu_gb_s"] == 37.0
    assert bw["soc_cache_hit_percent"] == 25.0
    assert imc.calls == 1               # baseline kept current for a fallback


def test_falls_back_to_imc_without_pmt_reading():
    bw = _sampler(_FakePmt(None), _imc(5.5, 9.8)).sample()
    assert bw["source"] == "imc"
    assert (bw["read_gb_s"], bw["write_gb_s"], bw["total_gb_s"]) == (5.5, 9.8, 15.3)
    assert bw["devices"] == {"cpu_gb_s": None, "gpu_gb_s": None}


def test_falls_back_to_imc_when_platform_has_no_pmt_dram():
    bw = _sampler(_pmt(None, None, cpu=3.0), _imc(1.0, 0.5)).sample()
    assert bw["source"] == "imc" and bw["total_gb_s"] == 1.5
    assert bw["devices"]["cpu_gb_s"] == 3.0


def test_pmt_only():
    bw = _sampler(_pmt(3.0, 1.5), None).sample()
    assert bw["source"] == "pmt"
    assert bw["total_gb_s"] == 4.5 and bw["usage_percent"] == 10.0


def test_blank_when_no_reading():
    bw = _sampler(_FakePmt(None), _FakeImc(None)).sample()
    assert bw["source"] is None
    assert bw["total_gb_s"] is None and bw["read_gb_s"] is None
    assert bw["usage_percent"] is None
    assert set(bw) >= {"devices", "soc_cache_hit_percent", "soc_cache_miss_gb_s"}


def test_instances_keep_separate_baselines():
    # The benchmark's instance must not advance the dashboard's sources.
    dash_pmt, bench_pmt = _pmt(1.0, 1.0), _pmt(1.0, 1.0)
    dash, bench = _sampler(dash_pmt, None), _sampler(bench_pmt, None)
    for _ in range(4):
        bench.sample()
    dash.sample()
    assert (dash_pmt.calls, bench_pmt.calls) == (1, 4)


def test_capacity_does_not_touch_bandwidth(monkeypatch):
    def boom():
        raise AssertionError("get_memory_capacity() must not sample bandwidth")
    monkeypatch.setattr(memory, "get_memory_bandwidth", boom)
    cap = memory.get_memory_capacity()
    assert "bandwidth" not in cap
    assert cap["total_gb"] > 0


def test_dynamic_is_capacity_plus_bandwidth(monkeypatch):
    monkeypatch.setattr(memory, "get_memory_bandwidth", lambda: {"source": "pmt"})
    dyn = memory.get_memory_dynamic()
    assert dyn["bandwidth"] == {"source": "pmt"}
    assert set(dyn) - {"bandwidth"} == set(memory.get_memory_capacity())


def test_shared_blank_without_sources(monkeypatch):
    monkeypatch.setattr(memory, "_shared_sampler", None)
    monkeypatch.setattr(memory, "_shared_sampler_started", True)
    monkeypatch.setattr(memory, "_dmi_ceiling", lambda: 45.0)
    bw = memory.get_memory_bandwidth()
    assert bw["source"] is None and bw["total_gb_s"] is None
