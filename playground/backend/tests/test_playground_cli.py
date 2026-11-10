"""Exercise the installed API console entry with real HTTP and external Workers."""

import asyncio
import signal
import socket
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from axcutor_host import AxcutorHost
from axcutor_playground import cli
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from helpers import assign, graph


@pytest.mark.parametrize("command, port", [([], 8080), (["api"], 8000)])
@pytest.mark.parametrize("environment", [False, True])
def test_server_entry_defaults_and_environment(monkeypatch, command, port, environment):
    for name, value in (
        ("TEMPORAL_ADDRESS", "environment.example:7233"),
        ("TEMPORAL_NAMESPACE", "environment-namespace"),
        ("AXCUTOR_TASK_QUEUE", "environment-queue"),
    ):
        if environment:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    seen = []

    async def run(args):
        seen.append(args)

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(sys, "argv", ["axcutor-playground", *command])
    cli.main()
    [args] = seen
    assert args.address == ("environment.example:7233" if environment else "localhost:7233")
    assert args.namespace == ("environment-namespace" if environment else "default")
    assert args.host == "127.0.0.1" and args.port == port
    if command:
        assert args.task_queue == ("environment-queue" if environment else "axcutor")


@pytest.mark.parametrize("position", ["before", "after", "split", "repeated"])
def test_api_preserves_explicit_options_on_either_side_of_command(monkeypatch, position):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "environment.example:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "environment-namespace")
    options = [
        "--address",
        "custom.example:7233",
        "--namespace",
        "custom-namespace",
        "--host",
        "0.0.0.0",
        "--port",
        "8910",
    ]
    arguments = {
        "before": [*options, "api"],
        "after": ["api", *options],
        "split": [*options[:4], "api", *options[4:]],
        "repeated": [*options, "api", "--port", "8920"],
    }
    seen = []

    async def run(args):
        seen.append(args)

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(sys, "argv", ["axcutor-playground", *arguments[position]])
    cli.main()
    [args] = seen
    assert args.address == "custom.example:7233"
    assert args.namespace == "custom-namespace"
    assert args.host == "0.0.0.0"
    assert args.port == (8920 if position == "repeated" else 8910)


@pytest.mark.parametrize("source", ["before", "after", "environment"])
def test_api_namespace_reaches_temporal_client(monkeypatch, source):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "127.0.0.1:1")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "environment-namespace")
    arguments = {
        "before": ["--namespace", "command-namespace", "api"],
        "after": ["api", "--namespace", "command-namespace"],
        "environment": ["api"],
    }
    real_connect = cli.Client.connect
    connections = []

    async def connect(address, *, namespace="default", lazy=False):
        client = await real_connect(address, namespace=namespace, lazy=lazy)
        connections.append((address, client.namespace, lazy))
        return client

    async def serve(server):
        pass

    monkeypatch.setattr(cli.Client, "connect", connect)
    monkeypatch.setattr(cli.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["axcutor-playground", *arguments[source]])
    cli.main()
    expected = "environment-namespace" if source == "environment" else "command-namespace"
    assert connections == [("127.0.0.1:1", expected, True)]


@asynccontextmanager
async def api_client(*args, cwd):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).with_name("axcutor-playground")),
        "api",
        "--port",
        str(port),
        *map(str, args),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with asyncio.timeout(10):
                while True:
                    if process.returncode is not None:
                        raise AssertionError(await process.communicate())
                    try:
                        await client.get("/openapi.json")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.02)
            yield client
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        # Uvicorn restores and re-raises SIGTERM after graceful ASGI shutdown.
        assert process.returncode in (0, -signal.SIGTERM), (stdout, stderr)


async def test_api_serves_schema_and_preflight_without_backend_or_workspace(tmp_path):
    async with api_client("--address", "127.0.0.1:1", cwd=tmp_path) as client:
        schema = loads((await client.get("/openapi.json")).text)
        assert schema["info"]["title"] == "Axcutor Execution API"
        assert "/v1/executions" in schema["paths"]
        assert "/v1/workspace" not in schema["paths"]
        invalid = await client.post("/v1/executions", content="{")
        assert invalid.status_code == 400
        assert loads(invalid.text)["error"]["code"] == "invalid_json"
        invalid = await client.post(
            "/v1/executions", content=dumps({"entry": "__main__", "mounts": {}, "input": None})
        )
        assert invalid.status_code == 400
        invalid = await client.post(
            "/v1/executions",
            content=dumps(
                {
                    "entry": "__main__",
                    "mounts": {
                        "__main__": {
                            "packages": {"__main__": {"format": "yaml", "text": "graph: {}"}}
                        }
                    },
                }
            ),
        )
        assert invalid.status_code == 422
        assert loads(invalid.text)["error"]["code"] == "compilation_failed"
        for path in ("/v1/workspace", "/"):
            assert (await client.get(path)).status_code == 404
        assert list(tmp_path.iterdir()) == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("environment", [False, True])
async def test_api_uses_configured_connection_and_external_worker_queue(
    server, tmp_path, monkeypatch, environment
):
    queue = str(uuid4())
    connection = {
        "address": server.client.service_client.config.target_host,
        "namespace": server.client.namespace,
        "task-queue": queue,
    }
    args = []
    if environment:
        for option, name in (
            ("address", "TEMPORAL_ADDRESS"),
            ("namespace", "TEMPORAL_NAMESPACE"),
            ("task-queue", "AXCUTOR_TASK_QUEUE"),
        ):
            monkeypatch.setenv(name, connection[option])
    else:
        for option, value in connection.items():
            args.extend(["--" + option, value])
    value = {"integer": 10**80, "decimal": Decimal("1.0000000000000000001"), "z": 3, "a": 1}
    kind = {
        "type": "object",
        "properties": {
            "integer": {"type": "integer"},
            "decimal": {"type": "number"},
            "z": {"type": "integer"},
            "a": {"type": "integer"},
        },
    }
    document = graph(
        {"copy": assign("value", "result")},
        ["start -> copy", "copy -> end"],
        inputs={"value": kind},
        outputs={"result": kind},
    )
    async with create_worker(server.client, queue), api_client(*args, cwd=tmp_path) as client:
        response = await client.post(
            "/v1/executions",
            content=dumps(
                {
                    "entry": "__main__",
                    "mounts": {
                        "__main__": {
                            "packages": {"__main__": {"format": "json", "text": dumps(document)}}
                        }
                    },
                    "input": {"value": value},
                }
            ),
        )
        assert response.status_code == 202, response.text
        first = loads(response.text)
        final = await asyncio.wait_for(AxcutorHost(server.client).wait(first["execution_id"]), 10)
        assert final["status"] == "completed", final
        response = await client.get(response.headers["location"])
        assert response.status_code == 200
        assert dumps(loads(response.text)["result"]["output"]["result"]) == dumps(value)
        assert (await client.post("/v1/workspace/executions", content="{}")).status_code == 404
        assert list(tmp_path.iterdir()) == []
