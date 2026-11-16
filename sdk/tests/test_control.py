# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune control`: plans change nothing, --yes applies exactly the plan, and
# the state guards refuse before any write reaches the server.

import os
import shlex
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartune_sdk.client import (EXIT_REFUSED, EXIT_USAGE, SmartuneClient,  # noqa: E402
                                 is_control_route)
from test_cli import FakeResponse, FakeSession, run  # noqa: E402

APP = {"app_id": "a1", "app_name": "ollama", "priority": "low", "network_priority": "low",
       "status": "Running", "control_status": "NORMAL", "frozen": False}
PROFILE = {
    "cpu": {"enabled": True, "value": 40.0, "min": 1.0, "max": 50.0},
    "memory": {"enabled": True, "value": 10.0, "min": 1.0, "max": 30.0},
    "disk_io": {"enabled": False, "is_io_limit": False,
                "write": {"value": 200, "min": 1, "max": 200}, "read": {"value": 300, "min": 1, "max": 300},
                "write_iops": {"value": 60000, "min": 1, "max": 60000},
                "read_iops": {"value": 90000, "min": 1, "max": 90000}},
    "cgroup_ids": ["app-ollama.scope"],
    "target_processes": [{"pid": 4242, "name": "ollama"}],
}
MANUAL_EFFECTIVE = {"cpu_mem": {"limited": True, "cpu_rate": 0.3, "mem_rate": 0.1},
                    "disk_io": {"limited": False}}


def routes(app=None, extra=None):
    base = {
        ("POST", "/app/get_controlled_app"): [app or APP],
        ("POST", "/app/resource_limit_profile"): PROFILE,
        ("POST", "/app/limit_snapshot"): {"limited": True, "control_status": "MANUAL_LIMITED",
                                          "effective": MANUAL_EFFECTIVE},
        ("POST", "/app/auto_limited_apps"): {"apps": [], "sys_pressure_level": "low"},
    }
    base.update(extra or {})
    return base


def ok(message="success", data=None):
    return FakeResponse({"retcode": 0, "retmsg": message, "data": data if data is not None else {}})


def writes(session):
    return [c for c in session.calls if is_control_route(c["method"], c["path"])]


def apply_argv(plan):
    return shlex.split(plan["apply_with"])[1:]


def test_limit_plan_changes_nothing_and_pins_every_value(capsys):
    code, plan, session = run(routes(), ["control", "limit", "a1", "--cpu", "30"], capsys)
    assert code == 0 and plan["dry_run"] and plan["can_apply"]
    assert not writes(session)
    # The profile supplies the values not given; apply_with writes them all out.
    assert plan["target"]["cpu_pct_of_usage"] == 30 and plan["target"]["memory_pct_of_usage"] == 10
    assert plan["target"]["disk_io"] == "off"
    assert apply_argv(plan) == ["control", "limit", "a1", "--cpu", "30", "--mem", "10", "--no-disk",
                                "--expect", "NORMAL", "--yes"]
    assert plan["undo_with"] == "smartune control restore a1"
    assert "Nothing was changed" in plan["note"]


def test_limit_apply_sends_the_planned_request(capsys):
    r = routes(None, {("POST", "/app/resource_limit"): ok("Successfully found and set resource limit")})
    _, plan, _ = run(r, ["control", "limit", "a1", "--cpu", "30", "--disk-write", "50"], capsys)
    code, out, session = run(r, apply_argv(plan), capsys)
    assert code == 0 and out["applied"] is True
    (call,) = writes(session)
    assert call["path"] == "/app/resource_limit"
    body = call["body"]
    assert body["target_cgroups"] == ["app-ollama.scope"] and body["priority"] == "low"
    assert body["limit_overrides"]["cpu"] == {"enabled": True, "rate": 0.3}
    assert body["limit_overrides"]["memory"] == {"enabled": True, "rate": 0.1}
    assert body["limit_overrides"]["disk_io"] == {
        "enabled": True, "rate": {"read": 300, "write": 50, "read_iops": 90000, "write_iops": 60000}}
    assert out["before"]["control_status"] == "NORMAL"
    assert out["after"] == {"control_status": "MANUAL_LIMITED", "cpu_pct_of_usage": 30.0,
                            "memory_pct_of_usage": 10.0}


def test_limit_value_outside_the_priority_band_is_rejected(capsys):
    code, out, session = run(routes(), ["control", "limit", "a1", "--cpu", "80", "--yes"], capsys)
    assert code == EXIT_USAGE and "outside the allowed range" in out["error"]
    assert "1 to 50" in out["hint"] and not writes(session)


def test_limit_refuses_an_auto_limited_app(capsys):
    app = {**APP, "control_status": "AUTO_LIMITED"}
    _, plan, _ = run(routes(app), ["control", "limit", "a1"], capsys)
    assert plan["can_apply"] is False and "apply_with" not in plan
    assert "control lock a1" in plan["blockers"][0]
    code, out, session = run(routes(app), ["control", "limit", "a1", "--yes"], capsys)
    assert code == EXIT_REFUSED and not writes(session)


def test_state_change_between_plan_and_apply_is_refused(capsys):
    manual = {**APP, "control_status": "MANUAL_LIMITED", "effective": MANUAL_EFFECTIVE}
    _, plan, _ = run(routes(manual), ["control", "freeze", "a1"], capsys)
    assert "--expect MANUAL_LIMITED" in plan["apply_with"]
    # The limit was restored meanwhile: the planned freeze must not go through.
    code, out, session = run(routes(), apply_argv(plan), capsys)
    assert code == EXIT_REFUSED and "changed since the plan" in out["error"]
    assert not writes(session)


@pytest.mark.parametrize("status,endpoint", [
    ("MANUAL_LIMITED", "/app/resource_restore"),
    ("AUTO_LIMITED", "/app/auto_limit_restore"),
])
def test_restore_picks_the_endpoint_for_the_owner(status, endpoint, capsys):
    app = {**APP, "control_status": status, "effective": MANUAL_EFFECTIVE}
    auto = {"apps": [{"app_id": "a1", "app_name": "ollama", "control_status": "AUTO_LIMITED",
                      "effective": MANUAL_EFFECTIVE}], "sys_pressure_level": "critical"}
    r = routes(app, {("POST", "/app/auto_limited_apps"): auto, ("POST", endpoint): ok("Restored")})
    _, plan, _ = run(r, ["control", "restore", "a1"], capsys)
    assert plan["current"]["cpu_pct_of_usage"] == 30.0
    if status == "AUTO_LIMITED":
        assert any("pressure is critical" in e for e in plan["effects"])
        assert plan["undo_with"] == "smartune control exclusion-remove a1"
    code, out, session = run(r, apply_argv(plan), capsys)
    assert code == 0 and [c["path"] for c in writes(session)] == [endpoint]


def test_restore_reaches_an_unregistered_auto_limited_instance(capsys):
    auto = {"apps": [{"app_id": "pid-77", "effective_app_id": "app-x.scope", "app_name": "x",
                      "control_status": "AUTO_LIMITED"}], "sys_pressure_level": "medium"}
    r = routes(None, {("POST", "/app/auto_limited_apps"): auto,
                  ("POST", "/app/auto_limit_restore"): ok("Restored")})
    _, plan, _ = run(r, ["control", "restore", "app-x.scope"], capsys)
    assert plan["can_apply"] and plan["app"]["controlled"] is False


def test_restore_of_an_unlimited_app_does_nothing(capsys):
    _, plan, _ = run(routes(), ["control", "restore", "a1"], capsys)
    assert plan["can_apply"] is False and plan["no_change"]
    code, out, session = run(routes(), ["control", "restore", "a1", "--yes"], capsys)
    assert code == 0 and out["applied"] is False and not writes(session)


def test_unknown_app_is_a_usage_error(capsys):
    code, out, _ = run(routes(), ["control", "priority", "nope", "high"], capsys)
    assert code == EXIT_USAGE and "smartune apps" in out["hint"]


def test_priority_plan_and_undo(capsys):
    _, plan, _ = run(routes(), ["control", "priority", "a1", "critical"], capsys)
    assert plan["current"] == {"priority": "low"} and plan["target"] == {"priority": "critical"}
    assert any("OOM" in e for e in plan["effects"])
    assert plan["undo_with"] == "smartune control priority a1 low"
    _, same, _ = run(routes(), ["control", "priority", "a1", "low"], capsys)
    assert same["no_change"]


def test_affinity_validates_the_core_list(capsys):
    code, out, _ = run(routes(), ["control", "affinity", "a1", "custom"], capsys)
    assert code == EXIT_USAGE
    code, out, _ = run(routes(), ["control", "affinity", "a1", "custom", "--cpus", "0-3;rm"], capsys)
    assert code == EXIT_USAGE
    r = routes(None, {("POST", "/app/cpu_affinity"): ok("ok")})
    _, plan, _ = run(r, ["control", "affinity", "a1", "custom", "--cpus", "0-3,8"], capsys)
    _, out, session = run(r, apply_argv(plan), capsys)
    assert writes(session)[0]["body"] == {"app_id": "a1", "class": "custom", "cpus": "0-3,8"}


def test_skipped_limit_is_not_reported_as_applied(capsys):
    r = routes(None, {("POST", "/app/resource_limit"): ok("App usage is negligible", {"skipped": True})})
    _, out, _ = run(r, ["control", "limit", "a1", "--yes"], capsys)
    assert out["applied"] is False and out["skipped"] is True
    assert out["message"] == "App usage is negligible"


def test_server_refusal_is_relayed(capsys):
    manual = {**APP, "control_status": "MANUAL_LIMITED"}
    r = routes(manual, {("POST", "/app/freeze"): FakeResponse(
        {"retcode": 103, "retmsg": "App has no manual limit; apply a limit first", "data": {}})})
    code, out, _ = run(r, ["control", "freeze", "a1", "--yes"], capsys)
    assert code == 5 and out["error"] == "App has no manual limit; apply a limit first"


def test_cancel_launch_needs_a_queued_app(capsys):
    r = routes(None, {("POST", "/app/get_pending_app"): [{"app_id": "a1", "app_name": "ollama"}],
                  ("POST", "/app/cancel_relaunch"): ok()})
    _, plan, _ = run(r, ["control", "cancel-launch", "a1"], capsys)
    assert plan["can_apply"] and "--expect" not in plan["apply_with"]
    _, plan, _ = run(r, ["control", "cancel-launch", "zz"], capsys)
    assert plan["no_change"]


def test_writes_only_through_control_and_only_to_control_routes():
    session = FakeSession({})
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    with pytest.raises(Exception) as exc:
        client.post("/app/resource_limit", {"app_id": "a1"})  # the read path
    assert exc.value.exit_code == EXIT_USAGE
    for path in ("/app/kill_process", "/app/suspend_process", "/app/set_oom_score",
                 "/app/set_to_control", "/app/get_controlled_app"):
        with pytest.raises(Exception):
            client.control(path, {})
    assert not session.calls


# -- register / unregister -------------------------------------------------------

BENCH_PROC = {"pid": 501, "comm": "benchmark_app", "process_name": "benchmark_app",
              "exe": "/usr/bin/benchmark_app", "cmdline": "benchmark_app -m m.xml -d GPU",
              "cgroup_unit": "vte-spawn-1.scope", "ppid": 1, "score": 9}
SHELL_PROC = {"pid": 502, "comm": "bash", "process_name": "bash", "exe": "/usr/bin/bash",
              "cmdline": "bash -c 'grep benchmark_app'", "cgroup_unit": "", "ppid": 1, "score": 1}
EXTRACT = {"bpf_name": ["benchmark_app"], "process_names": ["benchmark_app"],
           "commandline": ["benchmark_app -m m.xml -d GPU"], "cgroup_ids": ["vte-spawn-1.scope"],
           "id_suggestion": "vte-spawn-1.scope"}


def register_routes(apps=(), candidates=(BENCH_PROC, SHELL_PROC), extract=EXTRACT, extra=None):
    r = {("POST", "/app/get_controlled_app"): list(apps),
         ("POST", "/app/discover_search"): {"count": len(candidates), "candidates": list(candidates)},
         ("POST", "/app/discover_extract"): extract,
         ("POST", "/app/new_controlled_app"): ok("Application 'benchmark_app' added",
                                                 {"name": "benchmark_app", "id": "benchmark-app.id"})}
    r.update(extra or {})
    return r


def test_register_plan_picks_the_program_and_pins_its_identity(capsys):
    code, plan, session = run(register_routes(), ["control", "register", "benchmark_app"], capsys)
    assert code == 0 and plan["can_apply"] and not writes(session)
    # Only the process that is the program; a shell mentioning it is listed, not taken.
    extract_call = next(c for c in session.calls if c["path"] == "/app/discover_extract")
    assert extract_call["body"]["pids"] == [501]
    assert [p["pid"] for p in plan["current"]["other_matches_not_included"]] == [502]
    # A terminal scope is not a stable id; the name-derived one is used instead.
    assert plan["target"]["app_id"] == "benchmark-app.id"
    assert apply_argv(plan) == [
        "control", "register", "benchmark_app", "--id", "benchmark-app.id", "--priority", "low",
        "--bpf-name", "benchmark_app", "--process-name", "benchmark_app",
        "--commandline", "benchmark_app -m m.xml -d GPU", "--yes"]
    assert plan["undo_with"] == "smartune control unregister benchmark-app.id"


def test_register_apply_sends_the_pinned_identity_without_rediscovering(capsys):
    _, plan, _ = run(register_routes(), ["control", "register", "benchmark_app"], capsys)
    code, out, session = run(register_routes(), apply_argv(plan), capsys)
    assert code == 0 and out["applied"] is True
    assert not any(c["path"].startswith("/app/discover") for c in session.calls)
    (call,) = writes(session)
    assert call["path"] == "/app/new_controlled_app"
    assert call["body"] == {
        "name": "benchmark_app", "id": "benchmark-app.id", "priority": "low",
        "remark": "[smartune-control] registered by smartune-cli",
        "commandline": "benchmark_app -m m.xml -d GPU",
        "bpf_name": ["benchmark_app"], "process_names": ["benchmark_app"]}


@pytest.mark.parametrize("name,proc", [
    ("python3", {**BENCH_PROC, "comm": "python3", "process_name": "python3", "exe": "/usr/bin/python3"}),
    ("smartune", {**BENCH_PROC, "comm": "smartune", "process_name": "smartune", "exe": "/opt/smartune"}),
    ("claude", {**BENCH_PROC, "comm": "claude", "process_name": "claude", "exe": "/usr/bin/claude"}),
])
def test_register_refuses_generic_and_protected_identities(name, proc, capsys):
    extract = {**EXTRACT, "bpf_name": [proc["comm"]], "process_names": [proc["process_name"]]}
    r = register_routes(candidates=[proc], extract=extract)
    _, plan, _ = run(r, ["control", "register", name], capsys)
    assert plan["can_apply"] is False and plan["blockers"]
    code, _, session = run(r, ["control", "register", name, "--yes"], capsys)
    assert code == EXIT_REFUSED and not writes(session)


def test_register_needs_a_running_process(capsys):
    _, plan, _ = run(register_routes(candidates=[SHELL_PROC]), ["control", "register", "benchmark_app"],
                     capsys)
    assert plan["can_apply"] is False and "No running process" in plan["blockers"][0]


def test_register_of_an_existing_app_changes_nothing(capsys):
    existing = {**APP, "app_id": "benchmark-app.id", "app_name": "benchmark_app"}
    _, plan, _ = run(register_routes(apps=[existing]), ["control", "register", "benchmark_app"], capsys)
    assert plan["no_change"]


def test_unregister_only_apps_this_cli_registered(capsys):
    ours = {**APP, "remark": "[smartune-control] registered by openclaw"}
    r = routes(ours, {("POST", "/app/purge_controlled_app"): ok("purged", {"id": "a1"})})
    _, plan, _ = run(r, ["control", "unregister", "a1"], capsys)
    assert plan["can_apply"]
    code, out, session = run(r, apply_argv(plan), capsys)
    assert code == 0 and writes(session)[0]["body"] == {"id": "a1"}
    # Registered from the dashboard: refused before anything is sent.
    theirs = {**APP, "remark": "my inference server"}
    code, out, session = run(routes(theirs), ["control", "unregister", "a1", "--yes"], capsys)
    assert code == EXIT_REFUSED and "dashboard" in out["error"] and not writes(session)


# -- device frequency ----------------------------------------------------------------

def freq_unit(uid, kind, hw, rng, limited=None, **extra):
    return {"id": uid, "kind": kind, "label": uid.split(":")[1], "group": extra.pop("group", kind),
            "hw_min_mhz": hw[0], "hw_max_mhz": hw[1], "min_mhz": rng[0], "max_mhz": rng[1],
            "cur_mhz": 1234, "limited": limited is not None,
            "original_min_mhz": limited[0] if limited else None,
            "original_max_mhz": limited[1] if limited else None, "driver": "d", **extra}


P0 = freq_unit("cpu:policy0", "cpu", (800, 4800), (800, 4800), cpus=[0, 1], core_class="p")
P1 = freq_unit("cpu:policy1", "cpu", (800, 4600), (800, 4600), cpus=[2, 3], core_class="p")
LPE = freq_unit("cpu:policy4", "cpu", (700, 3500), (700, 2000), limited=(700, 3500),
                cpus=[4, 5, 6, 7], core_class="lpe")
GT0 = freq_unit("gpu:card0/gt0", "gpu", (300, 2250), (300, 2250), group="card0")


def freq_routes(units=(P0, P1, LPE, GT0), extra=None):
    r = {("GET", "/device/freq/status"): {"units": list(units)}}
    r.update(extra or {})
    return r


def test_freq_status_is_read_only_and_says_what_restore_puts_back(capsys):
    code, out, session = run(freq_routes(), ["control", "freq-status", "--kind", "cpu"], capsys)
    assert code == 0 and not writes(session)
    assert [u["id"] for u in out["units"]] == ["cpu:policy0", "cpu:policy1", "cpu:policy4"]
    assert out["limited_by_smartune"] == ["cpu:policy4"]
    lpe = out["units"][2]
    assert lpe["cpus"] == "4-7" and lpe["restores_to"] == "700-3500 MHz"


def test_freq_limit_plan_expands_groups_pins_both_sides_and_changes_nothing(capsys):
    code, plan, session = run(freq_routes(), ["control", "freq-limit", "p", "--max-mhz", "3000"], capsys)
    assert code == 0 and plan["can_apply"] and not writes(session)
    # The group becomes explicit unit ids; the minimum not given stays what each unit has.
    assert apply_argv(plan) == ["control", "freq-limit", "cpu:policy0", "cpu:policy1",
                                "--max-mhz", "3000", "--yes"]
    assert plan["summary"].startswith("Cap the maximum frequency at 3000 MHz on 2 unit(s)")
    assert plan["current"] == {"cpu:policy0": "800-4800 MHz", "cpu:policy1": "800-4600 MHz"}
    assert plan["target"] == {"cpu:policy0": "800-3000 MHz", "cpu:policy1": "800-3000 MHz"}
    assert any("every app" in e for e in plan["effects"])
    assert not any("hardware range are applied" in e for e in plan["effects"])
    assert plan["undo_with"] == "smartune control freq-restore cpu:policy0 cpu:policy1"
    # Live clocks change by the second; they stay out of the plan (the MCP form's identity).
    assert "1234" not in str({k: plan[k] for k in ("summary", "current", "target", "effects")})


def test_freq_limit_apply_sends_the_planned_request_and_reads_back(capsys):
    r = freq_routes(extra={("POST", "/device/freq/set"): ok("ok", {"results": {}})})
    _, plan, _ = run(r, ["control", "freq-limit", "cpu:policy0", "gpu", "--min-mhz", "1000",
                         "--max-mhz", "2000"], capsys)
    code, out, session = run(r, apply_argv(plan), capsys)
    assert code == 0 and out["applied"] is True
    (call,) = writes(session)
    assert call["path"] == "/device/freq/set"
    assert call["body"] == {"units": ["cpu:policy0", "gpu:card0/gt0"], "min_mhz": 1000.0, "max_mhz": 2000.0}
    assert set(out["after"]) == {"cpu:policy0", "gpu:card0/gt0"}


def test_freq_limit_warns_about_clamping_a_slow_cpu_and_a_raised_floor(capsys):
    _, plan, _ = run(freq_routes(), ["control", "freq-limit", "cpu:policy0", "gpu",
                                     "--min-mhz", "1000", "--max-mhz", "2000"], capsys)
    effects = " ".join(plan["effects"])
    assert plan["target"]["gpu:card0/gt0"] == "1000-2000 MHz"
    assert "runs noticeably slower" in effects and "cpu:policy0" in effects  # 2000 < 4800 / 2
    assert "clocked up even when idle" in effects
    _, plan, _ = run(freq_routes(), ["control", "freq-limit", "gpu", "--max-mhz", "9000"], capsys)
    assert plan["target"]["gpu:card0/gt0"] == "300-2250 MHz"
    assert any("gpu:card0/gt0 (300-2250 MHz)" in e for e in plan["effects"])


@pytest.mark.parametrize("argv,needle", [
    (["control", "freq-limit", "cpu"], "--min-mhz, --max-mhz or both"),
    (["control", "freq-limit", "cpu", "--min-mhz", "3000", "--max-mhz", "2000"], "above --max-mhz"),
    (["control", "freq-limit", "npu", "--max-mhz", "500"], "matches npu"),
    (["control", "freq-limit", "e", "--max-mhz", "500"], "matches e"),  # this machine has no E cores
])
def test_freq_limit_rejects_bad_requests_before_any_write(argv, needle, capsys):
    code, out, session = run(freq_routes(), argv + ["--yes"], capsys)
    assert code == EXIT_USAGE and needle in out["error"] and not writes(session)


def test_freq_limit_to_the_current_range_changes_nothing(capsys):
    _, plan, _ = run(freq_routes(), ["control", "freq-limit", "cpu:policy4", "--min-mhz", "700",
                                     "--max-mhz", "2000"], capsys)
    assert plan["can_apply"] is False and plan["no_change"] and "apply_with" not in plan
    assert any("Already limited by SmarTune: cpu:policy4" in e for e in plan["effects"])


def test_freq_partial_failure_says_not_to_repeat(capsys):
    r = freq_routes(extra={("POST", "/device/freq/set"): FakeResponse(
        {"retcode": 103, "retmsg": "cpu:policy1: write failed: Permission denied", "data": {}})})
    code, out, _ = run(r, ["control", "freq-limit", "p", "--max-mhz", "3000", "--yes"], capsys)
    assert code == 5 and "policy1" in out["error"]
    assert "Do not repeat" in out["hint"] and "freq-status" in out["hint"]


def test_freq_restore_names_the_limited_units_explicitly(capsys):
    r = freq_routes(extra={("POST", "/device/freq/restore"): ok("ok")})
    _, plan, _ = run(r, ["control", "freq-restore"], capsys)
    assert plan["can_apply"]
    assert apply_argv(plan) == ["control", "freq-restore", "cpu:policy4", "--yes"]
    assert plan["current"] == {"cpu:policy4": "700-2000 MHz"}
    assert plan["target"] == {"cpu:policy4": "700-3500 MHz"}
    code, out, session = run(r, apply_argv(plan), capsys)
    (call,) = writes(session)
    # Never an empty list: the server would restore every unit, including ones limited later.
    assert call["body"] == {"units": ["cpu:policy4"]}


def test_freq_restore_of_units_smartune_did_not_limit_changes_nothing(capsys):
    _, plan, _ = run(freq_routes(), ["control", "freq-restore", "gpu"], capsys)
    assert plan["can_apply"] is False and "None of the selected units" in plan["no_change"]
    _, plan, _ = run(freq_routes((P0, GT0)), ["control", "freq-restore"], capsys)
    assert plan["no_change"] == "SmarTune is not limiting any device frequency."
    code, out, session = run(freq_routes((P0, GT0)), ["control", "freq-restore", "--yes"], capsys)
    assert code == 0 and out["applied"] is False and not writes(session)


GT0_RPE = freq_unit("gpu:card0/gt0", "gpu", (100, 1500), (550, 1500), group="card0")
GT1_RPE = freq_unit("gpu:card0/gt1", "gpu", (100, 1000), (400, 1000), group="card0")


def test_freq_cap_keeps_each_units_own_minimum(capsys):
    # Xe gts start with a minimum above the hardware floor; capping the maximum must not lower it.
    r = freq_routes((GT0_RPE, GT1_RPE), {("POST", "/device/freq/set"): ok("ok", {"results": {}})})
    _, plan, _ = run(r, ["control", "freq-limit", "gpu", "--max-mhz", "900"], capsys)
    assert plan["target"] == {"gpu:card0/gt0": "550-900 MHz", "gpu:card0/gt1": "400-900 MHz"}
    assert any("keeps its current minimum" in e for e in plan["effects"])
    code, out, session = run(r, apply_argv(plan), capsys)
    assert code == 0 and out["applied"] is True
    # Different minimums: one request per pair, so neither gt moves its floor.
    assert [c["body"] for c in writes(session)] == [
        {"units": ["gpu:card0/gt0"], "min_mhz": 550, "max_mhz": 900.0},
        {"units": ["gpu:card0/gt1"], "min_mhz": 400, "max_mhz": 900.0}]


def test_freq_cap_below_a_kept_minimum_is_blocked(capsys):
    _, plan, _ = run(freq_routes((GT0_RPE, GT1_RPE)), ["control", "freq-limit", "gpu", "--max-mhz", "500"], capsys)
    assert plan["can_apply"] is False and "apply_with" not in plan
    assert "gpu:card0/gt0 (now 550-1500 MHz)" in plan["blockers"][0] and "--min-mhz" in plan["blockers"][0]
    code, out, session = run(freq_routes((GT0_RPE, GT1_RPE)),
                             ["control", "freq-limit", "gpu", "--max-mhz", "500", "--yes"], capsys)
    assert code == EXIT_REFUSED and not writes(session)
