# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmartTune diagnostics: the traceable diagnostic & abnormal-event system
# (see docs/diagnostics_plan.md). This package owns the operational-event ledger,
# alert derivation, context assembly, pluggable log sources, the unified
# log-query façade and the /diag/* API blueprint.
#
# Business modules should import only the seams from here -- ``emit_event`` for a
# single structured event, or ``record_control_action`` for a completed resource
# -control fact (it owns the CONTROL_* protocol so callers pass neutral facts, not
# event types). The heavier stack (event_store, diag_api, sources, metrics) is
# imported lazily by the pieces that need it, so these imports stay cheap and
# side-effect free.

from diagnostics.emitter import emit_event
from diagnostics.control_events import record_control_action

__all__ = ["emit_event", "record_control_action"]
