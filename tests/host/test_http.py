"""Exercise routing, strict JSON and error envelopes with the actual service."""

import asyncio
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import uvicorn
from axcutor_host import ExecutionService
from axcutor_host.http import create_app
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, N, assign, graph
from temporalio.client import Client


def request_body(*, inputs=None):
    doc = graph(
        {"copy": assign("n", "value")},
        ["start -> copy", "copy -> end"],
        inputs={"n": INTEGER},
        outputs={"value": INTEGER},
    )
    return {
        "entry": "main.yaml",
        "sources": {"main.yaml": {"format": "json", "text": dumps(doc)}},
        "input": {"n": 7} if inputs is None else inputs,
    }


def client_for(client, queue=None, timeout=0.15):
    service = ExecutionService(
        client,
        queue or str(uuid4()),
        rpc_timeout=timedelta(seconds=timeout),
        options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000},
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
    )


@asynccontextmanager
async def live_client(service):
    """Use real HTTP parsing/URL normalization as well as the Temporal backend."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(service), log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("HTTP server exited before startup")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                yield client
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    error = loads(response.text)["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code and isinstance(error["message"], str)
    return error


@pytest.mark.parametrize(
    "text",
    [
        b"",
        b"{",
        b"\xff",
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":-Infinity}',
        b'{"entry":"a","entry":"b"}',
        b'{"nested":{"n":1,"n":2}}',
        b"{}{}",
    ],
)
async def test_invalid_json_is_strict_and_precedes_backend(text):
    backend = await Client.connect("127.0.0.1:1", lazy=True)
    async with client_for(backend) as client:
        assert_error(await client.post("/v1/executions", content=text), 400, "invalid_json")


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        1,
        {},
        {"entry": "main"},
        {"entry": "main", "sources": []},
        {"entry": 1, "sources": {}},
        {"entry": "/main", "sources": {}},
        {"entry": "../main", "sources": {}},
        {"entry": "main", "sources": {}, "input": None},
        {"entry": "main", "sources": {}, "execution_id": None},
        {"entry": "main", "sources": {}, "execution_id": ""},
        {"entry": "main", "sources": {}, "unknown": 1},
        {"entry": "main", "sources": {"main": {"format": "xml", "text": ""}}},
        {"entry": "main", "sources": {"main": {"format": [], "text": ""}}},
        {"entry": "main", "sources": {"main": {"format": "yaml", "text": 1}}},
    ],
)
async def test_invalid_request_shapes_are_rejected_without_backend(body):
    backend = await Client.connect("127.0.0.1:1", lazy=True)
    async with client_for(backend) as client:
        assert_error(
            await client.post("/v1/executions", content=dumps(body)), 400, "invalid_request"
        )


async def test_compilation_input_and_backend_errors_use_distinct_contracts():
    backend = await Client.connect("127.0.0.1:1", lazy=True)
    async with client_for(backend) as client:
        body = request_body()
        body["sources"]["main.yaml"]["text"] = "input: {}\noutput: {}\ngraph: {}"
        error = assert_error(
            await client.post("/v1/executions", content=dumps(body)), 422, "compilation_failed"
        )
        assert error["details"]["diagnostics"]
        assert "code" in error["details"]["diagnostics"][0]
        assert_error(
            await client.post(
                "/v1/executions", content=dumps(request_body(inputs={"n": Decimal("1.0")}))
            ),
            422,
            "input_validation_failed",
        )
        assert_error(
            await client.post("/v1/executions", content=dumps(request_body())),
            503,
            "runtime_unavailable",
        )


async def test_not_found_and_no_worker_have_bounded_json_errors(server):
    async with client_for(server.client) as client:
        for method, url in [
            (client.get, f"/v1/executions/{uuid4()}"),
            (client.post, f"/v1/executions/{uuid4()}:cancel"),
        ]:
            assert_error(await method(url), 404, "execution_not_found")
        assert_error(await client.get("/unknown"), 404, "invalid_request")
        assert_error(await client.get("/v1/executions"), 405, "invalid_request")
        body = request_body()
        body["execution_id"] = str(uuid4())
        assert_error(
            await asyncio.wait_for(client.post("/v1/executions", content=dumps(body)), 3),
            503,
            "runtime_unavailable",
        )
        assert_error(
            await client.get(f"/v1/executions/{body['execution_id']}"), 503, "runtime_unavailable"
        )
        await server.client.get_workflow_handle(body["execution_id"]).terminate()


@pytest.mark.parametrize(
    "execution_id",
    [".", "..", "a/../b", "line\nid", "nul\x00id", "with\\slash", "%2E", "订单 / % ? # :cancel"],
)
async def test_live_routes_conflicts_cancellation_and_terminal_idempotency(server, execution_id):
    queue = str(uuid4())
    entered, cleaned, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work(request, signal):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert signal.aborted
            await release_cleanup.wait()
            cleaned.set()

    body = request_body()
    body["execution_id"] = execution_id
    body["sources"]["main.yaml"]["text"] = dumps(
        {
            "input": {"n": INTEGER},
            "output": {},
            "run": {"executor": "builtin", "content": "wait"},
        }
    )
    async with create_worker(server.client, queue, {"builtin": work}):
        try:
            host = ExecutionService(
                server.client,
                queue,
                options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000},
            )
            async with live_client(host) as client:
                submitted = await client.post("/v1/executions", content=dumps(body))
                assert submitted.status_code == 202, submitted.text
                location = submitted.headers["location"]
                assert "%" in location and loads(submitted.text)["execution_id"] == execution_id
                await asyncio.wait_for(entered.wait(), 5)
                response = await client.get(location)
                assert response.status_code == 200, response.text
                live = loads(response.text)
                assert live["execution_id"] == execution_id
                assert live["status"] == "running" and live["frames"][0]["variables"] == {"n": 7}
                assert_error(
                    await client.post("/v1/executions", content=dumps(body)),
                    409,
                    "execution_already_exists",
                )
                cancelled = await client.post(location + ":cancel")
                assert cancelled.status_code == 202
                assert loads(cancelled.text)["status"] == "running"
                assert loads(cancelled.text)["execution_id"] == execution_id
                release_cleanup.set()
                async with asyncio.timeout(5):
                    while True:
                        response = await client.get(location)
                        if loads(response.text)["status"] != "running":
                            break
                        await asyncio.sleep(0.02)
                final = loads(response.text)
                assert final["status"] == "cancelled" and final["frames"] == []
                assert final["error"] is final["result"] is None and cleaned.is_set()
                again = await client.post(location + ":cancel")
                assert again.status_code == 200 and loads(again.text) == final
        finally:
            release_cleanup.set()


async def test_http_roundtrip_preserves_large_integer_decimal_and_map_order(server):
    queue = str(uuid4())
    schema = {
        "type": "object",
        "properties": {
            "integer": INTEGER,
            "decimal": N,
            "map": {"type": "object", "additionalProperties": INTEGER},
        },
    }
    doc = graph(
        {"copy": assign("value", "result")},
        ["start -> copy", "copy -> end"],
        inputs={"value": schema},
        outputs={"result": schema},
    )
    value = {
        "integer": 10**100,
        "decimal": Decimal("1.00000000000000000000001"),
        "map": {"b": 2, "a": 1},
    }
    body = {
        "entry": "main.yaml",
        "sources": {"main.yaml": {"format": "json", "text": dumps(doc)}},
        "input": {"value": value},
    }
    async with create_worker(server.client, queue):
        async with client_for(server.client, queue, timeout=5) as client:
            response = await client.post("/v1/executions", content=dumps(body))
            assert response.status_code == 202
            location = response.headers["location"]
            async with asyncio.timeout(5):
                while True:
                    response = await client.get(location)
                    final = loads(response.text)
                    if final["status"] == "completed":
                        break
                    await asyncio.sleep(0.02)
            assert dumps(final["result"]["output"]["result"]) == dumps(value)
            assert final["result"]["branch"] is None and final["frames"] == []
            assert (await client.post(location + ":cancel")).status_code == 200
