# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Log-source registry. New source types register here and become visible to
# log_query / detectors / bundle without touching them.
#
# Built-in sources include `smartune` (own JSON log files), `benchmark` (runs/
# artifacts), and the read-only local `journal` adapter. `platform` remains an
# optional future hardware-specific source.

from diagnostics.sources.base import LogQueryFilter, LogRecord, LogSource

_REGISTRY = {}


def register_source(source: LogSource) -> None:
    _REGISTRY[source.name] = source


def get_source(name):
    return _REGISTRY.get(name)


def iter_sources():
    return list(_REGISTRY.values())


def source_names():
    return list(_REGISTRY.keys())


def _register_builtin():
    """Register the stage-2 built-in sources. Import errors in one source must
    not sink the others, so each is guarded independently."""
    from utils.logger import get_logger
    logger = get_logger(__name__)
    for modname, clsname in (("smartune", "SmartuneLogSource"),
                             ("benchmark", "BenchmarkLogSource"),
                             ("journal", "JournalLogSource")):
        try:
            mod = __import__(f"diagnostics.sources.{modname}", fromlist=[clsname])
            register_source(getattr(mod, clsname)())
        except Exception as exc:
            logger.warning("Diagnostics source %r not registered: %s", modname, exc)


_register_builtin()

__all__ = ["LogSource", "LogRecord", "LogQueryFilter",
           "register_source", "get_source", "iter_sources", "source_names"]
