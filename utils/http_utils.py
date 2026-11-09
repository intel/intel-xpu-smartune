# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import ssl

from flask import jsonify, make_response, request
from enum import Enum, IntEnum
from werkzeug.serving import WSGIRequestHandler

MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Seconds any single read or write on a client connection may block: the TLS
# handshake, an idle keep-alive wait, a client not reading an event stream. An
# event stream itself stays open indefinitely; only a stalled peer is dropped.
CLIENT_SOCKET_TIMEOUT = 30


class DeferredHandshakeSSLContext(ssl.SSLContext):
    """Server TLS context whose handshake runs in the request thread.

    werkzeug wraps the listening socket with this context, and by default
    accept() then performs the TLS handshake -- on the server's single accept
    thread. One client that connects and never sends its TLS hello (a port
    scanner, a browser's speculative pre-connect, a proxy that gave up) stalls
    every other request until it disconnects. Accepted sockets made with
    do_handshake_on_connect=False return at once; QuietWSGIRequestHandler then
    completes the handshake in its own thread, under CLIENT_SOCKET_TIMEOUT.
    """

    def wrap_socket(self, sock, *args, **kwargs):
        kwargs["do_handshake_on_connect"] = False
        return super().wrap_socket(sock, *args, **kwargs)


class QuietWSGIRequestHandler(WSGIRequestHandler):
    """Use a product-only Server header for Werkzeug responses, and finish the
    TLS handshake that DeferredHandshakeSSLContext moved off the accept thread."""

    timeout = CLIENT_SOCKET_TIMEOUT  # applied to the connection in setup()

    def version_string(self):
        return "SmarTune"

    def handle(self):
        if isinstance(self.connection, ssl.SSLSocket):
            try:
                self.connection.do_handshake()
            except OSError:
                # Timed out, reset, not TLS, or the browser rejected our
                # self-signed certificate: nothing to serve, nobody else waits.
                return
        super().handle()


class CustomEnum(Enum):
    @classmethod
    def valid(cls, value):
        try:
            cls(value)
            return True
        except BaseException:
            return False

    @classmethod
    def values(cls):
        return [member.value for member in cls.__members__.values()]

    @classmethod
    def names(cls):
        return [member.name for member in cls.__members__.values()]

class RetCode(IntEnum, CustomEnum):
    SUCCESS = 0
    NOT_EFFECTIVE = 10
    EXCEPTION_ERROR = 100
    ARGUMENT_ERROR = 101
    DATA_ERROR = 102
    OPERATING_ERROR = 103
    CONNECTION_ERROR = 105
    RUNNING = 106
    PERMISSION_ERROR = 108
    AUTHENTICATION_ERROR = 109
    UNAUTHORIZED = 401
    NOT_EXISTING = 404
    CONFLICT = 409
    TOO_MANY_REQUESTS = 429
    SERVER_ERROR = 500

def get_json_result(retcode=RetCode.SUCCESS, retmsg='success',
                    data=None, job_id=None, meta=None):
    result_dict = {
        "retcode": retcode,
        "retmsg": retmsg,
        "data": data,
    }

    response = {}
    for key, value in result_dict.items():
        if value is None and key != "retcode":
            continue
        else:
            response[key] = value
    return jsonify(response)


def construct_response(retcode=RetCode.SUCCESS,
                       retmsg='success', data=None, auth=None):
    result_dict = {"retcode": retcode, "retmsg": retmsg, "data": data}
    response_dict = {}
    for key, value in result_dict.items():
        if value is None and key != "retcode":
            continue
        else:
            response_dict[key] = value
    response = make_response(jsonify(response_dict))
    if auth:
        response.headers["Authorization"] = auth
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Method"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response


def apply_request_limits(app, max_bytes=MAX_REQUEST_BYTES):
    """Bound request-body size app-wide and return a generic JSON 413."""
    app.config["MAX_CONTENT_LENGTH"] = max_bytes

    def _too_large_response():
        return make_response(
            get_json_result(
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="Request body exceeds the maximum allowed size.",
            ),
            413,
        )

    @app.before_request
    def _reject_oversized_body():
        length = request.content_length
        if length is not None and length > max_bytes:
            return _too_large_response()
        return None

    @app.errorhandler(413)
    def _payload_too_large(_err):
        return _too_large_response()
