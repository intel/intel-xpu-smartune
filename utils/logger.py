# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmartTune application logging.
#
# One structured store for every SmartTune-produced log line, so the diagnostics
# `smartune` source can read them back for querying, alerting and correlation.
# Design contract:
#
#   * production format = one JSON object per line (grep- and Agent-parseable);
#     development format = human-readable text.
#   * every record carries ts / level / service / mode / logger / pid / thread /
#     msg, plus app_id / job_id / event_type when relevant.
#   * single landing: SmartTune's own rotated JSON file is the ONLY place the
#     application log goes -- no journald, no second file (see smartune.service).
#   * level from SMARTUNE_LOG_LEVEL (default INFO); format from
#     SMARTUNE_LOG_FORMAT (json|text, auto by tty otherwise); mode from
#     SMARTUNE_MODE (all|monitor|diag).
#   * import-time init is fault tolerant: an unwritable log dir degrades to
#     console-only, it never stops the process (or a test) from starting.
#
# Business modules acquire a per-module logger with ``get_logger(__name__)``; the
# top-level package of the name becomes the ``service`` field. The module-level
# ``logger`` singleton is kept for backward compatibility with existing imports.

import contextlib
import contextvars
import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime


# --- configuration from the environment -----------------------------------

# Log directory. Defaults to <repo>/logs; SMARTUNE_LOG_DIR overrides it so a
# packaged install (or a test) can redirect logs elsewhere without code changes.
LOG_DIR = os.environ.get("SMARTUNE_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)
LOG_PREFIX = "multi_tasks"
# A fresh timestamped log file is created on every process start (below), i.e.
# one per service start/restart, so LOG_DIR would otherwise grow without bound.
# Keep only the most recent LOG_RETENTION run logs and prune the rest at startup.
LOG_RETENTION = 10

# Process launch mode, surfaced as the ``mode`` field. Set by smartune.py /
# monitor_service.py before this module is imported; defaults to "all".
MODE = os.environ.get("SMARTUNE_MODE", "all")

# Top-level package name -> service label. Anything not a first-class service
# (shared helpers, config, db, the api/launcher glue) reports as "smartune".
_SERVICE_PACKAGES = {"balancer", "monitor", "benchmark", "diagnostics"}

# Fields we lift off a record (via ``extra=`` or contextvars) when present.
_CONTEXT_FIELDS = ("app_id", "job_id", "event_type")

# Context-bound correlation ids: set at a request/task boundary with
# bind_log_context(); the formatter falls back to these when a call site did not
# pass the field explicitly via extra=.
_ctx_vars = {name: contextvars.ContextVar("smartune_" + name, default=None)
             for name in _CONTEXT_FIELDS}


@contextlib.contextmanager
def bind_log_context(**fields):
    """Bind correlation ids (app_id / job_id / event_type) for the
    duration of the ``with`` block so nested log calls inherit them without every
    call site passing ``extra=``. Unknown keys are ignored."""
    tokens = []
    for key, value in fields.items():
        var = _ctx_vars.get(key)
        if var is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


def current_log_context():
    """Return the currently bound correlation ids as a dict (non-None keys only),
    so diagnostics' emit_event() can inherit them from the ambient
    bind_log_context() without threading them through."""
    return {name: var.get() for name, var in _ctx_vars.items() if var.get() is not None}


def _resolve_level():
    """Logging level from SMARTUNE_LOG_LEVEL (name or number); default INFO."""
    raw = os.environ.get("SMARTUNE_LOG_LEVEL")
    if not raw:
        return logging.INFO
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    return logging.getLevelName(raw.upper()) if isinstance(
        logging.getLevelName(raw.upper()), int) else logging.INFO


def _resolve_format():
    """'json' or 'text'. SMARTUNE_LOG_FORMAT wins; otherwise text on an
    interactive tty (readable during development), json otherwise (systemd)."""
    raw = (os.environ.get("SMARTUNE_LOG_FORMAT") or "").strip().lower()
    if raw in ("json", "text"):
        return raw
    try:
        return "text" if sys.stderr.isatty() else "json"
    except Exception:
        return "json"


def _stderr_is_interactive():
    """True when stderr is a terminal (a developer console), False under systemd
    or when stderr is redirected. Governs whether the console log handler is
    attached (see Logger.__init__)."""
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _service_for(name):
    """Map a logger/module name to a ``service`` label by its top-level package."""
    top = (name or "").split(".", 1)[0]
    return top if top in _SERVICE_PACKAGES else "smartune"


def _iso_now(record):
    """Timezone-aware ISO 8601 with millisecond precision, e.g.
    2026-09-08T15:32:07.123+08:00."""
    dt = datetime.fromtimestamp(record.created).astimezone()
    return dt.isoformat(timespec="milliseconds")


def _record_context(record):
    """Collect the optional correlation fields present on a record, falling back
    to the context-bound values from bind_log_context()."""
    out = {}
    for field in _CONTEXT_FIELDS:
        value = getattr(record, field, None)
        if value is None:
            value = _ctx_vars[field].get()
        if value is not None:
            out[field] = value
    return out


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with the field schema from the module docstring."""

    def format(self, record):
        payload = {
            "ts": _iso_now(record),
            "level": record.levelname,
            "service": getattr(record, "service", None) or _service_for(record.name),
            "mode": MODE,
            "logger": getattr(record, "logger_name", None) or record.name,
            "pid": record.process,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        payload.update(_record_context(record))
        if record.exc_info:
            payload["exc"] = "".join(
                traceback.format_exception(*record.exc_info)).rstrip()
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """Human-readable development format; appends the correlation ids only when
    present so dev output stays uncluttered."""

    def __init__(self):
        super().__init__("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    def format(self, record):
        # Show the real module name (injected by get_logger) rather than the
        # shared base-logger name, so dev output still says which module logged.
        record.name = getattr(record, "logger_name", None) or record.name
        base = super().format(record)
        ctx = _record_context(record)
        if ctx:
            base += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return base


def _make_formatter():
    return _JsonFormatter() if _resolve_format() == "json" else _TextFormatter()


class _SmartuneLoggerAdapter(logging.LoggerAdapter):
    """Injects ``service`` and the real module ``logger_name`` onto every record
    while preserving any ``extra=`` a call site passes (call-site keys win)."""

    def process(self, msg, kwargs):
        extra = dict(self.extra)
        caller_extra = kwargs.get("extra")
        if caller_extra:
            extra.update(caller_extra)
        kwargs["extra"] = extra
        return msg, kwargs


# --- log-file rotation ------------------------------------------------------


def prune_old_logs(log_dir, prefix=LOG_PREFIX, keep=LOG_RETENTION):
    """Delete all but the ``keep`` newest ``<prefix>_<timestamp>.log`` files.

    Called at import so every process start trims the backlog. Best-effort: it
    never raises, so a cleanup hiccup cannot stop the service from starting. The
    ``<prefix>_latest.log`` symlink and non-regular files are left untouched.
    Files are ranked by name, which the fixed ``YYYYMMDD_HHMMSS`` stamp makes
    chronological without depending on mtimes. ``keep < 0`` disables pruning;
    ``keep == 0`` removes all matching files.
    """
    if keep < 0:
        return
    try:
        names = os.listdir(log_dir)
    except OSError:
        return
    latest = f"{prefix}_latest.log"
    candidates = []
    for name in names:
        if name == latest:
            continue
        if not (name.startswith(prefix + "_") and name.endswith(".log")):
            continue
        path = os.path.join(log_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        candidates.append(path)
    candidates.sort()  # lexical == chronological for the fixed-width timestamp
    for path in (candidates[:-keep] if keep else candidates):
        try:
            os.remove(path)
        except OSError:
            pass


# --- base logger configuration --------------------------------------------


class Logger:
    """Configures the shared base logger. Kept as a class for backward
    compatibility with ``Logger(...).get_logger()`` callers. The file handler is
    best-effort: an unwritable log directory degrades to console-only rather
    than aborting import."""

    def __init__(self, log_file=None, log_level=None):
        self.log_file = log_file
        self.log_level = _resolve_level() if log_level is None else log_level
        # A single named base logger owns the handlers; get_logger() hands out
        # adapters over it (see below). Naming it "smartune" keeps records off
        # the root logger so third-party libraries don't re-emit them.
        self.logger = logging.getLogger("smartune")
        self.logger.setLevel(self.log_level)

        # Wipe handlers left by a previous instantiation and stop propagation so
        # external libraries' root handlers don't duplicate our records.
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()
        self.logger.propagate = False

        # The file is the diagnostics `smartune` source's store and must parse as
        # one JSON object per line, so it is ALWAYS JSON regardless of the tty
        # heuristic. SMARTUNE_LOG_FORMAT governs only the console view.
        file_formatter = _JsonFormatter()
        console_formatter = _make_formatter()

        file_ok = False
        if log_file:
            try:
                os.makedirs(os.path.dirname(log_file), exist_ok=True)
                file_handler = logging.FileHandler(log_file)
                file_handler.setFormatter(file_formatter)
                file_handler.stream.reconfigure(encoding="utf-8")
                self.logger.addHandler(file_handler)
                file_ok = True
            except OSError:
                # Unwritable/missing log dir must not stop startup: fall back to
                # console-only. systemd captures the console via StandardError.
                pass

        # The console sink would violate the single-landing contract under
        # systemd (StandardError -> journald, a second store nothing reads), so
        # attach it only when there is no working file sink (degraded fallback)
        # or we are on an interactive tty (developer console). A fatal crash
        # still reaches journald as the interpreter's own stderr.
        if (not file_ok) or _stderr_is_interactive():
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(console_formatter)
            with contextlib.suppress(Exception):
                console_handler.stream.reconfigure(encoding="utf-8")
            self.logger.addHandler(console_handler)

    def get_logger(self):
        return self.logger

    def info(self, message):
        self.logger.info(message)

    def debug(self, message):
        self.logger.debug(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)


def _init_base_logger():
    """Create the timestamped run log, refresh the ``latest`` symlink, prune the
    backlog, and return the configured base logger. All filesystem steps are
    best-effort so import never fails on a read-only log directory."""
    log_file_path = None
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file_path = os.path.join(LOG_DIR, f"{LOG_PREFIX}_{stamp}.log")
    except Exception:
        log_file_path = None

    base = Logger(log_file=log_file_path).get_logger()

    # Stable "latest" symlink so log-tailing scripts don't guess the timestamp.
    if log_file_path:
        try:
            latest_link = os.path.join(LOG_DIR, f"{LOG_PREFIX}_latest.log")
            if os.path.islink(latest_link) or os.path.exists(latest_link):
                os.remove(latest_link)
            os.symlink(os.path.basename(log_file_path), latest_link)
        except OSError:
            pass
        # Trim the backlog now that this run's file exists, so it is counted
        # among the retained set and never pruned from under the handler.
        prune_old_logs(LOG_DIR)

    return base


_base_logger = _init_base_logger()


def _install_crash_hooks():
    """Route uncaught exceptions through the logger so a crash lands in the same
    JSON store as every other record and the diagnostics ``smartune`` source can
    turn it into a LOG_EXCEPTION event/alert.

    Production (no tty): the traceback is NOT echoed to stderr, keeping the log
    stream out of journald. Developer (tty): the previous hook is chained so the
    console traceback still shows."""
    previous_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_excepthook(exc_type, exc_value, exc_tb)
            return
        with contextlib.suppress(Exception):
            _base_logger.critical(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        if _stderr_is_interactive():
            previous_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        thread = getattr(args, "thread", None)
        with contextlib.suppress(Exception):
            _base_logger.critical(
                "Uncaught exception in thread %s", getattr(thread, "name", "?"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    with contextlib.suppress(Exception):
        threading.excepthook = _thread_excepthook


_install_crash_hooks()


def get_logger(name=None):
    """Return a logger for ``name`` (typically ``__name__``). The adapter tags
    every record with the ``service`` derived from the module's top-level
    package plus the real module name, then writes through the shared base
    logger's handlers. Use it exactly like a stdlib logger."""
    name = name or "smartune"
    return _SmartuneLoggerAdapter(
        _base_logger, {"service": _service_for(name), "logger_name": name})


# Backward-compatible module-level singleton. Existing code does
# ``from utils.logger import logger``; it keeps working, tagged service=smartune.
logger = get_logger("smartune")


# Test the logger
def test_logger():
    logger.info("This is an info message.")
    logger.debug("This is a debug message.")
    logger.error("This is an error message.")
    logger.critical("This is a critical message.")


if __name__ == "__main__":
    test_logger()
