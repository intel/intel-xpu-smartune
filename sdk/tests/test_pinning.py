# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Certificate pinning for remote access (client.pinned_fingerprint). The server's
# self-signed certificate names only localhost and 127.0.0.1; pinning to that one
# certificate lets other addresses work without disabling verification. These
# tests use real certificates made with openssl. The handshake itself (pinned
# access by another address succeeds, a different certificate is refused, even
# one issued by the pinned key) was checked against a live TLS server by hand.

import hashlib
import os
import shutil
import ssl
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartune_sdk import client as client_mod  # noqa: E402
from smartune_sdk.client import PinnedCertAdapter, SmartuneClient, pinned_fingerprint  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def make_cert(directory, name):
    """A certificate like the one start_smartune.sh generates."""
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert


def der_sha256(path):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text())).hexdigest()


def test_server_certificate_is_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv("SMARTUNE_PIN_CERT", raising=False)
    cert = make_cert(tmp_path, "b_server")
    assert pinned_fingerprint(str(cert)) == der_sha256(cert)


def test_pinning_can_be_turned_off(tmp_path, monkeypatch):
    cert = make_cert(tmp_path, "b_server")
    monkeypatch.setenv("SMARTUNE_PIN_CERT", "0")
    assert pinned_fingerprint(str(cert)) is None


def test_a_bundle_is_not_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv("SMARTUNE_PIN_CERT", raising=False)
    bundle = tmp_path / "bundle.pem"
    bundle.write_text(make_cert(tmp_path, "a").read_text() + make_cert(tmp_path, "b").read_text())
    assert pinned_fingerprint(str(bundle)) is None
    assert pinned_fingerprint(None) is None
    assert pinned_fingerprint(str(tmp_path / "missing.crt")) is None


def test_client_mounts_the_pinning_adapter(tmp_path, monkeypatch):
    monkeypatch.delenv("SMARTUNE_PIN_CERT", raising=False)
    cert = make_cert(tmp_path, "b_server")
    client = SmartuneClient(url="https://192.168.1.20:9001", token="t", cacert=str(cert))
    adapter = client.session.get_adapter("https://192.168.1.20:9001/smartune/capabilities")
    assert isinstance(adapter, PinnedCertAdapter) and adapter.fingerprint == der_sha256(cert)
    assert adapter.poolmanager.connection_pool_kw["assert_fingerprint"] == der_sha256(cert)


def test_saved_url_is_the_default(isolated_client_config, monkeypatch):
    monkeypatch.delenv("SMARTUNE_URL", raising=False)
    isolated_client_config.mkdir(parents=True)
    (isolated_client_config / "url").write_text("https://192.168.1.20:9001\n")
    assert SmartuneClient(token="t").url == "https://192.168.1.20:9001"
    monkeypatch.setenv("SMARTUNE_URL", "https://10.0.0.5:9001")
    assert SmartuneClient(token="t").url == "https://10.0.0.5:9001"


def test_no_saved_url_means_localhost(monkeypatch):
    monkeypatch.delenv("SMARTUNE_URL", raising=False)
    assert SmartuneClient(token="t").url == client_mod.DEFAULT_URL
