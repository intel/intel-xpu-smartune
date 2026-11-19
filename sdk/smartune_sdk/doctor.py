# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# `smartune doctor`: check, step by step, that this client can reach and use the
# SmarTune server. It is what an agent runs first after any connection or token
# error, and what an operator runs to accept a deployment.
#
# Each check reports ok / warn / fail / skip with one line of detail and, when it
# is not ok, one hint on how to fix it. A failed check skips the checks that
# depend on it, so the first fail is the thing to fix.
#
# The TCP and TLS checks use a bare socket handshake and send no HTTP request: an
# unauthenticated request would make the server log an AUTH_ACCESS_DENIED
# warning every time doctor runs.

import hashlib
import os
import socket
import ssl
from urllib.parse import urlsplit

from . import __version__
from .client import (ADDRESS_MISMATCH_HINT, INSTALL_CMD, EXIT_AUTH, EXIT_CONNECTION, SUPPORTED_API_LEVELS,
                     SmartuneError, token_source)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# OpenSSL verify codes for a certificate that is trusted but names another host.
X509_V_ERR_HOSTNAME_MISMATCH = 62
X509_V_ERR_IP_ADDRESS_MISMATCH = 64

# capabilities flag -> (feature name, what is unavailable without it)
FEATURES = (
    ("capabilities", "balancer", "apps, app, limits, pending and the control history in `diag why`"),
    ("diagnostics", "diagnostics", "every `smartune diag` command (the smartune-diagnose skill)"),
    ("benchmark", "benchmark", "every `smartune bench` command (the smartune-bench skill)"),
)


class Report(dict):
    """The doctor result. `exit_code` is that of the first failed check, else 0."""

    exit_code = 0


def _check(name, status, detail, hint=None):
    out = {"name": name, "status": status, "detail": detail}
    if hint:
        out["hint"] = hint
    return out


def _config(client):
    """(check, exit code if it failed, whether a usable token is configured)."""
    problems, warnings, detail = [], [], [f"url {client.url}"]

    has_token = bool(client.token)
    if client.token_error is not None:
        problems.append((client.token_error.message, client.token_error.hint, EXIT_AUTH))
    elif not has_token:
        problems.append(("no API token configured",
                         f"Run {INSTALL_CMD} once (it copies key/api_token to ~/.config/smartune/), "
                         "or set SMARTUNE_TOKEN_FILE to a readable copy of it.",
                         EXIT_AUTH))
    else:
        detail.append(f"token from {token_source() or 'the caller'}")

    if client.cacert:
        if not os.path.isfile(client.cacert) or not os.access(client.cacert, os.R_OK):
            problems.append((f"SMARTUNE_CACERT {client.cacert} is missing or not readable",
                             "Point SMARTUNE_CACERT at a readable copy of key/b_server.crt.",
                             EXIT_CONNECTION))
        else:
            detail.append(f"CA certificate {client.cacert}")
    elif urlsplit(client.url).scheme == "https":
        warnings.append(("SMARTUNE_CACERT is not set, so the system CA store is used",
                         f"SmarTune uses a self-signed certificate: run {INSTALL_CMD} (it copies key/b_server.crt "
                         "to ~/.config/smartune/), or set SMARTUNE_CACERT to it."))

    if problems:
        _, hint, code = problems[0]
        return _check("config", FAIL, "; ".join(p[0] for p in problems), hint), code, has_token
    if warnings:
        return _check("config", WARN, "; ".join(detail + [w[0] for w in warnings]), warnings[0][1]), 0, True
    return _check("config", OK, "; ".join(detail)), 0, True


def _endpoint(url):
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname or "127.0.0.1", port


def _connect(client):
    _, host, port = _endpoint(client.url)
    try:
        sock = socket.create_connection((host, port), timeout=min(client.timeout, 5))
    except OSError as exc:
        return _check("connect", FAIL, f"cannot open {host}:{port}: {exc.strerror or exc}",
                      "Start the SmarTune service (packaged: `systemctl status intel-smartune`) "
                      "or correct SMARTUNE_URL."), None
    return _check("connect", OK, f"{host}:{port} accepts connections"), sock


def _tls_context(cafile):
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def _tls(client, sock):
    scheme, host, _ = _endpoint(client.url)
    if scheme != "https":
        sock.close()
        return _check("tls", WARN, "SMARTUNE_URL is plain http; nothing is encrypted",
                      "The SmarTune server serves https; use https://127.0.0.1:9001.")
    if client.cacert and not os.path.isfile(client.cacert):
        sock.close()
        return _check("tls", FAIL, f"cannot verify the server: CA certificate {client.cacert} does not exist",
                      "Point SMARTUNE_CACERT at a readable copy of key/b_server.crt.")
    pinned = getattr(client, "pinned_fingerprint", None)
    try:
        context = _tls_context(client.cacert)
        if pinned:
            context.check_hostname = False  # replaced by the fingerprint comparison below
        with context.wrap_socket(sock, server_hostname=host) as tls:
            presented = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
        if pinned and presented != pinned:
            return _check("tls", FAIL, "the server did not present the pinned certificate",
                          "Copy key/b_server.crt again (skills/install.sh); the server may have regenerated "
                          "it. If SMARTUNE_CACERT is a CA certificate, set SMARTUNE_PIN_CERT=0.")
    except ssl.SSLCertVerificationError as exc:
        if getattr(exc, "verify_code", None) in (X509_V_ERR_HOSTNAME_MISMATCH, X509_V_ERR_IP_ADDRESS_MISMATCH):
            return _check("tls", FAIL, f"certificate does not cover this address: {exc.verify_message}",
                          ADDRESS_MISMATCH_HINT)
        return _check("tls", FAIL, f"certificate not trusted: {exc.verify_message}",
                      "Set SMARTUNE_CACERT to key/b_server.crt of the running instance "
                      "(copy it again if the server regenerated it).")
    except (ssl.SSLError, OSError) as exc:
        return _check("tls", FAIL, f"TLS handshake failed: {exc}",
                      "Check that SMARTUNE_URL points at the SmarTune https port.")
    finally:
        sock.close()
    if pinned:
        return _check("tls", OK, f"certificate verified: pinned to the server's own certificate "
                                 f"(sha256 {pinned[:16]}...), so any address works")
    return _check("tls", OK, "certificate verified")


def _auth(client):
    try:
        caps = client.capabilities() or {}
    except SmartuneError as exc:
        return _check("auth", FAIL, exc.message, exc.hint), None, exc.exit_code
    return _check("auth", OK, "token accepted"), caps, 0


def _version(caps):
    version, level = caps.get("version"), caps.get("api_level")
    detail = f"server {version or 'unknown'}, api_level {level if level is not None else 'unknown'}, " \
             f"sdk {__version__}"
    if level is None:
        return _check("version", WARN, detail,
                      "The server does not report its API level (older than SmarTune 1.5); "
                      "commands may still work, but upgrade the server to be sure.")
    if level not in SUPPORTED_API_LEVELS:
        newer = level > max(SUPPORTED_API_LEVELS)
        return _check("version", WARN, detail,
                      "The server is newer than this SDK: upgrade the smartune SDK." if newer
                      else "The server is older than this SDK: upgrade SmarTune or use a matching SDK.")
    return _check("version", OK, detail)


def _features(caps):
    served = [name for flag, name, _ in FEATURES if caps.get(flag) == 1]
    missing = [(name, what) for flag, name, what in FEATURES if caps.get(flag) != 1]
    detail = "served: " + (", ".join(served) or "monitor only")
    if missing:
        detail += "; not served: " + ", ".join(name for name, _ in missing)
        return _check("features", WARN, detail,
                      "Unavailable here: " + "; ".join(what for _, what in missing) + ".")
    return _check("features", OK, detail)


def _telemetry(client):
    try:
        data = client.dynamic_info(("cpu", "pressure", "gpu", "npu")) or {}
    except SmartuneError as exc:
        return _check("telemetry", FAIL, exc.message, exc.hint)

    sources, problems = {}, []
    sources["psi"] = "ok" if (data.get("pressure") or {}).get("level") else "missing"
    sources["rapl"] = "ok" if (data.get("cpu") or {}).get("pkg_power_w") is not None else "missing"
    for name, block in (("gpu", ((data.get("gpu") or {}).get("gpu_usage") or {})),
                        ("npu", ((data.get("npu") or {}).get("npu_smi") or {}))):
        if block.get("available"):
            sources[name] = "ok"
        elif block.get("error"):
            sources[name] = "error"
            problems.append(f"{name}: {block['error']}")
        else:
            sources[name] = "not present"

    detail = ", ".join(f"{k} {v}" for k, v in sources.items())
    if (data.get("quiet_mode") or {}).get("active"):
        return _check("telemetry", SKIP, detail + " (quiet mode: a benchmark run has paused collection)",
                      "Run `smartune doctor` again after the benchmark finishes.")
    if sources["psi"] != "ok":
        problems.insert(0, "no PSI pressure reading (needs /proc/pressure, kernel CONFIG_PSI)")
    if problems:
        return _check("telemetry", WARN, detail + "; " + "; ".join(problems),
                      "Metrics from these sources are unavailable; check the driver or kernel support. "
                      "cgroup v2 and eBPF are not reported by the API.")
    return _check("telemetry", OK, detail + " (cgroup v2 and eBPF are not reported by the API)")


def run_doctor(client):
    checks, exit_code = [], 0

    def add(check, code=0):
        nonlocal exit_code
        checks.append(check)
        if check["status"] == FAIL and not exit_code:
            exit_code = code or EXIT_CONNECTION
        return check["status"] != FAIL

    def skip(*names):
        for name in names:
            checks.append(_check(name, SKIP, "skipped: an earlier check failed"))

    # Connection and TLS are still checked after a config failure: they need no
    # token, and knowing the server is up narrows down what is wrong.
    config, config_code, has_token = _config(client)
    add(config, config_code)

    connect, sock = _connect(client)
    if not add(connect, EXIT_CONNECTION):
        skip("tls", "auth", "version", "features", "telemetry")
    elif not add(_tls(client, sock), EXIT_CONNECTION):
        skip("auth", "version", "features", "telemetry")
    elif not has_token:
        skip("auth", "version", "features", "telemetry")
    else:
        auth, caps, auth_code = _auth(client)
        if not add(auth, auth_code or EXIT_AUTH):
            skip("version", "features", "telemetry")
        else:
            add(_version(caps))
            add(_features(caps))
            add(_telemetry(client))

    failed = [c for c in checks if c["status"] == FAIL]
    report = Report(
        ok=not failed,
        summary=(f"{failed[0]['name']} failed: {failed[0]['detail']}" if failed
                 else "SmarTune is reachable and the token works."),
        checks=checks,
    )
    report.exit_code = exit_code
    return report
