#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials, 
#  and your use of them is governed by the express license under which they 
#  were provided to you ("License"). Unless the License provides otherwise, 
#  you may not use, modify, copy, publish, distribute, disclose or transmit 
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express 
#  or implied warranties, other than those that are expressly stated in the License.
#

from flask import jsonify, make_response
from enum import Enum, IntEnum

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
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response
