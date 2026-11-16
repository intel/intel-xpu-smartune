# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Offline tests for `smartune doctor`. The socket and TLS handshake are faked, so
# these run without a server. (A real end-to-end check against a self-signed
# HTTPS server was done by hand when doctor was written.)

import json
import os
import socket
import ssl
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from smartune_sdk import cli, doctor  # noqa: E402
from smartune_sdk.client import EXIT_AUTH, EXIT_CONNECTION, SmartuneClient  # noqa: E402
from test_cli import FakeResponse, FakeSession  # noqa: E402

CAPS = {"capabilities": 1, "benchmark": 1, "diagnostics": 1, "shutdown": 0,
        "version": "1.8.0", "api_level": 1}
DYNAMIC = {"cpu": {"pkg_power_w": 11.0}, "pressure": {"level": "low"},
           "gpu": {"gpu_usage": {"available": True}},
           "npu": {"npu_smi": {"available": False, "error": None}}}


class FakeSocket:
    def close(self):
        pass


class FakeContext:
    def __init__(self, error=None):
        self.error = error

    check_hostname = True
    peer = b"server-cert-der"

    def wrap_socket(self, sock, server_hostname=None):
        if self.error:
            raise self.error
        return self

    def getpeercert(self, binary_form=False):
        return self.peer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def network(monkeypatch):
    """Controls what the TCP connect and TLS handshake do."""
    state = {"connect": None, "tls": None}

    def connect(addr, timeout=None):
        if state["connect"]:
            raise state["connect"]
        return FakeSocket()

    monkeypatch.setattr(doctor.socket, "create_connection", connect)
    monkeypatch.setattr(doctor, "_tls_context", lambda cafile=None: FakeContext(state["tls"]))
    for var in ("SMARTUNE_TOKEN", "SMARTUNE_TOKEN_FILE", "SMARTUNE_CACERT"):
        monkeypatch.delenv(var, raising=False)
    return state


def run_doctor(capsys, routes=None, token="t", cacert=None, **client_kwargs):
    routes = routes if routes is not None else {
        ("GET", "/smartune/capabilities"): CAPS, ("GET", "/monitor/dynamic_info"): DYNAMIC}
    client = SmartuneClient(url="https://127.0.0.1:9001", token=token, cacert=cacert,
                            session=FakeSession(routes), **client_kwargs)
    code = cli.main(["doctor"], client=client)
    out = json.loads(capsys.readouterr().out)
    return code, out, {c["name"]: c for c in out["checks"]}


@pytest.fixture
def cacert(tmp_path):
    path = tmp_path / "b_server.crt"
    path.write_text("cert")
    return str(path)


def test_all_good(network, capsys, cacert):
    code, out, checks = run_doctor(capsys, cacert=cacert)
    assert code == 0 and out["ok"] is True
    assert [c["status"] for c in out["checks"]] == ["ok"] * 7
    assert "npu not present" in checks["telemetry"]["detail"]


def test_service_not_running(network, capsys, cacert):
    network["connect"] = ConnectionRefusedError(111, "Connection refused")
    code, out, checks = run_doctor(capsys, cacert=cacert)
    assert code == EXIT_CONNECTION and not out["ok"]
    assert checks["connect"]["status"] == "fail" and "intel-smartune" in checks["connect"]["hint"]
    assert all(checks[n]["status"] == "skip" for n in ("tls", "auth", "version", "features", "telemetry"))


def test_certificate_not_trusted(network, capsys):
    error = ssl.SSLCertVerificationError("verify failed")
    error.verify_message = "self-signed certificate"
    network["tls"] = error
    code, out, checks = run_doctor(capsys)
    assert code == EXIT_CONNECTION
    assert checks["config"]["status"] == "warn" and "SMARTUNE_CACERT" in checks["config"]["hint"]
    assert checks["tls"]["status"] == "fail" and "self-signed" in checks["tls"]["detail"]


def test_certificate_file_missing(network, capsys):
    code, _, checks = run_doctor(capsys, cacert="/nonexistent/b_server.crt")
    assert code == EXIT_CONNECTION
    assert checks["config"]["status"] == "fail" and checks["tls"]["status"] == "fail"
    assert "does not exist" in checks["tls"]["detail"]


def test_token_rejected(network, capsys, cacert):
    denied = FakeResponse({"retcode": 401, "data": {"authenticated": False}}, status=401)
    code, out, checks = run_doctor(capsys, routes={("GET", "/smartune/capabilities"): denied}, cacert=cacert)
    assert code == EXIT_AUTH and out["summary"].startswith("auth failed")
    assert checks["tls"]["status"] == "ok" and checks["auth"]["status"] == "fail"


def test_no_token_still_checks_the_connection(network, capsys, cacert):
    code, _, checks = run_doctor(capsys, token="", cacert=cacert)
    assert code == EXIT_AUTH
    assert checks["config"]["status"] == "fail" and checks["connect"]["status"] == "ok"
    assert checks["tls"]["status"] == "ok" and checks["auth"]["status"] == "skip"


def test_unreadable_token_file_is_reported_not_raised(network, capsys, monkeypatch, cacert):
    monkeypatch.setenv("SMARTUNE_TOKEN_FILE", "/nonexistent/api_token")
    client = SmartuneClient(url="https://127.0.0.1:9001", cacert=cacert, session=FakeSession({}))
    assert client.token_error is not None
    code = cli.main(["doctor"], client=client)
    out = json.loads(capsys.readouterr().out)
    assert code == EXIT_AUTH and "/nonexistent/api_token" in out["checks"][0]["detail"]


def test_unreadable_token_file_still_fails_other_commands(capsys, monkeypatch):
    monkeypatch.delenv("SMARTUNE_TOKEN", raising=False)
    monkeypatch.setenv("SMARTUNE_TOKEN_FILE", "/nonexistent/api_token")
    client = SmartuneClient(url="https://127.0.0.1:9001", session=FakeSession({}))
    code = cli.main(["caps"], client=client)
    out = json.loads(capsys.readouterr().out)
    assert code == EXIT_AUTH and "/nonexistent/api_token" in out["error"]


@pytest.mark.parametrize("caps,status,needle", [
    ({k: v for k, v in CAPS.items() if k != "api_level"}, "warn", "older than SmarTune 1.5"),
    (dict(CAPS, api_level=99), "warn", "upgrade the smartune SDK"),
    (dict(CAPS, api_level=1), "ok", None),
])
def test_version_check(network, capsys, cacert, caps, status, needle):
    routes = {("GET", "/smartune/capabilities"): caps, ("GET", "/monitor/dynamic_info"): DYNAMIC}
    _, _, checks = run_doctor(capsys, routes=routes, cacert=cacert)
    assert checks["version"]["status"] == status
    if needle:
        assert needle in checks["version"]["hint"]


def test_missing_features_and_broken_telemetry_warn(network, capsys, cacert):
    caps = dict(CAPS, benchmark=0, diagnostics=0)
    dynamic = dict(DYNAMIC, gpu={"gpu_usage": {"available": False, "error": "xe PMU not readable"}})
    routes = {("GET", "/smartune/capabilities"): caps, ("GET", "/monitor/dynamic_info"): dynamic}
    code, out, checks = run_doctor(capsys, routes=routes, cacert=cacert)
    assert code == 0 and out["ok"] is True  # warnings do not fail the run
    assert checks["features"]["status"] == "warn" and "smartune diag" in checks["features"]["hint"]
    assert checks["telemetry"]["status"] == "warn" and "xe PMU" in checks["telemetry"]["detail"]


def test_quiet_mode_skips_telemetry(network, capsys, cacert):
    dynamic = dict(DYNAMIC, quiet_mode={"active": True})
    routes = {("GET", "/smartune/capabilities"): CAPS, ("GET", "/monitor/dynamic_info"): dynamic}
    _, _, checks = run_doctor(capsys, routes=routes, cacert=cacert)
    assert checks["telemetry"]["status"] == "skip" and "quiet mode" in checks["telemetry"]["detail"]


def test_doctor_sends_no_unauthenticated_request(network, capsys, cacert):
    # An unauthenticated request makes the server log AUTH_ACCESS_DENIED.
    session = FakeSession({("GET", "/smartune/capabilities"): CAPS, ("GET", "/monitor/dynamic_info"): DYNAMIC})
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", cacert=cacert, session=session)
    cli.main(["doctor"], client=client)
    capsys.readouterr()
    assert session.calls and all(c["headers"].get("X-Auth-Token") == "t" for c in session.calls)
    # The telemetry probe must name sections: without them the server starts its full collector.
    assert all(c["params"].get("sections") for c in session.calls if c["path"] == "/monitor/dynamic_info")


def test_default_config_dir_supplies_token_and_cert(isolated_client_config, monkeypatch):
    # skills/install.sh copies both files here; no SMARTUNE_* variable is needed then.
    for var in ("SMARTUNE_TOKEN", "SMARTUNE_TOKEN_FILE", "SMARTUNE_CACERT"):
        monkeypatch.delenv(var, raising=False)
    isolated_client_config.mkdir(parents=True)
    (isolated_client_config / "api_token").write_text("from-default\n")
    (isolated_client_config / "b_server.crt").write_text("cert")
    client = SmartuneClient(session=FakeSession({}))
    assert client.token == "from-default"
    assert client.cacert == str(isolated_client_config / "b_server.crt")
    # An explicit variable still wins over the default file.
    monkeypatch.setenv("SMARTUNE_TOKEN", "from-env")
    assert SmartuneClient(session=FakeSession({})).token == "from-env"


def test_no_default_files_means_no_token(isolated_client_config, monkeypatch):
    for var in ("SMARTUNE_TOKEN", "SMARTUNE_TOKEN_FILE", "SMARTUNE_CACERT"):
        monkeypatch.delenv(var, raising=False)
    client = SmartuneClient(session=FakeSession({}))
    assert client.token == "" and client.cacert is None and client.token_error is None


def test_address_mismatch_gets_its_own_hint(network, capsys, cacert):
    error = ssl.SSLCertVerificationError("verify failed")
    error.verify_code, error.verify_message = 64, "IP address mismatch, certificate is not valid for '192.168.1.20'."
    network["tls"] = error
    code, _, checks = run_doctor(capsys, cacert=cacert)
    assert code == EXIT_CONNECTION and "does not cover this address" in checks["tls"]["detail"]
    assert "--remote" in checks["tls"]["hint"] and "Do not disable" in checks["tls"]["hint"]


def test_pinned_certificate_must_be_the_one_presented(network, capsys, cacert, monkeypatch):
    monkeypatch.setattr(doctor, "_tls_context", lambda cafile=None: FakeContext(None))
    import hashlib
    good = hashlib.sha256(FakeContext.peer).hexdigest()
    session = FakeSession({("GET", "/smartune/capabilities"): CAPS, ("GET", "/monitor/dynamic_info"): DYNAMIC})
    client = SmartuneClient(url="https://192.168.1.20:9001", token="t", cacert=cacert, session=session)
    client.pinned_fingerprint = good
    cli.main(["doctor"], client=client)
    tls = [c for c in json.loads(capsys.readouterr().out)["checks"] if c["name"] == "tls"][0]
    assert tls["status"] == "ok" and "pinned" in tls["detail"]
    client.pinned_fingerprint = "0" * 64
    code = cli.main(["doctor"], client=client)
    tls = [c for c in json.loads(capsys.readouterr().out)["checks"] if c["name"] == "tls"][0]
    assert code == EXIT_CONNECTION and tls["status"] == "fail" and "pinned" in tls["detail"]
