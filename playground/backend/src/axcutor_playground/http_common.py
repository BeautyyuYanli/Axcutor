"""Shared HTTP wire rules for execution APIs and the workspace workbench.

Business JSON always crosses the lossless runtime codec. Execution IDs are any
nonempty string; routing and Location encoding must preserve slashes and dots.
These adapters own no execution, workspace or Worker state.
"""

import logging
from typing import Any
from urllib.parse import quote

from axcutor_host import HostError, InvalidRequest
from axcutor_runtime.bundle import CompilationError
from axcutor_runtime.codec import dumps, loads
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

STATUS = {
    "invalid_request": 400,
    "execution_not_found": 404,
    "execution_already_exists": 409,
    "signal_not_waiting": 409,
    "input_validation_failed": 422,
    "signal_validation_failed": 422,
    "runtime_unavailable": 503,
    "workspace_conflict": 409,
}


class _ExecutionIDConvertor(PathConvertor):
    # IDs can contain encoded slashes, newlines and other nonempty text.
    regex = r"[\s\S]+"


register_url_convertor("execution_id", _ExecutionIDConvertor())


def execution_location(execution_id: str) -> str:
    # quote() leaves unreserved dots intact. Escape them too so '.' and '..'
    # survive HTTP client path normalization.
    return "/v1/executions/" + quote(execution_id, safe="").replace(".", "%2E")


def json_response(
    value: object, status: int = 200, headers: dict[str, str] | None = None
) -> Response:
    return Response(
        dumps(value), status_code=status, headers=headers, media_type="application/json"
    )


def error_response(code: str, message: str, status: int, details: object = None) -> Response:
    return json_response({"error": {"code": code, "message": message, "details": details}}, status)


class InvalidJSON(Exception):
    pass


async def read_body(request: Request) -> dict[str, Any]:
    """Reject malformed JSON and non-object bodies before contacting Temporal."""
    try:
        body = loads((await request.body()).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError, OverflowError) as error:
        raise InvalidJSON(str(error)) from error
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be an object")
    return body


async def invalid_json(request: Request, error: Exception) -> Response:
    return error_response("invalid_json", str(error), 400)


async def host_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, HostError)
    return error_response(error.code, str(error), STATUS[error.code], error.details)


async def compilation_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, CompilationError)
    return error_response("compilation_failed", str(error), 422, {"diagnostics": error.diagnostics})


async def http_error(request: Request, error: Exception) -> Response:
    assert isinstance(error, HTTPException)
    return error_response("invalid_request", str(error.detail), error.status_code)


async def unexpected_error(request: Request, error: Exception) -> Response:
    logging.getLogger(__name__).error("HTTP request failed", exc_info=error)
    return error_response("internal_error", "internal server error", 500)


EXCEPTION_HANDLERS = {
    InvalidJSON: invalid_json,
    HostError: host_error,
    CompilationError: compilation_error,
    HTTPException: http_error,
    Exception: unexpected_error,
}
