"""Thin ASGI controller; all JSON crosses the lossless runtime codec.

Request models deliberately avoid framework JSON/Pydantic conversion: ordinary
float decoding would round user values before the TypeSpec validator sees them.
The injected service owns execution behavior; the app owns no execution state.
"""

import logging
from urllib.parse import quote

from axcutor_runtime.bundle import CompilationError
from axcutor_runtime.codec import dumps, loads
from starlette.applications import Starlette
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .service import ExecutionService, HostError, InvalidRequest

_STATUS = {
    "invalid_request": 400,
    "execution_not_found": 404,
    "execution_already_exists": 409,
    "input_validation_failed": 422,
    "runtime_unavailable": 503,
}


class _ExecutionIDConvertor(PathConvertor):
    # IDs are arbitrary nonempty strings, including encoded slashes/newlines.
    regex = r"[\s\S]+"


register_url_convertor("execution_id", _ExecutionIDConvertor())


def _json(value: object, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        dumps(value), status_code=status, headers=headers, media_type="application/json"
    )


def _error(code: str, message: str, status: int, details: object = None) -> Response:
    return _json({"error": {"code": code, "message": message, "details": details}}, status)


def create_app(service: ExecutionService) -> Starlette:
    """Build the three execution routes around one reusable service instance."""

    async def submit(request: Request) -> Response:
        try:
            body = loads((await request.body()).decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as error:
            return _error("invalid_json", str(error), 400)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be an object")
        if not {"entry", "sources"} <= body.keys():
            raise InvalidRequest("entry and sources are required")
        if body.keys() - {"entry", "sources", "input", "execution_id"}:
            raise InvalidRequest("unknown request fields")
        if "input" in body and not isinstance(body["input"], dict):
            raise InvalidRequest("input must be an object")
        if "execution_id" in body and (
            not isinstance(body["execution_id"], str) or not body["execution_id"]
        ):
            raise InvalidRequest("execution_id must be a nonempty string")
        execution = await service.submit(
            body["entry"], body["sources"], body.get("input", {}), body.get("execution_id")
        )
        # quote() leaves unreserved dots intact. Escape them too so IDs such as
        # '.' and '..' survive the HTTP client's path normalization.
        location = "/v1/executions/" + quote(execution["execution_id"], safe="").replace(".", "%2E")
        return _json(execution, 202, {"Location": location})

    async def get(request: Request) -> Response:
        return _json(await service.get(request.path_params["execution_id"]))

    async def cancel(request: Request) -> Response:
        execution = await service.cancel(request.path_params["execution_id"])
        return _json(execution, 202 if execution["status"] == "running" else 200)

    async def host_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HostError)
        return _error(error.code, str(error), _STATUS[error.code], error.details)

    async def compilation_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, CompilationError)
        return _error("compilation_failed", str(error), 422, {"diagnostics": error.diagnostics})

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        return _error("invalid_request", str(error.detail), error.status_code)

    async def unexpected_error(request: Request, error: Exception) -> Response:
        logging.getLogger(__name__).error("HTTP request failed", exc_info=error)
        return _error("internal_error", "internal server error", 500)

    app = Starlette(
        routes=[
            Route("/v1/executions", submit, methods=["POST"]),
            Route("/v1/executions/{execution_id:execution_id}:cancel", cancel, methods=["POST"]),
            Route("/v1/executions/{execution_id:execution_id}", get, methods=["GET"]),
        ],
        exception_handlers={
            HostError: host_error,
            CompilationError: compilation_error,
            HTTPException: http_error,
            Exception: unexpected_error,
        },
    )
    app.router.redirect_slashes = False
    return app
