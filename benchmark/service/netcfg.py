# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Network settings for the benchmark toolchain: HuggingFace token / endpoint and
# the proxy that model downloads and environment builds go through.
#
# Every value is resolved from four layers, first non-empty wins:
#
#   1. settings  key/bench_network.json, written from the dashboard's Settings page.
#                Root-only (0600): it holds the HF token and, often, proxy
#                credentials. Not in git, and dpkg does not track key/, so it
#                survives package upgrades and is dropped only by `apt purge`.
#   2. config    the `benchmark:` block of config/config.yaml (hf_token,
#                hf_endpoint, http_proxy, https_proxy, no_proxy): the entry point
#                where there is no dashboard. Read when the service starts.
#   3. env       the service process's own environment: an `export` in the shell
#                that ran start_smartune.sh (it starts Python with `sudo -E`), or
#                /etc/default/intel-smartune for the systemd unit.
#   4. none      direct connection / anonymous hub access.
#
# https_proxy is the one coupled field: it follows http_proxy unless it is set at
# the same layer or a higher one, so a proxy entered in Settings is not silently
# paired with a stale https_proxy inherited from the environment.
#
# Benchmark subprocesses never read the file: build_subprocess_env() hands them
# the resolved values as environment variables.

import contextlib
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from config.config import b_config
from utils.logger import get_logger
logger = get_logger(__name__)

# <repo>/benchmark/service/netcfg.py -> <repo>; the same key/ dir smartune_api.py uses.
KEY_DIR = Path(__file__).resolve().parents[2] / "key"
STORE_FILE = KEY_DIR / "bench_network.json"

FIELDS = ("hf_token", "hf_endpoint", "http_proxy", "https_proxy", "no_proxy")
PROXY_FIELDS = ("http_proxy", "https_proxy")
# URLs that may carry user:password@ and are masked wherever they are shown.
SECRET_URL_FIELDS = ("hf_endpoint",) + PROXY_FIELDS

# Environment spellings read for layer 2, in lookup order.
_ENV_NAMES = {
    "hf_token": ("HF_TOKEN",),
    "hf_endpoint": ("HF_ENDPOINT",),
    "http_proxy": ("http_proxy", "HTTP_PROXY"),
    "https_proxy": ("https_proxy", "HTTPS_PROXY"),
    "no_proxy": ("no_proxy", "NO_PROXY"),
}

# Lower rank = higher priority.
_RANK = {"settings": 0, "config": 1, "env": 2, "none": 3}

DEFAULT_HF_ENDPOINT = "https://huggingface.co"
_MAX_VALUE_LEN = 2048
# No socks5://: urllib (the model list, the connection test) sends it HTTP CONNECT
# and wget cannot use it, so it would pass validation and then fail everywhere.
_PROXY_SCHEMES = ("http", "https")
_NO_PROXY_RE = re.compile(r"^[A-Za-z0-9.*:\[\]/_-]+$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")   # printable ASCII, no spaces

_lock = threading.Lock()


# --- store ----------------------------------------------------------------
# {"updated_at": <unix s, 0 = never saved>, "direct": bool, "values": {field: str}}

def _clean_values(raw) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v.strip() for k, v in raw.items()
            if k in FIELDS and isinstance(v, str) and v.strip()}


def read_store() -> dict:
    """The Settings layer. A missing or unreadable file is an empty layer."""
    try:
        with open(STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        logger.warning(f"Ignoring unreadable {STORE_FILE}: {exc}")
        data = {}
    if not isinstance(data, dict):
        data = {}
    updated_at = data.get("updated_at")
    return {
        "updated_at": updated_at if isinstance(updated_at, int) else 0,
        "direct": data.get("direct") is True,
        "values": _clean_values(data.get("values")),
    }


def _write_store(store: dict) -> None:
    """Atomically replace the store; created 0600, so never readable by others."""
    KEY_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = STORE_FILE.with_name(STORE_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STORE_FILE)


# --- config.yaml layer ----------------------------------------------------

# (field, value) pairs already warned about: a bad value is logged once, not on
# every job that resolves the settings.
_config_warned: set = set()


def config_values() -> Dict[str, str]:
    """The config.yaml layer: the network keys of its `benchmark:` block.

    Checked like a value saved from the Settings page. An invalid one is logged
    and skipped (the next layer shows through) rather than handed to every job.
    """
    cfg = getattr(b_config, "benchmark", None)
    if not isinstance(cfg, dict):
        return {}
    out: Dict[str, str] = {}
    for field in FIELDS:
        raw = cfg.get(field)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            value = _normalize(field, raw)
        except ValueError as exc:
            key = (field, str(raw))
            if key not in _config_warned:
                _config_warned.add(key)
                logger.warning(f"Ignoring benchmark.{field} in config.yaml: {exc}")
            continue
        if value:
            out[field] = value
    return out


# --- resolution -----------------------------------------------------------

def _env_value(field: str, environ) -> str:
    for name in _ENV_NAMES[field]:
        value = str(environ.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve(store: Optional[dict] = None, environ=None,
            config: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """Effective value and source of every field: {field: {"value", "source", ...}}.

    ``store`` overrides the Settings layer (used to test a draft before saving);
    ``config`` and ``environ`` override the config.yaml layer and the process
    environment (tests).
    """
    store = read_store() if store is None else store
    config = config_values() if config is None else config
    environ = os.environ if environ is None else environ
    values = store.get("values") or {}

    out: Dict[str, dict] = {}
    for field in FIELDS:
        if values.get(field):
            out[field] = {"value": values[field], "source": "settings"}
        elif config.get(field):
            out[field] = {"value": config[field], "source": "config"}
        elif _env_value(field, environ):
            out[field] = {"value": _env_value(field, environ), "source": "env"}
        else:
            out[field] = {"value": "", "source": "none"}

    if not out["hf_endpoint"]["value"]:
        out["hf_endpoint"] = {"value": DEFAULT_HF_ENDPOINT, "source": "none"}

    http, https = out["http_proxy"], out["https_proxy"]
    if http["value"] and _RANK[https["source"]] > _RANK[http["source"]]:
        out["https_proxy"] = {"value": http["value"], "source": http["source"],
                              "follows": "http_proxy"}

    if store.get("direct"):
        for field in PROXY_FIELDS:
            out[field] = {"value": "", "source": "settings", "direct": True}
    return out


def apply_to_env(env: Dict[str, str], resolved: Optional[Dict[str, dict]] = None) -> None:
    """Write the resolved values into a subprocess environment, in place.

    Each variable is set or removed in both spellings, so a value inherited from
    the service's own environment can never sneak past a different resolved one.
    """
    resolved = resolve() if resolved is None else resolved

    token = resolved["hf_token"]["value"]
    if token:
        env["HF_TOKEN"] = token
    else:
        env.pop("HF_TOKEN", None)
    env["HF_ENDPOINT"] = resolved["hf_endpoint"]["value"]

    for field in ("http_proxy", "https_proxy", "no_proxy"):
        value = resolved[field]["value"]
        for name in (field, field.upper()):
            if value:
                env[name] = value
            else:
                env.pop(name, None)


# --- masking --------------------------------------------------------------

def mask_url(value: str) -> str:
    """Hide the password in ``scheme://user:pass@host`` (the user name stays)."""
    if not value or "@" not in value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return "***"
    if parts.password is None:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username or ''}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def mask_token(value: str) -> Optional[str]:
    """A recognisable hint of a token, never enough to use it."""
    if not value:
        return None
    if len(value) < 12:
        return "****"
    return f"{value[:3]}…{value[-4:]}"


def _public(field: str, value: str) -> str:
    return mask_url(value) if field in SECRET_URL_FIELDS else value


def view(store: Optional[dict] = None) -> dict:
    """What GET /bench/network returns. The token itself is never included."""
    store = read_store() if store is None else store
    values = store.get("values") or {}
    resolved = resolve(store)

    effective = {}
    for field, entry in resolved.items():
        item = dict(entry)
        if field == "hf_token":
            item = {"set": bool(entry["value"]), "source": entry["source"],
                    "preview": mask_token(entry["value"])}
        else:
            item["value"] = _public(field, entry["value"])
        effective[field] = item

    return {
        "settings": {
            "hf_endpoint": mask_url(values.get("hf_endpoint") or ""),
            "http_proxy": mask_url(values.get("http_proxy") or ""),
            "https_proxy": mask_url(values.get("https_proxy") or ""),
            "no_proxy": values.get("no_proxy") or "",
            "direct": bool(store.get("direct")),
            "hf_token_set": bool(values.get("hf_token")),
            "hf_token_preview": mask_token(values.get("hf_token") or ""),
        },
        "effective": effective,
        "store_file": str(STORE_FILE),
        "updated_at": int(store.get("updated_at") or 0),
    }


# --- validation / update --------------------------------------------------

def _check_text(field: str, value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > _MAX_VALUE_LEN:
        raise ValueError(f"{field} is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


_ENCODE_HINT = ("A '#', '/', '?' or '@' in the user name or password must be "
                "percent-encoded: %23, %2F, %3F, %40.")


def _check_url(field: str, value: str, schemes: Tuple[str, ...], *, path_ok: bool) -> str:
    """A URL with an allowed scheme and a host, and nothing that would split it wrong.

    An unencoded '#', '/' or '?' in a password ends the address early: the URL
    then parses with the user name as its host, the rest as a path or fragment --
    and with no password found, the masking that keeps it off the page misses it.
    """
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise ValueError(f"{field} is not a valid URL. {_ENCODE_HINT}")
    if parts.scheme not in schemes or not parts.hostname:
        raise ValueError(f"{field} must look like {schemes[0]}://host[:port]"
                         f" (allowed schemes: {', '.join(schemes)})")
    if parts.query or parts.fragment or (not path_ok and parts.path not in ("", "/")):
        raise ValueError(f"{field} must be {parts.scheme}://[user:password@]host[:port]"
                         f"{'[/path]' if path_ok else ''} with nothing else. {_ENCODE_HINT}")
    return value


def _normalize(field: str, value) -> str:
    """One field checked and normalised as it is stored. Raises ValueError.

    The one rule set for every field, wherever it comes from: the Settings page
    (build_store) and config.yaml (config_values).
    """
    value = _check_text(field, value)
    if not value:
        return ""
    if field == "hf_endpoint":
        return _check_url(field, value.rstrip("/"), ("https", "http"), path_ok=True)
    if field in PROXY_FIELDS:
        return _check_url(field, value, _PROXY_SCHEMES, path_ok=False)
    if field == "no_proxy":
        items = [item.strip() for item in value.split(",") if item.strip()]
        for item in items:
            if not _NO_PROXY_RE.match(item):
                raise ValueError(f"no_proxy entry {item!r} is not a host name, domain or address")
        return ",".join(items)
    if not _TOKEN_RE.match(value) or len(value) > 512:
        raise ValueError("hf_token must be printable ASCII without spaces")
    return value


def build_store(body: dict, current: Optional[dict] = None) -> dict:
    """Validate a POST body into the next store. Raises ValueError on bad input.

    The non-token fields are replaced wholesale (empty = inherit from
    config.yaml or the service environment). ``hf_token`` is write-only: {"action": "keep"|"set"|"clear",
    "value": "..."}. A proxy submitted in its masked form keeps the stored password.
    """
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    current = read_store() if current is None else current
    old = current.get("values") or {}
    new: Dict[str, str] = {}

    for field in SECRET_URL_FIELDS + ("no_proxy",):
        value = _normalize(field, body.get(field))
        # The form shows the URLs masked; sent back unchanged, the stored password
        # stays. (A masked URL is itself valid, so it passes _normalize as is.)
        if field in SECRET_URL_FIELDS and value and old.get(field) \
                and value == mask_url(old[field]):
            value = old[field]
        if value:
            new[field] = value

    token_spec = body.get("hf_token") or {"action": "keep"}
    if not isinstance(token_spec, dict):
        raise ValueError("hf_token must be an object: {action, value}")
    action = token_spec.get("action", "keep")
    if action == "keep":
        if old.get("hf_token"):
            new["hf_token"] = old["hf_token"]
    elif action == "set":
        token = _normalize("hf_token", token_spec.get("value"))
        if not token:
            raise ValueError("hf_token value is empty; use action 'clear' to remove it")
        new["hf_token"] = token
    elif action != "clear":
        raise ValueError("hf_token action must be keep, set or clear")

    direct = body.get("direct", False)
    if not isinstance(direct, bool):
        raise ValueError("direct must be a boolean")

    return {"updated_at": current.get("updated_at", 0), "direct": direct, "values": new}


def save(body: dict, expected_updated_at) -> Tuple[str, dict]:
    """Validate and persist. Returns ("ok" | "conflict", view).

    Optimistic concurrency like the /monitor/config/<section> routes: the caller
    sends the updated_at it loaded, and a mismatch means another client saved.
    """
    with _lock:
        current = read_store()
        # Omitted = "I loaded a never-saved store", as in _check_config_conflict.
        try:
            expected = int(expected_updated_at or 0)
        except (TypeError, ValueError):
            raise ValueError("expected_updated_at must be an integer")
        if expected != current["updated_at"]:
            return "conflict", view(current)
        store = build_store(body, current)
        store["updated_at"] = max(int(time.time()), current["updated_at"] + 1)
        _write_store(store)
    old, new = current.get("values") or {}, store["values"]
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    if store["direct"] != bool(current.get("direct")):
        changed.append("direct")
    # Field names only: values may be secrets.
    logger.info(f"Benchmark network settings saved to {STORE_FILE}; changed: {changed or 'nothing'}")
    return "ok", view(store)


# --- connection test ------------------------------------------------------

_TEST_TIMEOUT_SEC = 10
_TEST_DEADLINE_SEC = 15
PYPI_PROBE_URL = "https://pypi.org/simple/pip/"


def _opener_for(url: str, resolved: Dict[str, dict]):
    host = urlsplit(url).hostname or ""
    no_proxy = resolved["no_proxy"]["value"]
    proxies = {}
    if not (no_proxy and urllib.request.proxy_bypass_environment(host, {"no": no_proxy})):
        for scheme in ("http", "https"):
            value = resolved[f"{scheme}_proxy"]["value"]
            if value:
                proxies[scheme] = value
    # An explicit (possibly empty) ProxyHandler: without one urllib falls back to
    # the service's own environment, which is exactly what is being overridden.
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies)), proxies


def _describe_error(exc: BaseException) -> Tuple[str, str]:
    """(kind, human message) for a failed probe."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 407:
            return "proxy_auth", "The proxy requires authentication (HTTP 407)."
        if exc.code in (401, 403):
            return "auth", f"Rejected by the server (HTTP {exc.code})."
        return "http", f"HTTP {exc.code} {exc.reason}"
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    if isinstance(reason, socket.gaierror) or "Name or service not known" in text \
            or "Temporary failure in name resolution" in text:
        return "dns", f"Host name could not be resolved ({text}). A proxy is usually needed here."
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "timeout", "Timed out. The host or proxy is unreachable from this machine."
    if isinstance(reason, ConnectionRefusedError) or "refused" in text.lower():
        return "refused", f"Connection refused ({text}). Check the proxy host and port."
    if "Network is unreachable" in text or "No route to host" in text:
        return "unreachable", "No route to the host. This network probably needs a proxy."
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return "tls", (f"Certificate not trusted ({text}). A TLS-inspecting proxy needs its "
                       "CA installed on this machine.")
    if isinstance(reason, ssl.SSLError) or "EOF occurred" in text:
        return "tls", (f"The connection was cut during the TLS handshake ({text}). "
                       "The proxy may be blocking this host.")
    if "Tunnel connection failed" in text:
        return "proxy", f"The proxy refused the tunnel ({text})."
    return "error", text


def _via(url: str, resolved: Dict[str, dict]) -> str:
    _opener, proxies = _opener_for(url, resolved)
    return mask_url(proxies.get(urlsplit(url).scheme, "")) or "direct"


def _probe(name: str, url: str, resolved: Dict[str, dict], headers=None) -> dict:
    opener, _proxies = _opener_for(url, resolved)
    started = time.monotonic()
    result = {"name": name, "url": mask_url(url), "via": _via(url, resolved)}
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with opener.open(request, timeout=_TEST_TIMEOUT_SEC) as response:
            result.update(ok=True, status=response.status)
            if headers and "Authorization" in headers:
                try:
                    payload = json.loads(response.read(64 * 1024).decode("utf-8"))
                    result["user"] = payload.get("name")
                except ValueError:
                    pass
    except Exception as exc:  # every failure is an answer here, not an error
        kind, message = _describe_error(exc)
        result.update(ok=False, kind=kind, detail=message,
                      status=getattr(exc, "code", None))
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _probe_proxy_tls(name: str, proxy: str) -> dict:
    """Does an ``https://`` proxy really speak TLS?

    The scheme of a proxy address is the protocol spoken *to the proxy*: for
    https:// uv, pip, curl and huggingface_hub open TLS to the proxy itself.
    Python's urllib -- which the other probes here use -- ignores it for HTTPS
    targets and talks plain HTTP CONNECT, so a plain proxy entered as https://
    passes them while every real tool fails with "tls handshake eof". This is
    the one probe that answers the way those tools will.
    """
    parts = urlsplit(proxy)
    host, port = parts.hostname or "", parts.port or 443
    masked = mask_url(proxy)
    plain = mask_url(urlunsplit(("http",) + tuple(parts)[1:]))
    result = {"name": name, "url": masked}
    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=_TEST_TIMEOUT_SEC)
    except OSError as exc:
        kind, message = _describe_error(exc)
        result.update(ok=False, kind=kind, detail=f"{masked}: {message}")
    else:
        try:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                result.update(ok=True)
        except ssl.SSLCertVerificationError as exc:
            result.update(ok=False, kind="tls", detail=(
                f"{masked} speaks TLS, but its certificate is not trusted ({exc.verify_message}). "
                f"Install its CA on this machine, or use {plain} if it also takes plain connections."))
        except OSError as exc:  # ssl.SSLError, EOF, reset, or no TLS answer at all
            result.update(ok=False, kind="proxy_scheme", detail=(
                f"{masked} does not speak TLS ({exc.__class__.__name__}), yet an https:// "
                f"proxy address makes uv, pip, curl and model downloads use TLS to reach "
                f"it, so they will fail (the checks below do not, so they may still pass). "
                f"Most proxies take http:// even for HTTPS traffic: use {plain}."))
        finally:
            sock.close()
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _tls_proxies(resolved: Dict[str, dict]) -> list:
    """The distinct https:// proxy addresses in force, as (check name, url)."""
    seen = []
    for field in PROXY_FIELDS:
        value = resolved[field]["value"]
        if value and urlsplit(value).scheme == "https" and value not in seen:
            seen.append(value)
    return [("proxy_tls" if i == 0 else f"proxy_tls_{i + 1}", value)
            for i, value in enumerate(seen)]


class TestBusy(RuntimeError):
    """A connection test (or a probe left over from one) is still running."""


# Probe threads of the last test. A probe stuck past the deadline keeps its
# thread, so a new test waits until they are gone rather than piling up more.
_test_threads: list = []
_test_running = False
_test_lock = threading.Lock()


@contextlib.contextmanager
def _test_slot():
    global _test_running
    with _test_lock:
        if _test_running or any(t.is_alive() for t in _test_threads):
            raise TestBusy("A connection test is still running; try again in a few seconds.")
        _test_running = True
        _test_threads.clear()
    try:
        yield
    finally:
        with _test_lock:
            _test_running = False


def _token_route(resolved: Dict[str, dict], endpoint: str) -> Tuple[str, str]:
    """Where a token sent to ``endpoint`` goes: (endpoint, proxy it passes through)."""
    url = f"{endpoint}/api/whoami-v2"
    return endpoint, _opener_for(url, resolved)[1].get(urlsplit(url).scheme, "")


def check_connection(body: Optional[dict] = None) -> dict:
    """Probe PyPI and the HF hub with the given draft (or the saved settings).

    Runs in the service process, not as a subprocess: it needs no tooling, and
    the resolved values are exactly what a subprocess would be handed.

    A token already in force is only sent the way jobs already send it. A draft
    that points it elsewhere -- another endpoint, or a different proxy for a
    plain http:// one -- tests the token only if the token is typed into the
    draft too; otherwise a test, which saves nothing, would hand the stored
    token to any address put in the form.
    """
    current = read_store()
    store = build_store(body, current) if body else current
    resolved = resolve(store)
    endpoint = resolved["hf_endpoint"]["value"].rstrip("/")
    token = resolved["hf_token"]["value"]

    token_typed = (isinstance(body, dict) and isinstance(body.get("hf_token"), dict)
                   and body["hf_token"].get("action") == "set")
    withheld = None
    if token and not token_typed:
        saved = resolve(current)
        saved_endpoint = saved["hf_endpoint"]["value"].rstrip("/")
        route, saved_route = _token_route(resolved, endpoint), _token_route(saved, saved_endpoint)
        moved = route[0] != saved_route[0] or (endpoint.startswith("http://") and route != saved_route)
        if moved:
            token = ""
            withheld = {"name": "hf_token", "ok": None, "kind": None, "detail": (
                "Not checked: the saved token is only sent to the saved endpoint. "
                "Type the token again to test it against this one.")}

    tasks = []
    for name, proxy in _tls_proxies(resolved):
        tasks.append((name, mask_url(proxy), None,
                      lambda n=name, p=proxy: _probe_proxy_tls(n, p)))
    probes = [("pypi", PYPI_PROBE_URL, None),
              ("hf_endpoint", f"{endpoint}/api/models?limit=1", None)]
    if token:
        probes.append(("hf_token", f"{endpoint}/api/whoami-v2",
                       {"Authorization": f"Bearer {token}"}))
    for name, url, headers in probes:
        tasks.append((name, mask_url(url), _via(url, resolved),
                      lambda n=name, u=url, h=headers: _probe(n, u, resolved, headers=h)))
    with _test_slot():
        checks = _run_parallel(tasks)
    if withheld:
        checks.append(withheld)

    by_name = {c["name"]: c for c in checks}
    auth = by_name.get("hf_token")
    if auth is not None and not auth.get("ok"):
        if not by_name["hf_endpoint"].get("ok"):
            auth.update(ok=None, kind=None,
                        detail="Not checked: the HuggingFace endpoint is unreachable.")
        elif auth.get("kind") == "auth":
            auth["detail"] = "The HuggingFace token was rejected (invalid or expired)."
    return {"ok": all(c.get("ok") is not False for c in checks), "checks": checks}


def _run_parallel(tasks) -> list:
    """Run ``(name, shown_url, via, fn)`` tasks concurrently under one deadline.

    The socket timeout bounds each connection attempt, not the request: a host
    with several addresses on a network with no route out takes a timeout per
    address. A daemon thread per task keeps the whole test bounded regardless.
    """
    results = [None] * len(tasks)

    def work(i, fn):
        results[i] = fn()

    threads = [threading.Thread(target=work, args=(i, task[3]), daemon=True,
                                name=f"bench-net-test-{task[0]}")
               for i, task in enumerate(tasks)]
    with _test_lock:
        _test_threads.extend(threads)
    for t in threads:
        t.start()
    deadline = time.monotonic() + _TEST_DEADLINE_SEC
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    for i, (name, url, via, _fn) in enumerate(tasks):
        if results[i] is None:
            results[i] = {"name": name, "url": url, "ok": False, "kind": "timeout",
                          "detail": f"No answer within {_TEST_DEADLINE_SEC}s. The host or "
                                    "proxy is unreachable from this machine.",
                          "elapsed_ms": _TEST_DEADLINE_SEC * 1000}
            if via is not None:
                results[i]["via"] = via
    return results
