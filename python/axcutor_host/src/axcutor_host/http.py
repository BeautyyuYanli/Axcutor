"""Thin ASGI controller; all JSON crosses the lossless runtime codec.

Request models deliberately avoid framework JSON/Pydantic conversion: ordinary
float decoding would round user values before the TypeSpec validator sees them.
The injected service owns execution behavior; the app owns no execution state.
"""

import logging
from pathlib import Path
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
    "signal_not_waiting": 409,
    "input_validation_failed": 422,
    "signal_validation_failed": 422,
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


def openapi_schema() -> dict[str, object]:
    """Describe the same lossless JSON wire contract used by the ASGI routes.

    Arbitrary business JSON is deliberately untyped here. Clients must retain
    raw input, frame variables and output instead of converting through floats.
    Consumers can generate clients from this schema.
    """

    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    schemas: dict[str, object] = {
        "SourceDocument": {
            "type": "object",
            "required": ["format", "text"],
            "additionalProperties": False,
            "properties": {
                "format": {"type": "string", "enum": ["yaml", "json"]},
                "text": {"type": "string"},
            },
        },
        "SubmitExecutionRequest": {
            "type": "object",
            "required": ["entry", "packages"],
            "additionalProperties": False,
            "properties": {
                "entry": {
                    "type": "string",
                    "minLength": 1,
                    "description": "__main__ or a public absolute dotted package path.",
                },
                "packages": {
                    "type": "object",
                    "additionalProperties": ref("SourceDocument"),
                    "description": (
                        "Dotted package names to workflow documents; "
                        "no Python files or Worker mount configuration."
                    ),
                },
                "input": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Lossless JSON object; preserve number tokens and key order.",
                },
                "execution_id": {"type": "string", "minLength": 1},
            },
        },
        "Frame": {
            "type": "object",
            "required": [
                "kind",
                "address",
                "cursor",
                "waiting",
                "call_depth",
                "steps",
                "max_steps",
                "variables",
                "children",
            ],
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["function", "block", "iteration"]},
                "address": {"type": "string"},
                "cursor": {"type": "string"},
                "waiting": {"type": "array", "items": {"type": "string"}},
                "call_depth": {"type": "integer", "minimum": 1},
                "steps": {"type": "integer", "minimum": 0},
                "max_steps": {"type": ["integer", "null"]},
                "variables": {"type": "object", "additionalProperties": True},
                "children": {"type": "array", "items": ref("Frame")},
            },
        },
        "SignalExecutionRequest": {
            "type": "object",
            "required": ["address"],
            "additionalProperties": False,
            "properties": {
                "address": {"type": "string", "minLength": 1},
                "branch": {"type": ["string", "null"], "minLength": 1},
                "output": {"type": "object", "additionalProperties": True, "default": {}},
                "step": {"type": ["integer", "null"], "minimum": 1},
            },
        },
        "ExecutionResult": {
            "type": "object",
            "required": ["branch", "output"],
            "additionalProperties": False,
            "properties": {
                "branch": {"type": ["string", "null"]},
                "output": {"type": "object", "additionalProperties": True},
            },
        },
        "ExecutionError": {
            "type": "object",
            "required": ["category", "type", "message"],
            "additionalProperties": False,
            "properties": {
                "category": {"type": "string", "enum": ["recoverable", "panic"]},
                "type": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        "Execution": {
            "type": "object",
            "required": [
                "execution_id",
                "status",
                "started_at",
                "finished_at",
                "frames",
                "result",
                "error",
            ],
            "additionalProperties": False,
            "properties": {
                "execution_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["running", "completed", "failed", "cancelled"],
                },
                "started_at": {"type": "string", "format": "date-time"},
                "finished_at": {"type": ["string", "null"], "format": "date-time"},
                "frames": {"type": "array", "items": ref("Frame")},
                "result": {"anyOf": [ref("ExecutionResult"), {"type": "null"}]},
                "error": {"anyOf": [ref("ExecutionError"), {"type": "null"}]},
            },
        },
        "Diagnostic": {
            "type": "object",
            "required": [
                "workflow_id",
                "scope_graph_path",
                "target_kind",
                "node_id",
                "branch_id",
                "edge_index",
                "param_path",
                "code",
                "message",
                "source_location",
            ],
            "properties": {
                "workflow_id": {"type": "string"},
                "scope_graph_path": {"type": "array", "items": {"type": "string"}},
                "target_kind": {"type": "string"},
                "node_id": {"type": ["string", "null"]},
                "branch_id": {"type": ["string", "null"]},
                "edge_index": {"type": ["integer", "null"]},
                "param_path": {"type": "string"},
                "code": {"type": "string"},
                "message": {"type": "string"},
                "source_location": {
                    "type": ["object", "null"],
                    "required": ["line", "column"],
                    "properties": {"line": {"type": "integer"}, "column": {"type": "integer"}},
                },
            },
        },
        "ErrorResponse": {
            "type": "object",
            "required": ["error"],
            "additionalProperties": False,
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "details"],
                    "additionalProperties": False,
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {
                            "description": "Optional details; compilation_failed has diagnostics.",
                            "anyOf": [
                                {"type": "null"},
                                {
                                    "type": "object",
                                    "additionalProperties": True,
                                    "properties": {
                                        "diagnostics": {
                                            "type": "array",
                                            "items": ref("Diagnostic"),
                                        }
                                    },
                                },
                            ],
                        },
                    },
                },
            },
        },
    }

    def response(name: str, description: str) -> dict[str, object]:
        return {
            "description": description,
            "content": {"application/json": {"schema": ref(name)}},
        }

    errors: dict[str, object] = {
        str(status): response("ErrorResponse", description)
        for status, description in {
            400: "Invalid JSON or request",
            404: "Execution not found",
            405: "Method not allowed",
            409: "Execution ID already exists or signal target is not waiting",
            422: "Compilation, input or signal validation failed",
            503: "Runtime unavailable",
            500: "Internal server error",
        }.items()
    }
    parameter = {
        "name": "execution_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "minLength": 1},
    }

    def responses(success: dict[str, object]) -> dict[str, object]:
        return {**errors, **success}

    return {
        "openapi": "3.1.0",
        "info": {"title": "Axcutor Execution API", "version": "0.1.0"},
        "paths": {
            "/v1/executions": {
                "post": {
                    "operationId": "submitExecution",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": ref("SubmitExecutionRequest")}},
                    },
                    "responses": responses(
                        {
                            "202": {
                                **response("Execution", "Submitted; may already be terminal"),
                                "headers": {"Location": {"schema": {"type": "string"}}},
                            },
                        }
                    ),
                }
            },
            "/v1/executions/{execution_id}": {
                "get": {
                    "operationId": "getExecution",
                    "parameters": [parameter],
                    "responses": responses({"200": response("Execution", "Current execution")}),
                }
            },
            "/v1/executions/{execution_id}:cancel": {
                "post": {
                    "operationId": "cancelExecution",
                    "parameters": [parameter],
                    "responses": responses(
                        {
                            "200": response("Execution", "Terminal execution"),
                            "202": response("Execution", "Cooperative cleanup is still running"),
                        }
                    ),
                }
            },
            "/v1/executions/{execution_id}:signal": {
                "post": {
                    "operationId": "signalExecution",
                    "parameters": [parameter],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": ref("SignalExecutionRequest")}},
                    },
                    "responses": responses(
                        {
                            "200": response("Execution", "Terminal execution after delivery"),
                            "202": response("Execution", "Delivered; execution is still running"),
                        }
                    ),
                }
            },
        },
        "components": {"schemas": schemas},
    }


async def signal_execution(request: Request, service: ExecutionService) -> Response:
    """Shared Execution/Playground signal controller with strict, lossless JSON."""
    try:
        body = loads((await request.body()).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError, OverflowError) as error:
        return _error("invalid_json", str(error), 400)
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be an object")
    if "address" not in body:
        raise InvalidRequest("address is required")
    if body.keys() - {"address", "branch", "output", "step"}:
        raise InvalidRequest("unknown request fields")
    if "output" in body and not isinstance(body["output"], dict):
        raise InvalidRequest("output must be an object")
    execution = await service.signal(
        request.path_params["execution_id"],
        body["address"],
        body.get("output", {}),
        body.get("branch"),
        body.get("step"),
    )
    return _json(execution, 202 if execution["status"] == "running" else 200)


def create_app(service: ExecutionService) -> Starlette:
    """Build execution routes and their stateless schema around one service."""

    async def schema(request: Request) -> Response:
        return _json(openapi_schema())

    async def submit(request: Request) -> Response:
        try:
            body = loads((await request.body()).decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as error:
            return _error("invalid_json", str(error), 400)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be an object")
        if not {"entry", "packages"} <= body.keys():
            raise InvalidRequest("entry and packages are required")
        if body.keys() - {"entry", "packages", "input", "execution_id"}:
            raise InvalidRequest("unknown request fields")
        if "input" in body and not isinstance(body["input"], dict):
            raise InvalidRequest("input must be an object")
        if "execution_id" in body and (
            not isinstance(body["execution_id"], str) or not body["execution_id"]
        ):
            raise InvalidRequest("execution_id must be a nonempty string")
        execution = await service.submit(
            body["entry"], body["packages"], body.get("input", {}), body.get("execution_id")
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

    async def signal(request: Request) -> Response:
        return await signal_execution(request, service)

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
            Route("/openapi.json", schema, methods=["GET"]),
            Route("/v1/executions", submit, methods=["POST"]),
            Route("/v1/executions/{execution_id:execution_id}:cancel", cancel, methods=["POST"]),
            Route("/v1/executions/{execution_id:execution_id}:signal", signal, methods=["POST"]),
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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export the execution API OpenAPI 3.1 schema")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.write_text(dumps(openapi_schema()) + "\n", encoding="utf-8")
