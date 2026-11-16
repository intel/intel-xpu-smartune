# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Offline tests for `smartune bench run --log`, `bench wait` and `bench results`.

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from smartune_sdk import cli  # noqa: E402
from smartune_sdk.client import EXIT_API, SmartuneClient  # noqa: E402
from test_cli import FakeResponse, run  # noqa: E402

JOB = {"id": "a1b2c3d4e5f6", "kind": "run", "status": "running", "returncode": None,
       "started_at": 1761750000.0, "finished_at": None, "duration": 12.0,
       "log_path": "/x/run.log", "meta": {"run_name": "qwen-sweep"}}


class LogSession:
    """Serves GET /bench/run/<id> with a log read forwards from ?offset=, like jobs.read_log."""

    def __init__(self, log, statuses=("done",), max_bytes=256 * 1024):
        self.log = log.encode("utf-8")
        self.statuses = list(statuses)
        self.max_bytes = max_bytes
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=None):
        path = url.split("9001", 1)[1]
        self.calls.append({"path": path, "params": params})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        job = dict(JOB, status=status)
        if params and "offset" in params:
            offset = int(params["offset"])
            chunk = self.log[offset:offset + self.max_bytes]
            job.update(chunk=chunk.decode("utf-8"), offset=offset + len(chunk), size=len(self.log))
        return FakeResponse({"retcode": 0, "data": job})


def run_with(session, argv, capsys):
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    code = cli.main(argv, client=client)
    return code, json.loads(capsys.readouterr().out)


def test_run_log_returns_the_tail_of_a_short_log(capsys):
    session = LogSession("line1\nline2\nfinal line\n")
    code, out = run_with(session, ["bench", "run", JOB["id"], "--log", "--log-chars", "11"], capsys)
    assert code == 0
    assert out["log_tail"].endswith("final line\n")
    assert "earlier bytes omitted" in out["log_tail"]
    assert "chunk" not in out and "size" not in out
    assert len(session.calls) == 1  # the whole log came back in the first read


def test_run_log_rereads_from_the_end_of_a_long_log(capsys):
    session = LogSession("x" * 1000 + "THE END", max_bytes=100)
    code, out = run_with(session, ["bench", "run", JOB["id"], "--log", "--log-chars", "50"], capsys)
    assert code == 0 and out["log_tail"].endswith("THE END")
    assert [c["params"]["offset"] for c in session.calls] == [0, 1007 - 50]


def test_run_without_log_sends_no_offset(capsys):
    session = LogSession("ignored")
    _, out = run_with(session, ["bench", "run", JOB["id"]], capsys)
    assert session.calls[0]["params"] in (None, {}) and "log_tail" not in out


def test_wait_polls_until_done(capsys, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    session = LogSession("", statuses=("running", "running", "done"))
    code, out = run_with(session, ["bench", "wait", JOB["id"], "--interval", "1"], capsys)
    assert code == 0 and out["finished"] is True and out["run"]["status"] == "done"
    assert len(session.calls) == 3 and "log_tail" not in out["run"]


def test_wait_attaches_log_tail_on_failure(capsys, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    session = LogSession("Traceback: model not found\n", statuses=("running", "failed"))
    _, out = run_with(session, ["bench", "wait", JOB["id"]], capsys)
    assert out["run"]["status"] == "failed" and "model not found" in out["run"]["log_tail"]


def test_wait_times_out_with_a_hint(capsys, monkeypatch):
    clock = iter(range(0, 1000, 4))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    session = LogSession("", statuses=("running",))
    code, out = run_with(session, ["bench", "wait", JOB["id"], "--timeout", "10"], capsys)
    assert code == 0 and out["finished"] is False
    assert f"smartune bench wait {JOB['id']}" in out["hint"]


def test_wait_without_id_uses_the_current_run(capsys, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    routes = {("GET", "/bench/run"): {"current": JOB, "recent": []},
              ("GET", f"/bench/run/{JOB['id']}"): dict(JOB, status="done")}
    code, out, session = run(routes, ["bench", "wait"], capsys)
    assert code == 0 and out["finished"] is True
    assert session.calls[1]["path"] == f"/bench/run/{JOB['id']}"


def test_wait_without_id_and_nothing_running(capsys):
    code, out, _ = run({("GET", "/bench/run"): {"current": None, "recent": []}}, ["bench", "wait"], capsys)
    assert code == 0 and out["current"] is None


def test_wait_unknown_run_is_an_api_error(capsys):
    missing = FakeResponse({"retcode": 404, "retmsg": "Unknown benchmark job: nope"})
    code, out, _ = run({("GET", "/bench/run/nope"): missing}, ["bench", "wait", "nope"], capsys)
    assert code == EXIT_API and "Unknown benchmark job" in out["error"]


def _row(job, model, device, status="ok", **extra):
    return {"backend": "genai", "run": f"{job}_{device}", "job": job, "job_started_at": 1761750000.0,
            "ov": "2026.1", "updated_at": 1761750100.0, "model": model, "device": device,
            "precision": "int4", "quant": "int4", "mode": "genai", "batch_size": "1",
            "status": status, "task": "text", "case_dir": "/long/path", "log_file": "/long/path/b.log",
            "model_dir": "/models/x", "failure_reason": extra.get("failure_reason"),
            "metrics": {"kpi_throughput_tokens_s": 21.5, "kpi_first_latency_ms": 640.0,
                        "d_speedup_vs_cpu": 3.1, "cpu_usage_percent_median": 40.0}}


MATRIX = {
    "rows": [
        _row("sweep-new", "Qwen3-8B", "gpu"),
        _row("sweep-new", "Qwen3-8B", "npu", status="failed: compile", failure_reason="unsupported op"),
        _row("sweep-new", "Llama-3-8B", "gpu"),
        _row("sweep-old", "Qwen3-8B", "cpu"),
    ],
    "dimensions": {"models": ["Llama-3-8B", "Qwen3-8B"], "devices": ["cpu", "gpu", "npu"],
                   "precisions": ["int4"], "backends": ["genai"], "runs": [], "jobs": ["sweep-new", "sweep-old"],
                   "ovs": ["2026.1"]},
    "metrics": [
        {"key": "kpi_throughput_tokens_s", "label": "throughput", "unit": "tok/s", "higher_is_better": True,
         "group": "kpi", "group_label": "KPI", "description": None},
        {"key": "kpi_first_latency_ms", "label": "TTFT", "unit": "ms", "higher_is_better": False,
         "group": "kpi", "group_label": "KPI", "description": "First token"},
        {"key": "d_speedup_vs_cpu", "label": "speedup vs CPU", "unit": "×", "higher_is_better": True,
         "group": "derived", "group_label": "Comparison", "description": None},
        {"key": "cpu_usage_percent_median", "label": "CPU usage", "unit": "%", "higher_is_better": None,
         "group": "cpu", "group_label": "CPU", "description": None},
    ],
    "primary_metric": "kpi_throughput_tokens_s",
    "benchmarks_dir": "/opt/intel/smartune/benchmark/runtime/benchmarks",
    "metrics_available": True,
}


def test_results_default_keeps_kpis_and_drops_paths(capsys):
    code, out, _ = run({("GET", "/bench/results/matrix"): MATRIX}, ["bench", "results"], capsys)
    assert code == 0 and out["matched"] == 4
    row = out["rows"][0]
    assert set(row["metrics"]) == {"kpi_throughput_tokens_s", "kpi_first_latency_ms", "d_speedup_vs_cpu"}
    assert "case_dir" not in row and "model_dir" not in row and "log_file" not in row
    assert {m["key"] for m in out["metrics"]} == set(row["metrics"])
    assert out["jobs"] == ["sweep-new", "sweep-old"] and "benchmarks_dir" not in out


def test_results_filters(capsys):
    routes = {("GET", "/bench/results/matrix"): MATRIX}
    _, out, _ = run(routes, ["bench", "results", "--latest", "--model", "qwen3"], capsys)
    assert [(r["model"], r["device"]) for r in out["rows"]] == [("Qwen3-8B", "gpu"), ("Qwen3-8B", "npu")]
    assert out["rows"][1]["failure_reason"] == "unsupported op"
    _, out, _ = run(routes, ["bench", "results", "--job", "sweep-old", "--device", "CPU"], capsys)
    assert [r["job"] for r in out["rows"]] == ["sweep-old"]


def test_results_all_metrics_and_raw(capsys):
    routes = {("GET", "/bench/results/matrix"): MATRIX}
    _, out, _ = run(routes, ["bench", "results", "--metrics", "all", "-n", "1"], capsys)
    assert "cpu_usage_percent_median" in out["rows"][0]["metrics"] and len(out["rows"]) == 1
    _, out, _ = run(routes, ["bench", "results", "--raw"], capsys)
    assert out["benchmarks_dir"] == MATRIX["benchmarks_dir"]
