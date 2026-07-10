# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmarTune product-level API: cross-service endpoints shared by the combined
# balancer service (balance_service.py) and the standalone monitor service
# (monitor_service.py). Each service registers smartune_bp alongside its own
# domain blueprints/routes.

from flask import Blueprint

from utils.http_utils import construct_response

# The dashboard queries /smartune/capabilities to learn whether the server it is
# connected to provides the full balancer feature set or is a monitor-only
# deployment: 1 = balancer + monitor, 0 = monitor only.
smartune_bp = Blueprint('smartune', __name__, url_prefix='/smartune')

_balancer_available = False


def set_balancer_available(available: bool) -> None:
    """Mark whether balancer features are served by this process. balance_service
    calls this at startup; the standalone monitor service leaves it False."""
    global _balancer_available
    _balancer_available = bool(available)


@smartune_bp.route('/capabilities', methods=['GET'])
def get_capabilities():
    """Report server capability level: 1 = balancer + monitor, 0 = monitor only."""
    return construct_response(
        data={"capabilities": 1 if _balancer_available else 0},
        retmsg="Successfully retrieved capabilities",
    )
