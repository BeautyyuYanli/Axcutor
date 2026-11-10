"""Independent workspace HTTP, Execution controls and static assets.

The serve command never installs these writable routes. Business input is read
from workspace files using the Python codec, never parsed by the browser or an
HTTP framework's JSON conversion. OpenAPI here is the workbench SDK source.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from axcutor_host.http import openapi_schema as execution_schema
from axcutor_host.http import signal_execution
from axcutor_host.service import HostError, InvalidRequest
from axcutor_runtime.bundle import CompilationError
from axcutor_runtime.codec import dumps, loads
from starlette.applications import Starlette
from starlette.convertors import PathConvertor, register_url_convertor
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from .coordinator import Playground

_STATUS = {
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
    # Match the Execution API's arbitrary nonempty IDs, including slashes/newlines.
    regex = r"[\s\S]+"


register_url_convertor("playground_execution_id", _ExecutionIDConvertor())


def _json(value: object, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        dumps(value), status_code=status, headers=headers, media_type="application/json"
    )


def _error(code: str, message: str, status: int, details: object = None) -> Response:
    return _json({"error": {"code": code, "message": message, "details": details}}, status)


def openapi_schema() -> dict[str, Any]:
    """Extend the Execution schemas without changing their wire contract."""
    schema: dict[str, Any] = execution_schema()
    schema["info"]["title"] = "Axcutor Playground API"
    paths = schema["paths"]
    base_responses = paths.pop("/v1/executions")["post"]["responses"]
    errors = {code: body for code, body in base_responses.items() if code != "202"}
    errors["409"]["description"] = (
        "Workspace revision conflict, Playground busy, or signal target is not waiting"
    )
    schemas = schema["components"]["schemas"]

    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }

    text = {"type": "string"}
    path = {"type": "string", "minLength": 1, "description": "Relative workspace source path"}
    schemas.update(
        {
            "WorkspaceFileEdit": obj({"path": path, "text": text}, ["path", "text"]),
            "WorkspaceFile": obj(
                {"path": path, "text": text, "generated": {"type": "boolean"}},
                ["path", "text", "generated"],
            ),
            "Workspace": obj(
                {
                    "revision": text,
                    "files": {"type": "array", "items": ref("WorkspaceFile")},
                    "busy": {
                        "type": "boolean",
                        "description": "Execution or local cleanup in flight",
                    },
                    "execution_id": {"type": ["string", "null"]},
                },
                ["revision", "files", "busy", "execution_id"],
            ),
            "SaveWorkspaceRequest": obj(
                {
                    "revision": text,
                    "files": {"type": "array", "items": ref("WorkspaceFileEdit")},
                },
                ["revision", "files"],
            ),
            "RunWorkspaceRequest": obj(
                {
                    "revision": text,
                    "entry": path,
                    "input": {"anyOf": [path, {"type": "null"}]},
                },
                ["revision", "entry"],
            ),
            "SubmittedSource": obj(
                {
                    "entry": text,
                    "mounts": {"type": "object", "additionalProperties": ref("MountUnit")},
                },
                ["entry", "mounts"],
            ),
            "WorkspaceExecution": obj(
                {
                    "execution": ref("Execution"),
                    "source": ref("SubmittedSource"),
                    "workspace": ref("Workspace"),
                },
                ["execution", "source", "workspace"],
            ),
            "TraceError": obj({"type": text, "message": text}, ["type", "message"]),
            "TraceEvent": {
                "type": "object",
                "additionalProperties": True,
                "properties": {"kind": text, "at": text},
                "required": ["kind", "at"],
            },
            "TraceSpan": obj(
                {
                    "id": text,
                    "parent_id": {"type": ["string", "null"]},
                    "kind": text,
                    "address": text,
                    "frame_kind": {"type": "string", "enum": ["function", "iteration"]},
                    "cursor": text,
                    "started_at": text,
                    "finished_at": {"type": ["string", "null"]},
                    "status": {
                        "type": "string",
                        "enum": ["running", "completed", "failed", "cancelled"],
                    },
                    "input": {"type": "object", "additionalProperties": True},
                    "output": {"type": ["object", "null"], "additionalProperties": True},
                    "branch": {"type": ["string", "null"]},
                    "error": {"anyOf": [ref("TraceError"), {"type": "null"}]},
                    "attempt": {"type": "integer"},
                    "events": {"type": "array", "items": ref("TraceEvent")},
                },
                [
                    "id",
                    "parent_id",
                    "kind",
                    "address",
                    "frame_kind",
                    "cursor",
                    "started_at",
                    "finished_at",
                    "status",
                    "input",
                    "output",
                    "branch",
                    "error",
                    "events",
                ],
            ),
            "LatestTrace": obj(
                {
                    "execution_id": text,
                    "execution": {"anyOf": [ref("Execution"), {"type": "null"}]},
                    "source": ref("SubmittedSource"),
                    "spans": {"type": "array", "items": ref("TraceSpan")},
                    "observed_at": {"type": ["string", "null"]},
                    "complete": {"type": "boolean"},
                    "trace_error": {"type": ["string", "null"]},
                },
                [
                    "execution_id",
                    "execution",
                    "source",
                    "spans",
                    "observed_at",
                    "complete",
                    "trace_error",
                ],
            ),
            "LatestTraceResponse": {"anyOf": [ref("LatestTrace"), {"type": "null"}]},
        }
    )

    def response(model: str) -> dict[str, Any]:
        return {"description": model, "content": {"application/json": {"schema": ref(model)}}}

    def operation(operation_id: str, result: str, request: str | None = None) -> dict[str, Any]:
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "responses": {
                **errors,
                "202" if result == "WorkspaceExecution" else "200": response(result),
            },
        }
        if request:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ref(request)}},
            }
        return operation

    paths["/v1/workspace"] = {
        "get": operation("getWorkspace", "Workspace"),
        "put": operation("saveWorkspace", "Workspace", "SaveWorkspaceRequest"),
    }
    paths["/v1/workspace/executions"] = {
        "post": operation("runWorkspace", "WorkspaceExecution", "RunWorkspaceRequest"),
    }
    paths["/v1/workspace/trace"] = {"get": operation("getLatestTrace", "LatestTraceResponse")}
    paths["/v1/workspace/trace"]["get"]["parameters"] = [
        {
            "name": "execution_id",
            "in": "query",
            "required": False,
            "schema": text,
            "description": "If supplied, reject a replaced trace rather than returning another run",
        }
    ]
    return schema


def create_playground_app(playground: Playground, static: Path | None = None) -> Starlette:
    """Own volume/Worker lifecycle for exactly one ASGI server process."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await playground.start()
            yield
        finally:
            await playground.close()

    async def schema(request: Request) -> Response:
        return _json(openapi_schema())

    async def workspace(request: Request) -> Response:
        if request.method == "GET":
            return _json(playground.view())
        body = await read_body(request)
        if body.keys() != {"revision", "files"}:
            raise InvalidRequest("revision and files are required")
        return _json(await playground.save(body["revision"], body["files"]))

    async def submit(request: Request) -> Response:
        body = await read_body(request)
        if not {"revision", "entry"} <= body.keys() or body.keys() - {"revision", "entry", "input"}:
            raise InvalidRequest("entry, revision and optional input are required")
        try:
            result = await playground.submit(body["entry"], body.get("input"), body["revision"])
        except CompilationError as error:
            return _error(
                "compilation_failed",
                str(error),
                422,
                {
                    "diagnostics": error.diagnostics,
                    "workspace": playground.view(),
                },
            )
        except HostError as error:
            # Contracts can change before input validation fails. Return that
            # revision too, but never adopt another page's conflicting write.
            details = error.details
            if error.code != "workspace_conflict":
                details = {**(details or {}), "workspace": playground.view()}
            return _error(error.code, str(error), _STATUS[error.code], details)
        location = "/v1/executions/" + quote(result["execution"]["execution_id"], safe="")
        return _json(result, 202, {"Location": location})

    async def invalid_json(request: Request, error: Exception) -> Response:
        return _error("invalid_json", str(error), 400)

    async def get(request: Request) -> Response:
        return _json(await playground.executions.get(request.path_params["execution_id"]))

    async def trace(request: Request) -> Response:
        latest = playground.latest
        expected = request.query_params.get("execution_id")
        if expected and (latest is None or latest["execution_id"] != expected):
            return _error("execution_not_found", "This trace has been replaced or cleared", 404)
        return _json(latest)

    async def cancel(request: Request) -> Response:
        execution = await playground.executions.cancel(request.path_params["execution_id"])
        return _json(execution, 202 if execution["status"] == "running" else 200)

    async def signal(request: Request) -> Response:
        return await signal_execution(request, playground.executions)

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
        logging.getLogger(__name__).error("Playground HTTP request failed", exc_info=error)
        return _error("internal_error", "internal server error", 500)

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/openapi.json", schema, methods=["GET"]),
            Route("/v1/workspace", workspace, methods=["GET", "PUT"]),
            Route("/v1/workspace/executions", submit, methods=["POST"]),
            Route("/v1/workspace/trace", trace, methods=["GET"]),
            Route(
                "/v1/executions/{execution_id:playground_execution_id}:cancel",
                cancel,
                methods=["POST"],
            ),
            Route(
                "/v1/executions/{execution_id:playground_execution_id}:signal",
                signal,
                methods=["POST"],
            ),
            Route("/v1/executions/{execution_id:playground_execution_id}", get, methods=["GET"]),
        ],
        exception_handlers={
            InvalidJSON: invalid_json,
            HostError: host_error,
            CompilationError: compilation_error,
            HTTPException: http_error,
            Exception: unexpected_error,
        },
    )
    app.router.redirect_slashes = False
    if static:
        app.mount("/", StaticFiles(directory=static, html=True))
    return app


class InvalidJSON(Exception):
    pass


async def read_body(request: Request) -> dict[str, Any]:
    try:
        body = loads((await request.body()).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidJSON(str(error)) from error
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be an object")
    return body


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export the Playground API OpenAPI schema")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.write_text(dumps(openapi_schema()) + "\n", encoding="utf-8")
