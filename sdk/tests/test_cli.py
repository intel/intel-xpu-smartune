# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Offline tests for the smartune CLI: a fake requests session stands in for the
# server, so these run without a SmarTune instance or a token.

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartune_sdk import cli  # noqa: E402
from smartune_sdk.client import (EXIT_API, EXIT_AUTH, EXIT_UNAVAILABLE,  # noqa: E402
                                 EXIT_USAGE, SmartuneClient, is_read_route)
from smartune_sdk.shape import fit, iso, localize_times, parse_duration  # noqa: E402

LOCAL_TIME = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Routes (method, path) to canned payloads and records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=None):
        path = url.split("9001", 1)[1]
        self.calls.append({"method": method, "path": path, "params": params, "body": json,
                           "headers": headers})
        route = self.routes.get((method, path))
        if route is None:
            return FakeResponse(ValueError("not json"), status=404)
        if isinstance(route, FakeResponse):
            return route
        return FakeResponse({"retcode": 0, "retmsg": "success", "data": route})


def run(routes, argv, capsys):
    session = FakeSession(routes)
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    code = cli.main(argv, client=client)
    out = json.loads(capsys.readouterr().out)
    return code, out, session


DYNAMIC = {
    "collected_at": "2026-10-29 15:00:00",
    "cpu": {"usage_total": 42.123, "temperature_c": 61.0, "per_core_usage": [1.0] * 32,
            "p_core_indices": [0, 1]},
    "memory": {"usage_percent": 71.5, "total_gb": 64.0, "available_gb": 18.2,
               "bandwidth": {"total_gb_s": 3.0}},
    "pressure": {"level": "medium", "score": 0.63, "cpu": 0.12, "network_interfaces": {"eth0": {}}},
    "gpu": {"vram": {"card0": {"used_mb": 1024, "total_mb": 8192}},
            "gpu_usage": {"available": True, "raw": "...",
                          "parsed": {"devices": [{"pci_dev": "0000:00:02.0",
                                                  "engine_util": {"rcs": 10.0, "ccs": 88.5},
                                                  "power_w": {"gpu": 12.0},
                                                  "freqs": [{"name": "gt0", "act_mhz": 1900,
                                                             "max_mhz": 2350, "rc6_pct": 3.0}]}]}}},
    "npu": {"npu_smi": {"available": True, "error": None,
                        "raw": json.dumps({"utilization_percent": 37.5, "frequency_mhz": 1400,
                                           "memory_bytes": 104857600})}},
    "disk": {"level": "low", "score": 0.1, "is_stressed": False, "pressure_pct": 4.0,
             "disk_io": {"nvme0n1": {"read_mbps": 1.0, "write_mbps": 2.0, "pressure": 0.01,
                                     "disk_type": "nvme", "is_busy": False, "utilization": 3}}},
}


def test_status_summarises_and_always_sends_sections(capsys):
    code, out, session = run({("GET", "/monitor/dynamic_info"): DYNAMIC}, ["status"], capsys)
    assert code == 0
    # An unfiltered dynamic_info call would start the full collector and persist history.
    assert session.calls[0]["params"]["sections"] == "cpu,memory,pressure,gpu,npu,disk"
    assert out["cpu"] == {"usage_total": 42.12, "temperature_c": 61.0}
    assert out["pressure"]["level"] == "medium" and "network_interfaces" not in out["pressure"]
    gpu = out["gpu"]["devices"][0]
    assert gpu["utilization"] == 88.5 and gpu["freqs"] == [{"name": "gt0", "act_mhz": 1900, "max_mhz": 2350}]
    assert out["npu"] == {"available": True, "utilization_percent": 37.5, "frequency_mhz": 1400, "memory_mb": 100.0}
    assert out["disk"]["disks"]["nvme0n1"] == {"read_mbps": 1.0, "write_mbps": 2.0, "pressure": 0.01,
                                              "disk_type": "nvme", "is_busy": False}


def test_status_rejects_unknown_section(capsys):
    code, out, session = run({}, ["status", "--sections", "cpu,bogus"], capsys)
    assert code == EXIT_USAGE and "bogus" in out["error"] and not session.calls


def test_top_converts_cpu_fraction_and_reranks(capsys):
    apps = {"apps": [
        {"app_id": "a", "pid": 1, "cpu_usage": 0.05, "memory_mb": 100, "gpu_util": 90.0, "score": 80},
        {"app_id": "b", "pid": 2, "cpu_usage": 0.60, "memory_mb": 50, "gpu_util": 0.0, "score": 70,
         "cmdline": "/usr/bin/b"},
    ]}
    code, out, session = run({("GET", "/monitor/app_resource_stats"): apps}, ["top", "--by", "cpu", "-n", "1"], capsys)
    assert code == 0
    assert session.calls[0]["params"]["n"] == 50  # wider pool to re-rank from
    assert out["apps"] == [{"app_id": "b", "pid": 2, "memory_mb": 50, "gpu_util": 0.0, "score": 70, "cpu_pct": 60.0}]


def test_ps_filters_sorts_and_truncates_cmdline(capsys):
    procs = {"count": 3, "processes": [
        {"pid": 1, "name": "python", "cpu_percent": 5, "memory_percent": 1, "mem_rss_kb": 2048,
         "cmdline": "python " + "x" * 300},
        {"pid": 2, "name": "python", "cpu_percent": 50, "memory_percent": 1, "cmdline": "python serve"},
        {"pid": 3, "name": "bash", "cpu_percent": 99, "memory_percent": 1, "cmdline": "bash"},
    ]}
    code, out, _ = run({("GET", "/monitor/processes"): procs}, ["ps", "--grep", "PYTHON"], capsys)
    assert code == 0 and out["matched"] == 2 and out["total_processes"] == 3
    assert [p["pid"] for p in out["processes"]] == [2, 1]
    assert out["processes"][1]["rss_mb"] == 2.0
    assert out["processes"][1]["cmdline"].endswith("...") and len(out["processes"][1]["cmdline"]) == 163


def test_history_stats_and_downsampling(capsys):
    items = [
        {"collected_at": 1000 + i * 5,
         "data": {"cpu": {"usage_total": float(i)}, "memory": {"usage_percent": 50.0},
                  "pressure": {"score": 0.1, "level": "low" if i < 8 else "high"},
                  "gpu": {"gpu_usage": {"parsed": {"devices": [{"utilization": 20.0}, {"utilization": 70.0}]}}}}}
        for i in reversed(range(10))  # server returns newest first
    ]
    data = {"start_time": 1000, "end_time": 1050, "items": items}
    code, out, session = run({("GET", "/monitor/history"): data},
                             ["history", "--since", "1m", "--points", "4"], capsys)
    assert code == 0
    params = session.calls[0]["params"]
    assert params["range_seconds"] == 60 and params["limit"] == 22 and params["snapshot_type"] == "dynamic"
    assert out["samples"] == 10
    assert out["stats"]["cpu"] == {"min": 0, "avg": 4.5, "max": 9, "last": 9}
    assert out["stats"]["gpu"]["max"] == 70.0
    assert out["pressure_levels"] == {"low": 8, "high": 2}
    assert len(out["series"]) == 4 and out["series"][-1]["cpu"] == 9.0


def test_apps_treats_not_existing_as_empty(capsys):
    empty = FakeResponse({"retcode": 404, "retmsg": "No controlled apps found", "data": []})
    code, out, _ = run({("POST", "/app/get_controlled_app"): empty}, ["apps"], capsys)
    assert code == 0 and out == {"count": 0, "apps": []}


def test_limits_merges_auto_and_manual(capsys):
    routes = {
        ("POST", "/app/auto_limited_apps"): {"apps": [{"app_id": "x", "limited_at": 0}],
                                             "sys_pressure_level": "high", "disk_pressure_level": "low"},
        ("POST", "/app/get_controlled_app"): [
            {"app_id": "m", "control_status": "MANUAL_LIMITED", "effective": {"cpu_mem": {"limited": True}},
             "process_status_rows": [{"big": "row"}]},
            {"app_id": "n", "control_status": "NORMAL"},
        ],
    }
    code, out, _ = run(routes, ["limits"], capsys)
    assert code == 0 and out["sys_pressure_level"] == "high"
    assert [a["app_id"] for a in out["manual_limited"]] == ["m"]
    assert "process_status_rows" not in out["manual_limited"][0]


def test_diag_events_passes_window_and_filters(capsys):
    code, out, session = run({("GET", "/diag/events"): {"events": [{"ts": 1_794_000_000, "event_type": "E"}], "count": 1}},
                             ["diag", "events", "--since", "2h", "--app", "a", "--severity", "warning"], capsys)
    params = session.calls[0]["params"]
    assert code == 0 and params["to"] - params["from"] == 7200
    assert params["app_id"] == "a" and params["min_severity"] == "warning" and "category" not in params
    assert params["keep_important"] == "true" and "severity" not in params
    assert out["count"] == 1 and isinstance(out["events"][0]["ts"], str)
    assert out["truncated_by_server"] is False


def test_diag_events_reports_server_truncation(capsys):
    code, out, _ = run({("GET", "/diag/events"): {"events": [], "count": 0, "truncated": True}},
                       ["diag", "events"], capsys)
    assert code == 0 and out["truncated_by_server"] is True


def test_diag_events_rejects_unknown_severity(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["diag", "events", "--severity", "warn"])
    assert exc.value.code == 2


def test_diag_why_survives_partial_failure(capsys):
    routes = {
        ("GET", "/diag/control-lifecycles"): {"lifecycles": []},
        ("GET", "/diag/events"): {"events": []},
        ("GET", "/diag/context"): {"window": {}, "findings": ["f"], "control_actions": [], "monitor": {"big": 1}},
        # /app/limit_snapshot missing: a monitor-only deployment
    }
    code, out, _ = run(routes, ["diag", "why", "com.example"], capsys)
    assert code == 0
    assert "error" in out["current_limit"]
    assert out["context"] == {"window": {}, "findings": ["f"], "control_actions": []}


def test_diag_context_bounds_app_scope_by_since(capsys):
    routes = {("GET", "/diag/context"): {"window": {}, "findings": []}}
    code, _, session = run(routes, ["diag", "context", "--app", "a", "--since", "6h"], capsys)
    params = session.calls[0]["params"]
    assert code == 0 and params["app_id"] == "a" and params["to"] - params["from"] == 6 * 3600


def test_diag_context_job_scope_uses_job_span(capsys):
    routes = {("GET", "/diag/context"): {"window": {}, "findings": []}}
    code, _, session = run(routes, ["diag", "context", "--job", "j", "--since", "6h"], capsys)
    params = session.calls[0]["params"]
    assert code == 0 and params["job_id"] == "j" and "from" not in params and "to" not in params


def test_diag_why_context_uses_since_window(capsys):
    routes = {
        ("GET", "/diag/control-lifecycles"): {"lifecycles": []},
        ("GET", "/diag/events"): {"events": []},
        ("GET", "/diag/context"): {"window": {}, "findings": [], "control_actions": []},
    }
    code, _, session = run(routes, ["diag", "why", "com.example", "--since", "2h"], capsys)
    context_call = next(c for c in session.calls if c["path"] == "/diag/context")
    assert code == 0 and context_call["params"]["to"] - context_call["params"]["from"] == 7200


def test_digest_defaults_to_today(capsys):
    code, _, session = run({("GET", "/diag/digest"): {}}, ["diag", "digest"], capsys)
    assert code == 0 and session.calls[0]["params"]["period"] == "day"
    assert len(session.calls[0]["params"]["date"]) == 10


def test_api_error_and_missing_feature(capsys):
    bad = FakeResponse({"retcode": 101, "retmsg": "Unknown section"})
    code, out, _ = run({("GET", "/monitor/static_info/gpu"): bad}, ["info", "gpu"], capsys)
    assert code == EXIT_API and out == {"error": "Unknown section", "retcode": 101}
    code, out, _ = run({}, ["bench", "status"], capsys)
    assert code == EXIT_UNAVAILABLE and "smartune caps" in out["hint"]


def test_auth_failure(capsys):
    denied = FakeResponse({"retcode": 401, "data": {"authenticated": False}}, status=401)
    code, out, _ = run({("GET", "/smartune/capabilities"): denied}, ["caps"], capsys)
    assert code == EXIT_AUTH and out["retcode"] == 401


def test_headers_carry_token_and_agent_id(capsys):
    _, _, session = run({("GET", "/smartune/capabilities"): {"capabilities": 1}}, ["caps"], capsys)
    assert session.calls[0]["headers"] == {"X-Auth-Token": "t", "X-Agent-Id": "smartune-cli"}


@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/monitor/processes", True),
    ("POST", "/app/limit_snapshot", True),
    ("GET", "/bench/run/bench-20261013-0912", True),
    ("GET", "/bench/run/../env", False),
    ("GET", "/bench/env", False),             # GET, but bootstraps the venv
    ("POST", "/app/kill_process", False),
    ("POST", "/app/resource_limit", False),
    ("POST", "/app/check_running_apps", False),
    ("POST", "/monitor/config/passive_control", False),
    ("GET", "/monitor/processes/extra", False),
])
def test_read_route_allowlist(method, path, allowed):
    assert is_read_route(method, path) is allowed


def test_client_refuses_write_routes():
    session = FakeSession({})
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    with pytest.raises(Exception) as exc:
        client.post("/app/kill_process", {"pid": 1})
    assert exc.value.exit_code == EXIT_USAGE and not session.calls


def test_fit_marks_truncation():
    obj = {"rows": [{"i": i, "pad": "x" * 50} for i in range(1000)], "meta": 1}
    out = fit(obj, 4000)
    assert len(json.dumps(out)) <= 4000
    note = out["_truncated"]["lists"]["$.rows"]
    assert note["total"] == 1000 and note["kept"] == len(out["rows"]) < 1000


def test_fit_leaves_small_output_alone():
    obj = {"a": [1, 2, 3]}
    assert fit(obj, 4000) == {"a": [1, 2, 3]}


@pytest.mark.parametrize("text,seconds", [("90", 90), ("30m", 1800), ("1h", 3600), ("2d", 172800), ("1.5h", 5400)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "0", "-1h", "1y"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_iso_carries_utc_offset():
    # A remote agent must be able to place the time without knowing the server's zone.
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}", iso(1_700_000_000))
    assert iso(None) is None and iso("2026-10-29 15:00:00") == "2026-10-29 15:00:00"


def test_localize_times_unifies_every_server_format():
    epoch = 1_794_453_595
    out = localize_times({
        "collected_at": "2026-11-12 11:19:30",                   # naive local string
        "generated_at": "2026-11-12T03:19:55.320716+00:00",      # UTC ISO
        "ts_utc": "2026-11-12T03:19:55Z",
        "ts_iso": "2026-11-12T11:19:17.637+08:00",
        "period": {"kind": "day", "from": epoch, "to": epoch},
        "boots": [{"first_ts": epoch, "last_ts": epoch, "index": 0}],
        "limited_at": 0,                                          # placeholder, not a time
        "acknowledged_at": None,
        "config_changes": [{"key": "mem_limit", "from": 4294967296, "to": 8589934592}],
        "window_seconds": 3600,
        "nested": {"window": {"from": "2026-11-12 10:19:36 +0800"}},
    })
    for key in ("collected_at", "generated_at", "ts_utc", "ts_iso"):
        assert LOCAL_TIME.fullmatch(out[key]), key
    assert out["ts_utc"] == iso(1_794_453_595)
    assert LOCAL_TIME.fullmatch(out["period"]["from"]) and LOCAL_TIME.fullmatch(out["boots"][0]["last_ts"])
    assert out["limited_at"] == 0 and out["acknowledged_at"] is None and out["window_seconds"] == 3600
    # from / to outside a window or period are old and new values, not times.
    assert out["config_changes"] == [{"key": "mem_limit", "from": 4294967296, "to": 8589934592}]
    assert out["nested"]["window"]["from"] == "2026-11-12 10:19:36 +0800"


def test_digest_and_logs_times_are_localized(capsys):
    digest = {"period": {"kind": "day", "from": 1_794_441_600, "to": 1_794_528_000},
              "generated_at": "2026-11-12T03:19:55+00:00"}
    _, out, _ = run({("GET", "/diag/digest"): digest}, ["diag", "digest"], capsys)
    assert LOCAL_TIME.fullmatch(out["period"]["from"]) and LOCAL_TIME.fullmatch(out["generated_at"])
    logs = {"records": [{"ts_epoch": 1_794_453_557.6, "ts_iso": "2026-11-12T03:19:17+00:00", "message": "m"}]}
    _, out, _ = run({("GET", "/diag/logs"): logs}, ["diag", "logs"], capsys)
    assert out["lines"][0]["ts_iso"] == out["lines"][0]["ts_epoch"] == iso(1_794_453_557)


def test_logs_reads_server_records(capsys):
    logs = {"records": [{"ts_epoch": 1_794_453_557.0, "message": "boom"}], "truncated": False}
    _, out, session = run({("GET", "/diag/logs"): logs},
                          ["diag", "logs", "--source", "journal", "--all-journal"], capsys)
    assert [line["message"] for line in out["lines"]] == ["boom"]
    assert session.calls[0]["params"]["kernel_only"] == "false"


def test_raw_output_keeps_api_times(capsys):
    _, out, _ = run({("GET", "/monitor/dynamic_info"): DYNAMIC}, ["status", "--raw"], capsys)
    assert out["collected_at"] == "2026-10-29 15:00:00"


def test_quiet_mode_is_reported_not_hidden(capsys):
    quiet = {"active": True, "cached_at": 1_794_453_500.0}
    data = dict(DYNAMIC, quiet_mode=quiet)
    _, out, _ = run({("GET", "/monitor/dynamic_info"): data}, ["status"], capsys)
    assert out["quiet_mode"] is True and "cached" in out["note"]
    assert LOCAL_TIME.fullmatch(out["cached_at"])
    _, out, _ = run({("GET", "/monitor/app_resource_stats"): {"apps": [], "quiet_mode": quiet}}, ["top"], capsys)
    assert out["apps"] == [] and out["quiet_mode"] is True and "idle" in out["note"]
    _, out, _ = run({("GET", "/monitor/app_disk_io_stats"): {"apps": [], "quiet_mode": quiet}}, ["top", "--io"], capsys)
    assert out["quiet_mode"] is True
    # No marker outside quiet mode.
    _, out, _ = run({("GET", "/monitor/app_resource_stats"): {"apps": []}}, ["top"], capsys)
    assert "quiet_mode" not in out and "note" not in out


def test_top_by_npu_memory(capsys):
    apps = {"apps": [{"app_id": "a", "npu_mem_mb": 0.0, "score": 90},
                     {"app_id": "ov", "npu_mem_mb": 512.0, "score": 10}]}
    _, out, _ = run({("GET", "/monitor/app_resource_stats"): apps}, ["top", "--by", "npu", "-n", "1"], capsys)
    assert out["apps"] == [{"app_id": "ov", "npu_mem_mb": 512.0, "score": 10}]


def test_apps_shows_cpu_affinity_only_when_pinned(capsys):
    apps = [{"app_id": "pinned", "control_status": "NORMAL", "cpu_affinity": [{"class": "p"}]},
            {"app_id": "free", "control_status": "NORMAL", "cpu_affinity": []}]
    _, out, _ = run({("POST", "/app/get_controlled_app"): apps}, ["apps"], capsys)
    assert out["apps"][0]["cpu_affinity"] == [{"class": "p"}] and "cpu_affinity" not in out["apps"][1]


def test_logs_source_is_validated(capsys):
    _, _, session = run({("GET", "/diag/logs"): {"records": []}},
                        ["diag", "logs", "--source", "smartune, journal"], capsys)
    assert session.calls[0]["params"]["source"] == "smartune,journal"
    assert "kernel_only" not in session.calls[0]["params"]
    with pytest.raises(SystemExit) as exc:  # the server would silently drop "journald"
        cli.main(["diag", "logs", "--source", "smartune,journald"], client=object())
    assert exc.value.code == EXIT_USAGE
    assert "journald" in capsys.readouterr().err
