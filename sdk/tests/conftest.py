# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import pytest


@pytest.fixture(autouse=True)
def isolated_client_config(tmp_path, monkeypatch):
    """Keep tests away from the real ~/.config/smartune (token and certificate
    copied by skills/install.sh), which the client otherwise picks up by default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return tmp_path / "xdg-config" / "smartune"
