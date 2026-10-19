# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Standalone entry point for the monitor telemetry service. Run from the
# repository root with ``python -m monitor.monitor_service``.

import logging
import os
import signal
import ssl
import sys

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set the service mode before importing the logger. Preserve a mode set by
# smartune.py when it launches this module.
os.environ.setdefault("SMARTUNE_MODE", "monitor")

from db.DatabaseModel import init_database
from monitor.monitor_api import (
    monitor_bp,
    _start_snapshot_cleanup_task,
    _start_dynamic_info_auto_refresh,
    stop_dynamic_info_collector,
)
from monitor.system_info import preload_static_info, shutdown_gpu_usage
from features import mount_benchmark, mount_dashboard, mount_diagnostics
from smartune_api import auth_bp, set_benchmark_available, set_diagnostics_available, smartune_bp
try:
    from diagnostics.detectors import start_detector_loop
except ModuleNotFoundError as exc:
    if exc.name not in {"diagnostics", "diagnostics.detectors"}:
        raise

    def start_detector_loop():
        return None
from utils.logger import get_logger
logger = get_logger(__name__)
try:
    from diagnostics import emit_event  # best-effort seam; no-op if stripped
except Exception:  # pragma: no cover
    def emit_event(*_a, **_k):
        return None
from utils.ui_lease import get_ui_lease_manager
from utils.http_utils import apply_request_limits, QuietWSGIRequestHandler

app = Flask(__name__)
apply_request_limits(app)
app.register_blueprint(monitor_bp)
# Report capabilities for the monitor-only service.
app.register_blueprint(smartune_bp)
# Apply the shared access-token gate to monitor routes.
app.register_blueprint(auth_bp)
# Benchmark and diagnostics routes are optional features mounted at startup.
set_benchmark_available(mount_benchmark(app))
# Mount diagnostics routes in monitor-only deployments as well.
set_diagnostics_available(mount_diagnostics(app))
# Serve the built dashboard (dashboard/dist) and route its /api/* calls to the
# blueprints above, so the browser sees a full UI at the same https origin.
mount_dashboard(app)
_start_snapshot_cleanup_task()

_KEY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "key")
CERT_FILE = os.path.join(_KEY_DIR, 'b_server.crt')
KEY_FILE = os.path.join(_KEY_DIR, 'b_server.key')

_shutdown_started = False


class _ClientDisconnectFilter(logging.Filter):
    """Silence werkzeug's noisy traceback when a client tears down a response
    mid-flight (benign — the peer closed the connection before we finished
    writing). Mirrors the filter used by balance_service.py."""

    _BENIGN = (
        "BrokenPipeError",
        "UNEXPECTED_EOF_WHILE_READING",
        "ConnectionResetError",
        "ConnectionAbortedError",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            text = str(record.msg)
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        return not any(marker in text for marker in self._BENIGN)


def _shutdown_once():
    global _shutdown_started
    if _shutdown_started:
        return
    _shutdown_started = True
    logger.info("Shutting down Monitor Service...")
    try:
        stop_dynamic_info_collector()
    except Exception as exc:
        logger.warning(f"stop_dynamic_info_collector failed: {exc}")
    try:
        shutdown_gpu_usage()
    except Exception as exc:
        logger.warning(f"shutdown_gpu_usage failed: {exc}")
    # Signal that shutdown is complete
    emit_event("PLATFORM_SERVICE_STOPPED", severity="info", category="platform.availability",
               source="monitor", summary="Monitor service stopped (API + Dashboard)",
               attributes={"api_service": "stopped", "dashboard_ui": "stopped"})


def _handle_signal(signum, frame):
    _shutdown_once()
    raise SystemExit(0)


def _request_ui_shutdown():
    """UI-lease watchdog callback: the last dashboard UI is gone, so exit.

    Runs on the watchdog thread; signal handlers only fire on the main thread,
    so we raise SIGTERM to ourselves. The existing _handle_signal then performs
    the same graceful teardown + SystemExit(0) as `systemctl stop`, and exit
    code 0 means systemd's Restart=on-failure will not bring us back."""
    logger.info("UI closed; shutting down monitor service.")
    os.kill(os.getpid(), signal.SIGTERM)


def main():
    f = _ClientDisconnectFilter()
    for name in ("werkzeug", ""):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)

    logger.info("Starting Monitor Service...")
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        logger.error(f"Certificate files not found: {CERT_FILE}, {KEY_FILE}, "
                     f"please run 'bash start_smartune.sh -m' to generate them.")
        return

    init_database()
    try:
        preload_static_info()
    except Exception as exc:
        logger.warning(f"Preload static info failed, will retry on first static request: {exc}")

    # Persist dynamic history in monitor-only mode. This is a no-op when no
    # sections are configured for monitoring.
    _start_dynamic_info_auto_refresh()
    start_detector_loop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    emit_event("PLATFORM_SERVICE_STARTED", severity="info", category="platform.availability",
               source="monitor", summary="Monitor service started (API + Dashboard)",
               attributes={
                   "mode": os.environ.get("SMARTUNE_MODE", "monitor"),
                   "api_service": "started",
                   "dashboard_ui": "mounted",
               })

    # Packaged deployments may stop the service when the last dashboard closes.
    if os.environ.get("SMARTUNE_UI_LEASE"):
        get_ui_lease_manager().enable(_request_ui_shutdown)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

    # Independent host/port from the balancer (which defaults to 9001) so both
    # can run side by side.
    host = os.environ.get("MONITOR_HOST", "127.0.0.1")
    port = int(os.environ.get("MONITOR_PORT", "9001"))

    try:
        # threaded=True: without it the dev server serializes every request, so a
        # blocking /diag/* query would stall the dashboard's continuous /monitor
        # polling. See balance_service.py for the same rationale.
        app.run(host=host, port=port, debug=False, use_reloader=False,
                threaded=True, ssl_context=ssl_context,
                request_handler=QuietWSGIRequestHandler)
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        # Record uncaught exception as a diagnostic event before cleanup.
        # Re-raise so process exits with non-zero; systemd's Restart=on-failure will recover.
        try:
            emit_event("PLATFORM_SERVICE_CRASHED", severity="critical", category="platform.availability",
                       impact="failed", source="monitor",
                       summary="Monitor service crashed with uncaught exception",
                       attributes={"exception_type": type(exc).__name__, "exception_message": str(exc)[:500]})
        except Exception:
            pass  # Best-effort; never let diagnostics itself break crash recovery
        raise
    finally:
        _shutdown_once()


if __name__ == "__main__":
    main()
