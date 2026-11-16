# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Proxy settings in the environment must not break a local SmarTune.

requests routes every URL that NO_PROXY does not cover through HTTPS_PROXY, and
a proxy cannot reach this machine's loopback address. The request then failed as
"cannot connect" while `smartune doctor`'s socket-level connect and tls checks
passed, which pointed at a stopped service instead of the proxy.
"""

import pytest
import requests

from smartune_sdk.client import SmartuneClient, SmartuneError, is_loopback_url, EXIT_CONNECTION


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:9001", "https://127.1.2.3:9001", "https://localhost:9001",
    "https://LOCALHOST", "https://[::1]:9001", "https://api.localhost:9001",
])
def test_loopback_urls(url):
    assert is_loopback_url(url)


@pytest.mark.parametrize("url", [
    "https://192.168.1.20:9001", "https://smartune-host:9001", "https://10.0.0.1",
    "https://localhost.example.com", "not a url",
])
def test_other_urls(url):
    assert not is_loopback_url(url)


@pytest.fixture
def proxy_env(monkeypatch):
    """A proxy for HTTPS and no NO_PROXY, whatever the machine running the tests has set."""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:912")


def test_local_server_ignores_proxy_variables(proxy_env):
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t")
    assert client.session.trust_env is False
    settings = client.session.merge_environment_settings(client.url, {}, None, True, None)
    assert not settings["proxies"]


def test_remote_server_keeps_proxy_variables(proxy_env):
    client = SmartuneClient(url="https://smartune-host:9001", token="t")
    assert client.session.trust_env is True
    settings = client.session.merge_environment_settings(client.url, {}, None, True, None)
    assert settings["proxies"].get("https") == "http://proxy.example:912"


def test_injected_session_is_left_alone():
    session = requests.Session()
    SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    assert session.trust_env is True


class ProxyFailingSession:
    def request(self, *args, **kwargs):
        raise requests.exceptions.ProxyError("Unable to connect to proxy")


def test_proxy_failure_names_the_proxy_and_no_proxy():
    client = SmartuneClient(url="https://smartune-host:9001", token="t", session=ProxyFailingSession())
    with pytest.raises(SmartuneError) as info:
        client.capabilities()
    assert "proxy" in info.value.message
    assert "NO_PROXY" in info.value.hint and "smartune-host" in info.value.hint
    assert info.value.exit_code == EXIT_CONNECTION
