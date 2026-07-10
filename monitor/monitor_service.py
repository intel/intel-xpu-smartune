# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Standalone entry point for the monitor (telemetry) service.
#
# Unlike balance_service.py — which mounts the monitor blueprint inside the
# balancer process — this module runs the monitor REST API on its own, without
# any dependency on the balancer control logic (balancer/controller,
# balancer/balancer). It is what keeps a usable, runnable monitor around even if
# the balancer pieces are removed. Run it from the repository root:
#     python -m monitor.monitor_service
#
# When run without the balancer, monitor_api has no SystemPressureMonitor
# registered, so it falls back to lazily creating its own instance
# (see monitor.monitor_api._get_system_pressure_monitor) and reports pure
# system pressure state.

import logging
import os
import signal
import ssl
import sys

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.DatabaseModel import init_database
from monitor.monitor_api import monitor_bp, _start_snapshot_cleanup_task
from monitor.system_info import preload_static_info, shutdown_gpu_usage
from smartune_api import smartune_bp
from utils.logger import logger

app = Flask(__name__)
app.register_blueprint(monitor_bp)
# smartune_bp reports capabilities. The balancer-available flag stays False here,
# so /smartune/capabilities returns 0 (monitor only).
app.register_blueprint(smartune_bp)
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
        shutdown_gpu_usage()
    except Exception as exc:
        logger.warning(f"shutdown_gpu_usage failed: {exc}")


def _handle_signal(signum, frame):
    _shutdown_once()
    raise SystemExit(0)


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
                     f"please run 'start_smartune.sh -m' to generate them.")
        return

    init_database()
    try:
        preload_static_info()
    except Exception as exc:
        logger.warning(f"Preload static info failed, will retry on first static request: {exc}")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(CERT_FILE, KEY_FILE)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

    # Independent host/port from the balancer (which defaults to 9001) so both
    # can run side by side.
    host = os.environ.get("MONITOR_HOST", "127.0.0.1")
    port = int(os.environ.get("MONITOR_PORT", "9001"))

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, ssl_context=ssl_context)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _shutdown_once()


if __name__ == "__main__":
    main()
