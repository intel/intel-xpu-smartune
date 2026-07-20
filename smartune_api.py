# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# SmarTune product-level API: cross-service endpoints shared by the combined
# balancer service (balance_service.py) and the standalone monitor service
# (monitor_service.py). Each service registers smartune_bp alongside its own
# domain blueprints/routes.

import hashlib
import hmac
import os
import secrets

from flask import Blueprint, request

from utils.http_utils import RetCode, construct_response
from utils.logger import logger

# The dashboard queries /smartune/capabilities to learn whether the server it is
# connected to provides the full balancer feature set or is a monitor-only
# deployment: 1 = balancer + monitor, 0 = monitor only.
smartune_bp = Blueprint('smartune', __name__, url_prefix='/smartune')

# auth_bp carries the token handshake (/auth/login) and, via before_app_request,
# the app-wide access-token enforcement. It is registered by BOTH balance_service
# and the standalone monitor_service, so a single definition here protects every
# endpoint of either process. Kept prefix-less so /auth/login stays at the root.
auth_bp = Blueprint('auth', __name__)

# Raw API access token is persisted here (0600, root-only) so operators can read
# it and hand it to dashboard users. Only its SHA-256 hash is kept in memory.
_KEY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "key")
TOKEN_FILE = os.path.join(_KEY_DIR, "api_token")

# Endpoints reachable without a valid token: the login handshake itself. Every
# other route requires a valid X-Auth-Token header.
_AUTH_EXEMPT_PATHS = frozenset({"/auth/login"})

# The SSE stream is the ONLY route allowed to authenticate via a query-string
# token, because EventSource cannot set custom headers. Query-string tokens are
# more exposed (access logs, proxies, Referer), so every other route must use the
# header. Kept in sync with the /app/events route in balance_service.py.
_SSE_TOKEN_PATH = "/app/events"

_secret_hash = None  # SHA-256 of the active API token; resolved lazily.

_balancer_available = False


def _provision_api_token():
    """Resolve the API access token: env override > persisted file > freshly generated.

    Order of precedence:
      1. BALANCER_API_TOKEN environment variable (operator-supplied override).
      2. An existing key/api_token file from a previous run.
      3. A newly generated random token, written to key/api_token with mode 0600.

    Both services run as root, so the token file is only readable by root.
    """
    env_token = os.environ.get("BALANCER_API_TOKEN")
    if env_token:
        logger.info("API token loaded from BALANCER_API_TOKEN environment variable.")
        return env_token

    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            if token:
                logger.info(f"API token loaded from {TOKEN_FILE}.")
                return token
    except OSError as exc:
        logger.warning(f"Failed to read API token file {TOKEN_FILE}: {exc}")

    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(_KEY_DIR, exist_ok=True)
        # Create with 0600 up front so the token is never briefly world-readable.
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        logger.info(
            f"Generated a new API token and saved it to {TOKEN_FILE}. "
            "Share this token with dashboard users to grant access."
        )
    except OSError as exc:
        logger.error(f"Failed to persist API token to {TOKEN_FILE}: {exc}")
    return token


def _get_secret_hash():
    """Return the SHA-256 hash of the active token, provisioning it on first use."""
    global _secret_hash
    if _secret_hash is None:
        _secret_hash = hashlib.sha256(_provision_api_token().encode()).hexdigest()
        logger.info("API access-token enforcement is active.")
    return _secret_hash


def _token_is_valid(provided: str) -> bool:
    """Constant-time comparison of a caller-supplied token against the stored hash."""
    if not provided:
        return False
    provided_hash = hashlib.sha256(provided.encode()).hexdigest()
    return hmac.compare_digest(provided_hash, _get_secret_hash())


@auth_bp.record_once
def _provision_token_on_register(_state):
    """Provision the token as soon as the blueprint is registered (i.e. at
    service startup), so key/api_token exists immediately rather than only after
    the first request arrives."""
    _get_secret_hash()


@auth_bp.before_app_request
def _enforce_api_token():
    """Application-wide gate: every request must carry a valid access token.

    Runs before every route of whichever app registers this blueprint. The login
    handshake and CORS preflight are exempt; everything else is rejected with 401
    unless it presents the token via the X-Auth-Token header. Only the SSE stream
    (_SSE_TOKEN_PATH) may instead pass ?token=, since EventSource cannot set headers.
    """
    if request.method == "OPTIONS":
        return None
    if request.path in _AUTH_EXEMPT_PATHS:
        return None

    # Header is required everywhere; the ?token= fallback is accepted only for the
    # SSE stream, which cannot send a custom header.
    provided = request.headers.get("X-Auth-Token")
    if not provided and request.path == _SSE_TOKEN_PATH:
        provided = request.args.get("token")
    if _token_is_valid(provided):
        return None

    response = construct_response(
        data={"authenticated": False},
        retcode=RetCode.UNAUTHORIZED,
        retmsg="Unauthorized: missing or invalid access token",
    )
    response.status_code = 401
    return response


@auth_bp.route('/auth/login', methods=['POST'])
def login():
    """Validate a caller-supplied token so the dashboard can gate its UI.

    Returns {"authenticated": true} on a match. This endpoint is exempt from the
    token gate so the frontend can verify a token before storing it; all other
    endpoints still require the token to be presented on every request.
    """
    try:
        data = request.get_json(silent=True) or {}
        token = data.get('pwd')
        if not token:
            return construct_response(
                data={"authenticated": False},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Token is required",
            )
        if _token_is_valid(token):
            return construct_response(
                data={"authenticated": True},
                retmsg="Authentication successful",
            )
        return construct_response(
            data={"authenticated": False},
            retcode=RetCode.UNAUTHORIZED,
            retmsg="Invalid token",
        )
    except Exception:
        logger.exception(f"Login failed")
        return construct_response(
            data={"authenticated": False},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="Login failed",
        )


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
