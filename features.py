# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Optional-component assembly for the SmarTune services.
#
# Not every deployment ships every part of the product: the monitor-only .deb
# leaves out benchmark/ entirely (see packaging/deb/build_deb.sh's SRC_PATHS),
# and dashboard/dist only exists once the UI has been built. Both are mounted
# onto the Flask app the same way from both service entry points, so the "is it
# here, and if so wire it up" logic lives in one module rather than being
# duplicated in balancer/balance_service.py and monitor/monitor_service.py.
#
# This is the root of the tree on purpose. Deciding whether an optional package
# is present has to happen from OUTSIDE that package -- a guard living in
# benchmark/ would have to be imported to answer "can benchmark/ be imported" --
# and it cannot live in smartune.py either, which is only a launcher and never
# sees the Flask app (balance_service importing it back would be a cycle).

import os

from flask import send_from_directory
from werkzeug.exceptions import NotFound

from utils.logger import logger

# --- dashboard ------------------------------------------------------------
#
# Serve the built dashboard (dashboard/dist) from the same Flask process that
# exposes the API, so a single origin (e.g. https://localhost:9001) renders the
# UI *and* answers its /api/* calls. This is what lets the packaged product open
# in a browser without the separate Vite dev server.
#
# The built dashboard issues every request under /api (axios baseURL '/api').
# In development the Vite dev server rewrites '^/api' -> '' before proxying to
# the backend, whose routes live at the root (/dynamic_info, /app/..., etc.).
# ApiPrefixMiddleware reproduces exactly that rewrite at the WSGI layer, so the
# same build works unchanged whether it is served by Vite or by this process.

# Flask endpoint name of the dashboard static-file handler. The access-token gate
# in smartune_api exempts exactly this endpoint (and nothing else) so the login
# page can load before a token exists. Keying the exemption on the resolved
# endpoint — not on the URL — is what keeps the API protected: the API routes are
# ALSO mounted at the root (not only under /api), so a path-prefix rule would let
# `GET /dynamic_info` slip past auth. The endpoint is unambiguous.
DASHBOARD_ENDPOINT = "smartune_dashboard"

# dashboard/dist sits next to this module, which is at the repo root:
#   <root>/features.py  ->  <root>/dashboard/dist
_DIST_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dashboard", "dist"
)


class ApiPrefixMiddleware:
    """Strip a leading /api from the request path so the built dashboard (which
    calls /api/*) reaches the API handlers registered at the root, exactly as the
    Vite dev server's '^/api' -> '' rewrite does in development."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/api" or path.startswith("/api/"):
            environ["PATH_INFO"] = path[len("/api"):] or "/"
        return self.wsgi_app(environ, start_response)


def mount_dashboard(app, dist_dir=_DIST_DIR):
    """Serve the built dashboard from ``dist_dir`` and route /api/* to the API.

    Registers a catch-all GET handler (endpoint ``DASHBOARD_ENDPOINT``) that
    returns a static file when one exists under ``dist_dir`` and otherwise falls
    back to index.html for client-side routes, then wraps the WSGI app with
    ApiPrefixMiddleware. If the build is absent the handler is skipped but the
    middleware is still installed, so API clients keep working.

    A fully static API rule (e.g. /smartune/capabilities) outranks the
    ``/<path:path>`` converter in Werkzeug's matcher, so this catch-all never
    shadows — nor exposes — a real endpoint.
    """
    if not os.path.isdir(dist_dir):
        app.logger.warning(
            "Dashboard build not found at %s; serving API only (no UI).", dist_dir
        )
        app.wsgi_app = ApiPrefixMiddleware(app.wsgi_app)
        return

    @app.route("/", defaults={"path": ""}, endpoint=DASHBOARD_ENDPOINT)
    @app.route("/<path:path>", endpoint=DASHBOARD_ENDPOINT)
    def _serve_dashboard(path):
        # send_from_directory uses werkzeug.safe_join, which rejects absolute
        # paths and ../ traversal (NotFound). Anything that is not an existing
        # file under dist_dir falls back to index.html for client-side routing —
        # never to a file outside the build.
        if path:
            try:
                return send_from_directory(dist_dir, path)
            except NotFound:
                pass
        return send_from_directory(dist_dir, "index.html")

    app.wsgi_app = ApiPrefixMiddleware(app.wsgi_app)


# --- benchmark ------------------------------------------------------------
#
# The benchmark toolchain lives in benchmark/ -- a multi-GB vendor drop plus
# SmarTune's benchmark.service package -- and not every deployment ships it. A
# top-level `from benchmark.service import ...` in the service modules would turn
# that from "the Benchmark tab is hidden" into "the process will not start", so
# the import happens here, once, behind a guard.

# Import errors naming exactly these mean "not deployed here". Anything deeper
# (benchmark.service.foo, or a third-party package the blueprint needs) is a real
# breakage, and silently degrading it to a missing tab would hide the bug.
_BENCHMARK_ABSENT = {"benchmark", "benchmark.service"}


def mount_benchmark(app) -> bool:
    """Register the /bench blueprint on ``app``. Returns whether it is available.

    False means the tab should be hidden: either benchmark/ is not part of this
    deployment, or ``benchmark.enabled`` is off in config.yaml, or the vendored
    pipeline is incomplete (benchmark.service.env.enabled checks for the run
    template). The caller passes the result to smartune_api.set_benchmark_available
    so /smartune/capabilities reports it to the dashboard.
    """
    try:
        from benchmark.service import bench_bp, benchmark_enabled, models, privilege
    except ImportError as exc:
        if getattr(exc, "name", None) not in _BENCHMARK_ABSENT:
            raise
        logger.info("Benchmark feature not present in this deployment; /bench not mounted.")
        return False

    if not benchmark_enabled():
        logger.info("Benchmark feature disabled or incomplete; /bench not mounted.")
        return False

    app.register_blueprint(bench_bp)
    # Where the pipeline lives and which account will run it, once, at startup.
    # The same lines head every job log; having them in the service log too is
    # what answers "why is the tab like this" before any job has been started.
    try:
        privilege.log_startup()
    except Exception:
        logger.exception("Could not describe the benchmark environment")
    # Enumerating the benchmarkable models takes minutes, which is unbearable to
    # discover on first use, so a missing or stale list is rebuilt now, in the
    # background. Returns immediately and declines to do anything when there is no
    # `hf` CLI or the cache is fresh -- see models.maybe_prefetch.
    try:
        models.maybe_prefetch()
    except Exception:
        # The tab works off whatever cache exists (and a manual refresh button);
        # it is not worth failing service startup over.
        logger.exception("Benchmark model prefetch could not be started")
    return True
