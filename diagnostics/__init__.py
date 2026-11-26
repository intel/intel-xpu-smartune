# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmartTune diagnostics: the traceable diagnostic & abnormal-event system
# (see docs/diagnostics_plan.md). This package owns the operational-event ledger,
# alert derivation, context assembly, pluggable log sources, the unified
# log-query façade and the /diag/* API blueprint.
#
# Business modules should import only the seams from here:
#   ``post_event``             one event, stored in the background;
#   ``record_control_action``  a completed resource-control fact (owns the CONTROL_* protocol);
#   ``record_control_change``  any other control change, such as freeze or CPU affinity;
#   ``emit_event``             one event, stored before it returns;
#   ``record_service_started`` the service start, after recording a crash of the previous run.
# The heavier stack (event_store, diag_api, sources, metrics) is imported lazily by the pieces
# that need it, so these imports stay cheap and side-effect free.

from diagnostics.emitter import emit_event, post_event
from diagnostics.control_events import record_control_action, record_control_change
from diagnostics.service_lifecycle import record_service_started

__all__ = ["emit_event", "post_event", "record_control_action", "record_control_change",
           "record_service_started"]
