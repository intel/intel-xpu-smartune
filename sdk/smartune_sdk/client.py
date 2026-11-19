# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# HTTP client for the SmarTune REST API, shared by the CLI, the agent skills and
# (later) the MCP server. Owns the parts every caller would otherwise repeat:
# endpoint discovery from the environment, the X-Auth-Token header, TLS
# verification against the server's self-signed certificate, unwrapping the
# {retcode, retmsg, data} envelope, and turning failures into errors that carry
# an actionable hint.
#
# Every ordinary request must match READ_ROUTES, so a query can never reach a
# write endpoint even by accident. The balancer control endpoints are a
# separate allow-list, CONTROL_ROUTES, reachable only through control(), which
# the CLI calls only after a dry-run plan and an explicit --yes (cli.py,
# `smartune control`). Destructive endpoints (kill, suspend, shutdown, OOM
# score, configuration) are in neither list.

import hashlib
import ipaddress
import os
import re
import ssl
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

DEFAULT_URL = "https://127.0.0.1:9001"
DEFAULT_TIMEOUT = 20
# A manual limit reclaims memory before it returns, which can take many seconds.
CONTROL_TIMEOUT = 90

# RetCode values from utils/http_utils.py that callers branch on.
RETCODE_SUCCESS = 0
RETCODE_NOT_EXISTING = 404

# Exit codes, so a shell caller can tell failure classes apart.
EXIT_USAGE = 2
EXIT_CONNECTION = 3
EXIT_AUTH = 4
EXIT_API = 5
EXIT_UNAVAILABLE = 6
EXIT_REFUSED = 7  # a control action cannot be applied in the app's current state

# The server's self-signed certificate names only localhost and 127.0.0.1
# (start_smartune.sh, packaging/deb/app/run_*.sh), so any other address fails the
# hostname check even with SMARTUNE_CACERT set correctly.
# Pinning (pinned_fingerprint) avoids this for the server's own certificate; the
# hint applies when pinning is off or SMARTUNE_CACERT is not that certificate.
# How hints name the per-user setup script: the deb puts it on PATH, the source code has it in skills/.
INSTALL_CMD = "smartune-agent-install (skills/install.sh when run from the source code)"

ADDRESS_MISMATCH_HINT = (
    "The SmarTune certificate only covers localhost and 127.0.0.1. Set SMARTUNE_CACERT to the "
    "server's own key/b_server.crt (the installer's --remote option copies it), which pins it and accepts "
    "any address, and leave SMARTUNE_PIN_CERT unset. Do not disable certificate verification.")

# REST surface versions (/smartune/capabilities -> api_level) this SDK was built
# against. Raise it together with API_LEVEL in smartune_api.py.
SUPPORTED_API_LEVELS = (1,)

# Every route the read-only client may call, as (method, path regex). POSTs are
# listed only where the server uses POST for what is really a query.
READ_ROUTES = (
    ("GET", r"/smartune/capabilities"),
    # /monitor/dynamic_info without ?sections= starts the full collector and
    # persists a history row; the client always sends sections (see get()).
    ("GET", r"/monitor/dynamic_info"),
    ("GET", r"/monitor/static_info(/[a-z]+)?"),
    ("GET", r"/monitor/app_resource_stats"),
    ("GET", r"/monitor/app_disk_io_stats"),
    ("GET", r"/monitor/processes"),
    ("GET", r"/monitor/process_detail"),
    ("GET", r"/monitor/history"),
    ("POST", r"/app/get_controlled_app"),
    ("POST", r"/app/get_pending_app"),
    ("POST", r"/app/get_priority_data"),
    ("POST", r"/app/limit_snapshot"),
    ("POST", r"/app/auto_limited_apps"),
    # Queries behind `smartune control` plans: limit defaults and bounds, and
    # the apps excluded from auto-limiting. Neither changes anything.
    ("POST", r"/app/resource_limit_profile"),
    ("POST", r"/app/auto_limit_exclusions"),
    # The registration wizard's discovery steps: they only scan /proc.
    ("POST", r"/app/discover_search"),
    ("POST", r"/app/discover_extract"),
    # Device frequency ranges, behind `smartune control freq-status` and the
    # freq-limit / freq-restore plans. Only reads sysfs.
    ("GET", r"/device/freq/status"),
    ("GET", r"/diag/event-catalog"),
    ("GET", r"/diag/events"),
    ("GET", r"/diag/alerts"),
    ("GET", r"/diag/control-lifecycles"),
    ("GET", r"/diag/retention"),
    ("GET", r"/diag/logs"),
    ("GET", r"/diag/boots"),
    ("GET", r"/diag/context"),
    ("GET", r"/diag/monitor-metrics"),
    ("GET", r"/diag/digest"),
    # Not /bench/env: despite being a GET it bootstraps the benchmark venv.
    ("GET", r"/bench/run"),
    ("GET", r"/bench/run/[A-Za-z0-9][A-Za-z0-9._:-]*"),
    ("GET", r"/bench/preflight"),
    ("GET", r"/bench/quiet_mode"),
    ("GET", r"/bench/results"),
    ("GET", r"/bench/results/matrix"),
    ("GET", r"/bench/models"),
)
_READ_ROUTES = tuple((m, re.compile(p + r"\Z")) for m, p in READ_ROUTES)

# Balancer write endpoints `smartune control` may call (the Control rows of
# docs/reference/agent-api-exposure.md). Exact paths only. Each one is
# reversible from the CLI or the dashboard; the irreversible ones stay out (see
# the module comment).
# purge_controlled_app is here only as the undo of new_controlled_app: the CLI
# sends it solely for apps that `smartune control register` added (control.py).
# The two /device/freq writes act on a whole device rather than one app; the
# server clamps them to the hardware range, records the original range, and
# restores it when the service stops.
CONTROL_ROUTES = (
    ("POST", "/app/resource_limit"),
    ("POST", "/app/resource_restore"),
    ("POST", "/app/freeze"),
    ("POST", "/app/set_priority"),
    ("POST", "/app/set_network_priority"),
    ("POST", "/app/cpu_affinity"),
    ("POST", "/app/lock_to_manual"),
    ("POST", "/app/auto_limit_restore"),
    ("POST", "/app/auto_limit_exclusion_remove"),
    ("POST", "/app/cancel_relaunch"),
    ("POST", "/app/new_controlled_app"),
    ("POST", "/app/purge_controlled_app"),
    ("POST", "/device/freq/set"),
    ("POST", "/device/freq/restore"),
)


class SmartuneError(Exception):
    """A failed call, with a hint aimed at whoever (or whatever) reads it next."""

    def __init__(self, message, hint="", exit_code=EXIT_API, retcode=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.retcode = retcode

    def to_dict(self):
        out = {"error": self.message}
        if self.retcode is not None:
            out["retcode"] = self.retcode
        if self.hint:
            out["hint"] = self.hint
        return out


def is_read_route(method, path):
    return any(m == method and p.match(path) for m, p in _READ_ROUTES)


def is_control_route(method, path):
    return (method, path) in CONTROL_ROUTES


def config_dir():
    """Per-user client config: $XDG_CONFIG_HOME/smartune, else ~/.config/smartune.

    skills/install.sh copies the server's token and certificate here, so the CLI
    works without any SMARTUNE_* variable set.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "smartune")


def default_token_file():
    path = os.path.join(config_dir(), "api_token")
    return path if os.path.isfile(path) else None


def default_cacert():
    path = os.path.join(config_dir(), "b_server.crt")
    return path if os.path.isfile(path) else None


def default_url():
    """The server address saved by `skills/install.sh --remote`, if any."""
    try:
        with open(os.path.join(config_dir(), "url"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def pinned_fingerprint(cacert):
    """SHA-256 of the certificate to pin to, or None for ordinary CA verification.

    SmarTune serves a self-signed certificate that names only localhost and
    127.0.0.1, so from another machine the hostname check always fails. When
    SMARTUNE_CACERT is exactly one self-signed certificate -- the server's own
    key/b_server.crt -- the client instead requires the server to present that
    very certificate (by fingerprint) and skips only the hostname comparison.
    That is what a browser's "trust this certificate" exception amounts to, and
    it is no weaker than the hostname check here: only the holder of the
    server's private key can present it. A CA bundle, or SMARTUNE_PIN_CERT=0,
    keeps the usual CA + hostname verification.
    """
    if not cacert or os.environ.get("SMARTUNE_PIN_CERT", "").strip() == "0":
        return None
    try:
        with open(cacert, encoding="ascii", errors="replace") as fh:
            pem = fh.read()
        certs = ssl.create_default_context(cafile=cacert).get_ca_certs()
    except (OSError, ssl.SSLError, ValueError):
        return None
    if pem.count("-----BEGIN CERTIFICATE-----") != 1 or len(certs) != 1:
        return None
    if certs[0].get("subject") != certs[0].get("issuer"):
        return None
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----"):])).hexdigest()


def is_loopback_url(url):
    """Whether `url` points at this machine (localhost, 127.0.0.0/8, ::1)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class PinnedCertAdapter(HTTPAdapter):
    """Accept only the certificate with this fingerprint; urllib3 skips the
    hostname check when assert_fingerprint is set (see pinned_fingerprint)."""

    def __init__(self, fingerprint, **kwargs):
        self.fingerprint = fingerprint
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self.fingerprint
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["assert_fingerprint"] = self.fingerprint
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def token_source():
    """Where the token comes from: an env variable name, the default file path, or None."""
    if os.environ.get("SMARTUNE_TOKEN", "").strip():
        return "SMARTUNE_TOKEN"
    if os.environ.get("SMARTUNE_TOKEN_FILE", "").strip():
        return "SMARTUNE_TOKEN_FILE"
    return default_token_file()


def _read_token():
    token = os.environ.get("SMARTUNE_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get("SMARTUNE_TOKEN_FILE", "").strip() or default_token_file()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise SmartuneError(
                f"Cannot read token file {token_file}: {exc.strerror}",
                hint="Point SMARTUNE_TOKEN_FILE at a token file readable by this user, "
                     f"or run {INSTALL_CMD} to copy the server's token for this user.",
                exit_code=EXIT_AUTH,
            ) from None
    return ""


class SmartuneClient:
    """Thin wrapper over the SmarTune REST API. Methods return the `data` field."""

    def __init__(self, url=None, token=None, cacert=None, timeout=None,
                 agent_id=None, session=None):
        self.url = (url or os.environ.get("SMARTUNE_URL") or default_url() or DEFAULT_URL).rstrip("/")
        # An unreadable token file is reported on the first request rather than
        # here, so `smartune doctor` can still run and point at it.
        self.token_error = None
        if token is not None:
            self.token = token
        else:
            try:
                self.token = _read_token()
            except SmartuneError as exc:
                self.token, self.token_error = "", exc
        self.cacert = cacert or os.environ.get("SMARTUNE_CACERT") or default_cacert()
        self.timeout = timeout or float(os.environ.get("SMARTUNE_TIMEOUT") or DEFAULT_TIMEOUT)
        self.agent_id = agent_id or os.environ.get("SMARTUNE_AGENT_ID") or "smartune-cli"
        self.session = session or requests.Session()
        if session is None and is_loopback_url(self.url):
            # requests sends every URL not covered by NO_PROXY through HTTPS_PROXY /
            # ALL_PROXY, and a proxy cannot reach this machine's loopback address: the
            # request fails as "cannot connect" while a plain socket (smartune doctor's
            # connect and tls checks) gets through. A local SmarTune is never proxied.
            # The certificate is passed explicitly, so no other environment setting is lost.
            self.session.trust_env = False
        self.pinned_fingerprint = pinned_fingerprint(self.cacert)
        if self.pinned_fingerprint and session is None:
            self.session.mount("https://", PinnedCertAdapter(self.pinned_fingerprint))

    # -- transport -----------------------------------------------------------

    def _request(self, method, path, params=None, body=None, allow_retcodes=(), write=False):
        if not self.token:
            if self.token_error is not None:
                raise self.token_error
            raise SmartuneError(
                "No API token configured",
                hint=f"Run {INSTALL_CMD} once to copy the server's token for this user, or set "
                     "SMARTUNE_TOKEN_FILE (or SMARTUNE_TOKEN) in the environment that runs this command.",
                exit_code=EXIT_AUTH,
            )
        resp = self._send(method, path, params=params, body=body, write=write,
                          timeout=max(self.timeout, CONTROL_TIMEOUT) if write else self.timeout)
        return self._unwrap(resp, path, allow_retcodes, with_message=write)

    def _send(self, method, path, params=None, body=None, write=False, timeout=None):
        allowed = is_control_route(method, path) if write else is_read_route(method, path)
        if not allowed:
            raise SmartuneError(
                f"{method} {path} is not available to this client",
                hint="Queries are limited to read endpoints, and changes to the `smartune control` "
                     "actions; other endpoints are not exposed.",
                exit_code=EXIT_USAGE,
            )
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        headers = {"X-Auth-Token": self.token, "X-Agent-Id": self.agent_id}
        try:
            resp = self.session.request(
                method, self.url + path, params=params, json=body, headers=headers,
                timeout=timeout or self.timeout, verify=self.cacert or True,
            )
        except requests.exceptions.SSLError as exc:
            if "Fingerprints did not match" in str(exc):
                raise SmartuneError(
                    f"TLS verification failed for {self.url}: the server did not present the pinned certificate",
                    hint="The server certificate differs from SMARTUNE_CACERT (it may have been regenerated): "
                         f"copy key/b_server.crt again ({INSTALL_CMD}). If SMARTUNE_CACERT is a CA "
                         "certificate rather than the server's own, set SMARTUNE_PIN_CERT=0.",
                    exit_code=EXIT_CONNECTION,
                ) from None
            if "mismatch" in str(exc):  # "Hostname mismatch" / "IP address mismatch"
                raise SmartuneError(
                    f"TLS verification failed for {self.url}: the server certificate does not list this address",
                    hint=ADDRESS_MISMATCH_HINT, exit_code=EXIT_CONNECTION,
                ) from None
            raise SmartuneError(
                f"TLS verification failed for {self.url}",
                hint="Set SMARTUNE_CACERT to the server certificate (key/b_server.crt of the running instance); "
                     "copy it again if the server regenerated it.",
                exit_code=EXIT_CONNECTION,
            ) from None
        except requests.exceptions.Timeout:
            if write:
                raise SmartuneError(
                    f"No answer within {timeout or self.timeout:g}s from {path}; the change may or may not have been applied",
                    hint="Do not repeat it. Check the app's state with `smartune app <app_id>` and report that.",
                    exit_code=EXIT_CONNECTION,
                ) from None
            raise SmartuneError(
                f"Timed out after {self.timeout:g}s calling {path}",
                hint="Narrow the query (shorter time range, fewer rows) or raise SMARTUNE_TIMEOUT.",
                exit_code=EXIT_CONNECTION,
            ) from None
        except requests.exceptions.ProxyError:
            host = urlsplit(self.url).hostname
            raise SmartuneError(
                f"Cannot reach SmarTune at {self.url} through the proxy set in the environment (HTTPS_PROXY / ALL_PROXY)",
                hint=f"Add {host} to NO_PROXY and no_proxy so the CLI connects to it directly, for example "
                     f"export NO_PROXY=\"$NO_PROXY,{host}\" no_proxy=\"$no_proxy,{host}\" (for OpenClaw, also in "
                     "~/.openclaw/.env).",
                exit_code=EXIT_CONNECTION,
            ) from None
        except requests.exceptions.ConnectionError:
            raise SmartuneError(
                f"Cannot connect to SmarTune at {self.url}",
                hint="Check that the SmarTune service is running and SMARTUNE_URL is correct.",
                exit_code=EXIT_CONNECTION,
            ) from None
        return resp

    def _unwrap(self, resp, path, allow_retcodes=(), with_message=False):
        if resp.status_code == 401:
            raise SmartuneError(
                "SmarTune rejected the API token",
                hint="SMARTUNE_TOKEN does not match the server's key/api_token.",
                exit_code=EXIT_AUTH, retcode=401,
            )
        if resp.status_code == 404:
            raise SmartuneError(
                f"{path} is not served by this SmarTune instance",
                hint="The feature may not be installed; run `smartune caps` to see what is available.",
                exit_code=EXIT_UNAVAILABLE,
            )
        try:
            payload = resp.json()
        except ValueError:
            raise SmartuneError(
                f"Unexpected non-JSON response (HTTP {resp.status_code}) from {path}",
                exit_code=EXIT_API,
            ) from None

        retcode = payload.get("retcode", RETCODE_SUCCESS)
        if retcode != RETCODE_SUCCESS and retcode not in allow_retcodes:
            raise SmartuneError(
                payload.get("retmsg") or f"{path} failed",
                exit_code=EXIT_API, retcode=retcode,
            )
        if retcode != RETCODE_SUCCESS:
            return None
        if with_message:
            # Control endpoints report outcomes such as "Already frozen" or why a
            # limit was skipped only in retmsg.
            return {"data": payload.get("data"), "message": payload.get("retmsg")}
        return payload.get("data")

    def get(self, path, allow_retcodes=(), **params):
        return self._request("GET", path, params=params, allow_retcodes=allow_retcodes)

    def post(self, path, body=None, allow_retcodes=()):
        return self._request("POST", path, body=body or {}, allow_retcodes=allow_retcodes)

    # -- service -------------------------------------------------------------

    def capabilities(self):
        return self.get("/smartune/capabilities")

    # -- monitor -------------------------------------------------------------

    def dynamic_info(self, sections):
        # Always pass sections: an unfiltered call starts the full background
        # collector and writes a history row.
        return self.get("/monitor/dynamic_info", sections=",".join(sections))

    def static_info(self, section=None):
        path = "/monitor/static_info" + (f"/{section}" if section else "")
        return self.get(path)

    def app_resource_stats(self, n=10):
        return self.get("/monitor/app_resource_stats", n=n)

    def app_disk_io_stats(self, n=10):
        return self.get("/monitor/app_disk_io_stats", n=n)

    def processes(self):
        return self.get("/monitor/processes")

    def process_detail(self, pid):
        return self.get("/monitor/process_detail", pid=pid)

    def history(self, range_seconds, sections, limit):
        return self.get("/monitor/history", snapshot_type="dynamic",
                        range_seconds=range_seconds, sections=",".join(sections), limit=limit)

    # -- balancer (read-only queries) -----------------------------------------

    def controlled_apps(self):
        # The server answers NOT_EXISTING rather than an empty list when nothing is controlled.
        return self.post("/app/get_controlled_app", allow_retcodes=(RETCODE_NOT_EXISTING,)) or []

    def pending_apps(self):
        return self.post("/app/get_pending_app", allow_retcodes=(RETCODE_NOT_EXISTING,)) or []

    def priority_data(self, app_id=None, app_name=None):
        body = {"app_id": app_id} if app_id else {"app_name": app_name}
        return self.post("/app/get_priority_data", body, allow_retcodes=(RETCODE_NOT_EXISTING,))

    def limit_snapshot(self, app_id):
        return self.post("/app/limit_snapshot", {"app_id": app_id})

    def auto_limited_apps(self):
        return self.post("/app/auto_limited_apps")

    def resource_limit_profile(self, app_id, app_name, priority):
        return self.post("/app/resource_limit_profile",
                         {"app_id": app_id, "app_name": app_name, "priority": priority})

    def auto_limit_exclusions(self):
        return self.post("/app/auto_limit_exclusions") or []

    def discover_search(self, keywords):
        return self.post("/app/discover_search", {"keywords": list(keywords)}) or {}

    def discover_extract(self, pids, name=""):
        return self.post("/app/discover_extract", {"pids": list(pids), "name": name}) or {}

    def freq_status(self):
        """CPU policies and GPU gts: hardware range, current min/max, live clock, limited."""
        return self.get("/device/freq/status") or {}

    # -- balancer control (writes) ---------------------------------------------

    def control(self, path, body):
        """Call one CONTROL_ROUTES endpoint: {"data", "message"}.

        Only `smartune control ... --yes` calls this, after building and
        validating the plan it carries out.
        """
        return self._request("POST", path, body=body, write=True)

    # -- diagnostics ---------------------------------------------------------

    def diag(self, name, **params):
        return self.get(f"/diag/{name}", **params)

    # -- benchmark (read-only) -----------------------------------------------

    def bench(self, name, **params):
        return self.get(f"/bench/{name}", **params)
