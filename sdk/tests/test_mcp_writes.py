# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# MCP write tools end to end: a real MCP client talks to the server in-process
# and answers the confirmation form through its elicitation callback, while the
# CLI underneath talks to a fake SmarTune (test_control's routes). Checks that
# nothing is written without an approved form, that the form carries the
# defaults and bounds, and that edited values are what gets applied.
# Every test runs on both protocol generations: the initialize handshake (a
# mid-call elicitation request) and 2026-07-28 (an InputRequiredResult round
# trip). Needs the optional MCP SDK 2.x; skipped without it.

import asyncio
import os
import sys

import pytest

pytest.importorskip("mcp.server.mcpserver")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import mcp.types as mt  # noqa: E402
from mcp import Client  # noqa: E402

from smartune_sdk import cli, mcp_server  # noqa: E402
from smartune_sdk.client import SmartuneClient  # noqa: E402
from test_cli import FakeSession  # noqa: E402
from test_control import APP, ok, register_routes, routes, writes  # noqa: E402


MODE = ["legacy"]


@pytest.fixture(autouse=True)
def _use_mode(mode):
    MODE[0] = mode


class Form:
    """An elicitation callback that records each form and answers it as told."""

    def __init__(self, action="accept", content=None):
        self.action, self.content, self.seen = action, content, []

    async def __call__(self, context, params):
        self.seen.append(params)
        if self.action != "accept":
            return mt.ElicitResult(action=self.action)
        schema = params.requested_schema
        answer = {k: v.get("default") for k, v in schema["properties"].items()}
        answer.update(self.content or {})
        return mt.ElicitResult(action="accept", content=answer)


@pytest.fixture(params=["legacy", "auto"], autouse=True)
def mode(request):
    return request.param


def call(monkeypatch, routes_, tool, args, form=None):
    protocol = MODE[0]
    session = FakeSession(routes_)
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=session)
    monkeypatch.setattr(mcp_server, "execute", lambda parsed: cli.execute(parsed, client=client))

    async def go():
        kwargs = {"elicitation_callback": form} if form else {}
        async with Client(mcp_server.build_server(allow_control=True), mode=protocol, **kwargs) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go()), session


def payload(result):
    import json
    return json.loads(result.content[0].text)


def test_no_form_support_means_no_change(monkeypatch):
    r = routes(None, {("POST", "/app/resource_limit"): ok()})
    result, session = call(monkeypatch, r, "limit_app", {"app_id": "a1"})
    assert result.is_error and "cannot show a confirmation form" in result.content[0].text
    assert not writes(session)


def test_limit_form_is_prefilled_and_applies_the_defaults(monkeypatch):
    form = Form()
    r = routes(None, {("POST", "/app/resource_limit"): ok("Successfully found and set resource limit")})
    result, session = call(monkeypatch, r, "limit_app", {"app_id": "a1"}, form)
    out = payload(result)
    assert out["applied"] is True
    (params,) = form.seen
    props = params.requested_schema["properties"]
    assert props["cpu_percent"]["default"] == 40.0 and props["cpu_percent"]["maximum"] == 50.0
    assert props["memory_percent"]["maximum"] == 30.0 and props["limit_disk"]["default"] is False
    assert "Limit ollama" in params.message and "Undo: smartune control restore a1" in params.message
    (call_,) = writes(session)
    assert call_["body"]["limit_overrides"]["cpu"] == {"enabled": True, "rate": 0.4}
    assert call_["body"]["limit_overrides"]["disk_io"]["enabled"] is False


def test_limit_applies_what_the_user_edited(monkeypatch):
    form = Form(content={"cpu_percent": 20, "limit_memory": False, "limit_disk": True, "disk_write_mb_s": 5})
    r = routes(None, {("POST", "/app/resource_limit"): ok()})
    result, session = call(monkeypatch, r, "limit_app", {"app_id": "a1", "cpu_percent": 30}, form)
    assert payload(result)["applied"] is True
    assert form.seen[0].requested_schema["properties"]["cpu_percent"]["default"] == 30.0  # the model's value
    body = writes(session)[0]["body"]["limit_overrides"]
    assert body["cpu"] == {"enabled": True, "rate": 0.2}
    assert body["memory"]["enabled"] is False
    assert body["disk_io"] == {"enabled": True,
                               "rate": {"read": 300, "write": 5, "read_iops": 90000, "write_iops": 60000}}


def test_limit_only_the_disk_starts_with_cpu_and_memory_off(monkeypatch):
    # "Limit only its disk IO": the form must not come prefilled with the CPU and memory defaults.
    form = Form()
    r = routes(None, {("POST", "/app/resource_limit"): ok()})
    args = {"app_id": "a1", "disk_read_mb_s": 100, "disk_write_mb_s": 50, "limit_cpu": False, "limit_memory": False}
    result, session = call(monkeypatch, r, "limit_app", args, form)
    assert payload(result)["applied"] is True
    props = form.seen[0].requested_schema["properties"]
    assert props["limit_cpu"]["default"] is False and props["limit_memory"]["default"] is False
    assert props["limit_disk"]["default"] is True and props["disk_write_mb_s"]["default"] == 50
    body = writes(session)[0]["body"]["limit_overrides"]
    assert body["cpu"]["enabled"] is False and body["memory"]["enabled"] is False
    assert body["disk_io"]["enabled"] is True and body["disk_io"]["rate"]["read"] == 100


def test_limit_disk_true_turns_disk_on_with_its_defaults(monkeypatch):
    form = Form()
    r = routes(None, {("POST", "/app/resource_limit"): ok()})
    result, session = call(monkeypatch, r, "limit_app", {"app_id": "a1", "limit_disk": True}, form)
    assert payload(result)["applied"] is True
    assert form.seen[0].requested_schema["properties"]["limit_disk"]["default"] is True
    body = writes(session)[0]["body"]["limit_overrides"]
    assert body["disk_io"]["enabled"] is True and body["cpu"]["enabled"] is True


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_declined_form_changes_nothing(monkeypatch, action):
    r = routes(None, {("POST", "/app/resource_limit"): ok()})
    result, session = call(monkeypatch, r, "limit_app", {"app_id": "a1"}, Form(action))
    out = payload(result)
    assert out["applied"] is False and out["user_response"] == action
    assert not writes(session)


def test_unticked_approval_changes_nothing(monkeypatch):
    manual = {**APP, "control_status": "MANUAL_LIMITED"}
    r = routes(manual, {("POST", "/app/resource_restore"): ok()})
    result, session = call(monkeypatch, r, "restore_app", {"app_id": "a1"}, Form(content={"approve": False}))
    assert payload(result)["applied"] is False and not writes(session)


def test_simple_change_applies_after_approval(monkeypatch):
    manual = {**APP, "control_status": "MANUAL_LIMITED"}
    r = routes(manual, {("POST", "/app/resource_restore"): ok("Restored")})
    form = Form()
    result, session = call(monkeypatch, r, "restore_app", {"app_id": "a1"}, form)
    assert payload(result)["applied"] is True
    assert list(form.seen[0].requested_schema["properties"]) == ["approve"]
    assert [c["path"] for c in writes(session)] == ["/app/resource_restore"]


def test_blocked_plan_is_returned_without_a_form(monkeypatch):
    form = Form()
    auto = {**APP, "control_status": "AUTO_LIMITED"}
    result, session = call(monkeypatch, routes(auto), "limit_app", {"app_id": "a1"}, form)
    out = payload(result)
    assert out["can_apply"] is False and out["blockers"]
    assert not form.seen and not writes(session)


def test_register_form_lets_the_user_change_the_priority(monkeypatch):
    form = Form(content={"priority": "medium"})
    result, session = call(monkeypatch, register_routes(), "register_app", {"name": "benchmark_app"}, form)
    assert payload(result)["applied"] is True
    assert "processes now: 501 benchmark_app" in form.seen[0].message
    assert "also matched the search but not included: bash" in form.seen[0].message
    (call_,) = writes(session)
    assert call_["body"]["priority"] == "medium" and call_["body"]["process_names"] == ["benchmark_app"]


def test_the_model_cannot_supply_the_answer(monkeypatch):
    # The resolver-filled parameters are not part of the tool's input schema.
    async def go():
        async with Client(mcp_server.build_server(allow_control=True), mode=MODE[0]) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    for name in mcp_server.WRITE_TOOLS:
        assert not {"plan", "answer", "yes", "approve"} & set(tools[name].input_schema.get("properties", {}))
        assert tools[name].annotations.destructive_hint is True


def test_write_tools_are_off_by_default():
    async def go():
        async with Client(mcp_server.build_server(), mode=MODE[0]) as c:
            return {t.name for t in (await c.list_tools()).tools}
    names = asyncio.run(go())
    assert names and not names & mcp_server.WRITE_TOOLS


class DownSession(FakeSession):
    """SmarTune not running: every request fails to connect."""

    def request(self, *args, **kwargs):
        import requests
        raise requests.exceptions.ConnectionError("refused")


@pytest.mark.parametrize("tool,args", [("limit_app", {"app_id": "a1"}), ("restore_app", {"app_id": "a1"}),
                                       ("register_app", {"name": "benchmark_app"})])
def test_service_down_reports_the_error_and_hint(monkeypatch, tool, args):
    client = SmartuneClient(url="https://127.0.0.1:9001", token="t", session=DownSession({}))
    monkeypatch.setattr(mcp_server, "execute", lambda parsed: cli.execute(parsed, client=client))
    form = Form()

    async def go():
        async with Client(mcp_server.build_server(allow_control=True), mode=MODE[0],
                          elicitation_callback=form) as c:
            return await c.call_tool(tool, args)
    result = asyncio.run(go())
    assert result.is_error and not form.seen
    assert "Cannot connect to SmarTune" in result.content[0].text
    assert "service is running" in result.content[0].text


def test_device_frequency_limit_applies_after_approval_with_a_stable_form(monkeypatch):
    from test_control import freq_routes
    r = freq_routes(extra={("POST", "/device/freq/set"): ok("ok", {"results": {}})})
    form = Form()
    result, session = call(monkeypatch, r, "limit_device_frequency", {"units": ["p"], "max_mhz": 3000}, form)
    out = payload(result)
    assert out["applied"] is True
    (call_,) = writes(session)
    assert call_["body"] == {"units": ["cpu:policy0", "cpu:policy1"], "min_mhz": 800.0, "max_mhz": 3000.0}
    message = form.seen[-1].message
    assert "cpu:policy0: 800-4800 MHz -> 800-3000 MHz" in message and "every app" in message
    assert "1234" not in message  # the live clock would make every round a new question


def test_device_frequency_declined_or_restored_only_when_approved(monkeypatch):
    from test_control import freq_routes
    r = freq_routes(extra={("POST", "/device/freq/restore"): ok("ok")})
    result, session = call(monkeypatch, r, "restore_device_frequency", {}, Form("decline"))
    assert payload(result)["applied"] is False and not writes(session)
    result, session = call(monkeypatch, r, "restore_device_frequency", {}, Form())
    assert payload(result)["applied"] is True
    assert [c["body"] for c in writes(session)] == [{"units": ["cpu:policy4"]}]
