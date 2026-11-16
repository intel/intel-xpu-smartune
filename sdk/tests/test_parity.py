# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Parity checks between the SDK and the rest of the repository, so the three
# places that describe the same surface cannot drift apart unnoticed:
#
#   - every READ_ROUTES and CONTROL_ROUTES entry still names a route the server
#     defines, and neither list reaches an endpoint it must not, and
#   - every `smartune ...` command quoted in a skill or in agent/ still parses.
#
# The server routes are read from the source by pattern, without importing the
# Flask apps (which pull in psutil, eBPF and the database). The tests skip when
# the SDK runs outside a repository checkout.

import os
import re
import functools
import shlex
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartune_sdk.cli import LOG_SOURCES, build_parser  # noqa: E402
from smartune_sdk.client import CONTROL_ROUTES, READ_ROUTES, is_read_route  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
IN_REPO = os.path.isfile(os.path.join(REPO, "smartune_api.py"))
pytestmark = pytest.mark.skipif(not IN_REPO, reason="needs the SmarTune repository checkout")

SKIP_DIRS = {"sdk", "dashboard", "node_modules", "test", "tests", "coverity_build", "build", ".git"}
BLUEPRINT_RE = re.compile(r"""(\w+)\s*=\s*Blueprint\([^)]*?url_prefix\s*=\s*['"]([^'"]+)['"]""")
ROUTE_RE = re.compile(r"""^\s*@(\w+)\.route\(\s*(['"])([^'"]+)\2\s*(?:,\s*methods\s*=\s*\[([^\]]*)\])?\s*\)""", re.M)
# A sample value for each <converter> segment, so a concrete path can be matched
# against the client's regexes. Letters only: the static_info section pattern is [a-z]+.
SAMPLE_SEGMENT = "sample"


def _python_sources():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


@functools.lru_cache(maxsize=None)
def server_routes():
    """{(method, concrete_path)} for every route decorator in the server source."""
    sources = {path: open(path, encoding="utf-8").read() for path in _python_sources()}
    prefixes = {"app": ""}
    for text in sources.values():
        prefixes.update(dict(BLUEPRINT_RE.findall(text)))
    routes = set()
    for text in sources.values():
        for owner, _, rule, methods in ROUTE_RE.findall(text):
            if owner not in prefixes:
                continue
            path = prefixes[owner] + re.sub(r"<[^>]+>", SAMPLE_SEGMENT, rule)
            for method in re.findall(r"[A-Z]+", methods or "GET"):
                routes.add((method, path))
    return frozenset(routes)


def test_route_scan_finds_the_server():
    # Guards the scanner itself: if it silently found nothing, the next test would pass vacuously.
    routes = server_routes()
    assert ("GET", "/monitor/processes") in routes
    assert ("POST", "/app/kill_process") in routes
    assert ("GET", "/bench/run/" + SAMPLE_SEGMENT) in routes


@pytest.mark.parametrize("method,pattern", READ_ROUTES)
def test_read_route_exists_on_server(method, pattern):
    regex = re.compile(pattern + r"\Z")
    matches = [p for m, p in server_routes() if m == method and regex.match(p)]
    assert matches, (f"READ_ROUTES has {method} {pattern}, but no server route matches it; "
                     "update client.py and docs/reference/agent-api-exposure.md")


# POST endpoints that are really queries (the read-only POST rows of agent-api-exposure.md). Any
# other POST the allow-list reaches is a write and fails the test below.
QUERY_POSTS = {
    "/app/get_controlled_app", "/app/get_pending_app", "/app/get_priority_data",
    "/app/limit_snapshot", "/app/auto_limited_apps",
    "/app/resource_limit_profile", "/app/auto_limit_exclusions",
    "/app/discover_search", "/app/discover_extract",
}
# GETs with side effects, kept out of the allow-list on purpose.
SIDE_EFFECT_GETS = {"/bench/env"}


def test_read_routes_reach_no_write_endpoint():
    reached = {(m, p) for m, p in server_routes() if is_read_route(m, p)}
    assert reached, "the allow-list reaches no server route at all"
    for method, path in sorted(reached):
        assert method in ("GET", "POST"), f"{method} {path} is allow-listed"
        if method == "POST":
            assert path in QUERY_POSTS, f"POST {path} is allow-listed but is not a known query"
        assert path not in SIDE_EFFECT_GETS, f"GET {path} has side effects but is allow-listed"


@pytest.mark.parametrize("method,path", CONTROL_ROUTES)
def test_control_route_exists_on_server(method, path):
    assert (method, path) in server_routes(), (
        f"CONTROL_ROUTES has {method} {path}, but the server defines no such route")


# Endpoints agent-api-exposure.md classifies as forbidden or internal and that change state: never
# reachable by an agent, whatever the allow-lists say.
FORBIDDEN_WRITES = {
    "/app/kill_process", "/app/suspend_process",
    "/app/set_oom_score", "/app/set_to_control",
    "/app/merge_controlled_app_processes", "/app/set_controlled_app_processes",
    "/app/adopt_auto_limit", "/app/check_running_apps", "/smartune/shutdown",
}


def test_control_routes_stay_inside_the_control_set():
    paths = {p for _, p in CONTROL_ROUTES}
    assert not paths & FORBIDDEN_WRITES
    # App actions, plus the two device frequency writes (manual, clamped to the hardware range,
    # restored when the service stops). Anything else needs its own review first.
    assert all(p.startswith("/app/") or p in ("/device/freq/set", "/device/freq/restore") for p in paths)
    assert not any(is_read_route(m, p) for m, p in CONTROL_ROUTES), "a write is also in READ_ROUTES"


def test_exposure_doc_lists_every_route():
    # agent-api-exposure.md decides, route by route, what agents may reach. A route
    # added to the server without a row there has not been classified yet.
    doc = open(os.path.join(REPO, "docs", "reference", "agent-api-exposure.md"), encoding="utf-8").read()
    wildcards = [w for w in re.findall(r"`(?:[A-Z/]+ )?(/[\w/-]+)/\*`", doc)]
    missing = []
    for method, path in sorted(server_routes()):
        static = re.sub(r"/" + SAMPLE_SEGMENT + r"(?=/|$)", "", path)
        last = "/" + static.rstrip("/").rsplit("/", 1)[-1]
        # Rows abbreviate sibling routes to their last segment, e.g. "`/silence`".
        if static in doc or f"`{last}`" in doc or f"{last}`" in doc:
            continue
        if any(static.startswith(w + "/") for w in wildcards):
            continue
        missing.append(f"{method} {path}")
    assert not missing, ("server routes not classified in docs/reference/agent-api-exposure.md: "
                         + ", ".join(missing))


def test_log_sources_match_server():
    # The server silently ignores an unknown ?source=, so the CLI validates names itself.
    text = open(os.path.join(REPO, "diagnostics", "sources", "__init__.py"), encoding="utf-8").read()
    registered = re.findall(r"""\(\s*["'](\w+)["']\s*,\s*["']\w+LogSource["']\s*\)""", text)
    assert registered, "could not find the built-in log sources"
    assert sorted(LOG_SOURCES) == sorted(registered)


def _quoted_commands():
    """(file, command) for every `smartune ...` inline code span in skills/ and agent/."""
    found = []
    for top in ("skills", "agent"):
        for root, _, files in os.walk(os.path.join(REPO, top)):
            for name in files:
                if name.endswith(".md"):
                    path = os.path.join(root, name)
                    text = open(path, encoding="utf-8").read()
                    rel = os.path.relpath(path, REPO)
                    found += [(rel, cmd) for cmd in re.findall(r"`(smartune(?: [^`]*)?)`", text)]
    return found


def _argv(command):
    # Placeholders such as <app_id> become sample values; <pid> must be an integer.
    def fill(token):
        token = token.replace("<pid>", "1")
        return re.sub(r"<[^>]+>", "sample", token)
    return [fill(t) for t in shlex.split(command)[1:]]


def test_skills_quote_commands():
    assert len(_quoted_commands()) > 20


@pytest.mark.parametrize("source,command", _quoted_commands() if IN_REPO else [])
def test_quoted_command_parses(source, command):
    argv = _argv(command)
    if not argv:  # a bare `smartune` mention
        return
    if argv in (["diag"], ["bench"], ["control"]):  # naming a command group, e.g. `smartune control`
        return
    try:
        build_parser().parse_args(argv)
    except SystemExit:
        pytest.fail(f"{source}: `{command}` is not a valid smartune command")


def _mcp_column_rows():
    """(file, row text, MCP tool cell) for every table row in skills/ with an "MCP tool" column."""
    found = []
    for root, _, files in os.walk(os.path.join(REPO, "skills")):
        for name in files:
            if name != "SKILL.md":
                continue
            path = os.path.join(root, name)
            column = None
            for line in open(path, encoding="utf-8"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else None
                if cells is None:
                    column = None
                elif "MCP tool" in cells:
                    column = cells.index("MCP tool")
                elif column is not None and not set(cells[0]) <= set("-"):
                    found.append((os.path.relpath(path, REPO), line, cells[column]))
    return found


def test_skills_have_mcp_columns():
    if not IN_REPO:
        pytest.skip("not in a repository checkout")
    assert len({source for source, _, _ in _mcp_column_rows()}) == 4


@pytest.mark.parametrize("source,row,cell", _mcp_column_rows() if IN_REPO else [])
def test_mcp_column_names_the_rows_tool(source, row, cell):
    """Each tool in a skill's MCP column exists, takes the parameters named there, and runs one of
    the commands quoted in the same row, so a model can use either column."""
    import inspect
    from smartune_sdk import mcp_server
    commands = [_argv(c) for c in re.findall(r"`(smartune [^`]*)`", row)]
    calls = re.findall(r"`([a-z_]+)(?:\(([^`]*)\))?`", cell)
    assert calls, f"{source}: no tool in the MCP column of: {row.strip()}"
    for tool, args in calls:
        assert tool in mcp_server.TOOL_COMMANDS, f"{source}: `{tool}` is not an MCP tool"
        words = mcp_server.TOOL_COMMANDS[tool].split()
        assert any(argv[:len(words)] == words for argv in commands), \
            f"{source}: `{tool}` runs `smartune {' '.join(words)}`, which this row does not quote"
        params = set(inspect.signature(mcp_server.TOOLS[tool]).parameters)
        named = {re.split(r"[=\s]", a.strip())[0] for part in args.split(",") for a in part.split("/") if a.strip()}
        assert named <= params, f"{source}: `{tool}` has no parameter {sorted(named - params)}"
