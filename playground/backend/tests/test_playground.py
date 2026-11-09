"""Real Temporal and resident executors: serial roots, concurrent graph calls.

Test templates use a distinct Python root so host/runtime tests may import their
own demo packages in the same pytest process without unloading foreign modules.
"""

import asyncio
import os
import py_compile
import shutil
import signal
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from axcutor_host.service import ExecutionService, RuntimeUnavailable
from axcutor_playground.coordinator import Playground
from axcutor_playground.http import create_playground_app
from axcutor_playground.workspace import Workspace
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
ENTRY = "workflows/playground_demo/main/__axcutor__.yaml"
INPUT = "inputs/demo.json"
DOUBLE = "workflows/playground_demo/double/__init__.py"
HELPER = "workflows/playground_demo/double/helper.py"
SUMMARY = "workflows/playground_demo/main/summarize.py"
DOUBLE_WORKFLOW = "workflows/playground_demo/double/__axcutor__.yaml"


@pytest.fixture(scope="module")
async def server():
    address = os.getenv("AXCUTOR_TEST_TEMPORAL_ADDRESS")
    if address:
        yield SimpleNamespace(client=await Client.connect(address))
    else:
        async with await WorkflowEnvironment.start_local(
            dev_server_download_version="v1.6.1"
        ) as environment:
            yield environment


@pytest.fixture
def templates(tmp_path):
    root = tmp_path / "templates"
    shutil.copytree(EXAMPLES, root, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "workflows/demo").rename(root / "workflows/playground_demo")
    for path in root.rglob("*.yaml"):
        path.write_text(path.read_text().replace("workflow:demo.", "workflow:playground_demo."))
    return root


@pytest.fixture
async def playground(server, tmp_path, templates):
    host = Playground(server.client, Workspace(tmp_path / "workspace", templates))
    await host.start()
    try:
        yield host
    finally:
        await host.close()


def edit(view, changes):
    files = {file["path"]: file["text"] for file in view["files"]}
    files.update(changes)
    return [{"path": path, "text": text} for path, text in files.items() if text is not None]


async def idle(host):
    async with asyncio.timeout(15):
        while host.view()["busy"]:  # noqa: ASYNC110 - observing cleanup after Temporal closes
            await asyncio.sleep(0.02)


async def wait_for(host, execution_id):
    final = await asyncio.wait_for(host.executions.wait(execution_id), 20)
    await idle(host)
    return final


async def test_resident_worker_reimports_python_helpers_contracts_and_deleted_modules(playground):
    host = playground
    worker = host._worker
    runtime_module = sys.modules["axcutor_runtime.module"]
    view = host.view()
    original = {file["path"]: file["text"] for file in view["files"]}
    double = original[DOUBLE].replace(
        "from ._contract",
        "from shared_helper import offset\nfrom . import helper\nfrom ._contract",
    )
    double = double.replace(
        "input.value * 2", "int(input.value) * helper.FACTOR + 0 + offset.VALUE"
    )
    view = await host.save(
        view["revision"],
        edit(
            view,
            {
                INPUT: '{"items":[3,1,2],"delay_seconds":0}',
                DOUBLE: double,
                HELPER: "FACTOR = 1\n",
                "shared_helper/offset.py": "VALUE = 0\n",  # Ordinary namespace helper package.
            },
        ),
    )
    # Seed a valid timestamp/size pyc, then change one byte with the same mtime.
    # Python's ordinary importer alone would keep FACTOR=1.
    helper = host.workspace.root / HELPER
    stamp = 1700000000
    os.utime(helper, (stamp, stamp))
    py_compile.compile(str(helper), doraise=True)
    helper.write_text("FACTOR = 2\n")
    os.utime(helper, (stamp, stamp))
    first = await host.submit(ENTRY, INPUT, host.view()["revision"])
    assert first["source"]["entry"] == "playground_demo.main"
    assert (await wait_for(host, first["execution"]["execution_id"]))["result"]["output"] == {
        "doubled": [6, 2, 4],
        "total": 12,
    }
    contract = sys.modules["playground_demo.double._contract"]
    old_input = contract.Input
    old_sha = contract.__axcutor_contract_sha256__
    old_namespace = sys.modules["shared_helper"]
    view = host.view()
    view = await host.save(
        view["revision"],
        edit(
            view,
            {
                DOUBLE: double.replace("+ 0", "+ 1"),  # Same-length business edit too.
                HELPER: "FACTOR = 3\n",
                DOUBLE_WORKFLOW: original[DOUBLE_WORKFLOW].replace(
                    "value: {type: integer}", "value: {type: number}", 1
                ),
                SUMMARY: original[SUMMARY].replace(
                    "sum(input.doubled)", "sum(input.doubled) + 100"
                ),
            },
        ),
    )
    second = await host.submit(ENTRY, INPUT, view["revision"])
    assert second["workspace"]["revision"] != view["revision"]  # New contract bytes.
    assert host._worker is worker
    assert (await wait_for(host, second["execution"]["execution_id"]))["result"]["output"] == {
        "doubled": [10, 4, 7],
        "total": 121,
    }
    contract = sys.modules["playground_demo.double._contract"]
    assert contract.Input is not old_input
    assert contract.__axcutor_contract_sha256__ != old_sha
    assert sys.modules["shared_helper"] is not old_namespace
    assert sys.modules["axcutor_runtime.module"] is runtime_module

    view = host.view()
    view = await host.save(view["revision"], edit(view, {HELPER: None}))
    third = await host.submit(ENTRY, INPUT, view["revision"])
    failed = await wait_for(host, third["execution"]["execution_id"])
    assert failed["status"] == "failed"
    assert "helper" in dumps(failed["error"])
    assert "playground_demo.double.helper" not in sys.modules
    assert host._worker is worker
    assert not (host.workspace.root / "runs").exists()


async def test_busy_rejects_writes_immediately_but_keeps_graph_concurrency(playground):
    host = playground
    view = host.view()
    double = next(f["text"] for f in view["files"] if f["path"] == DOUBLE)
    double = double.replace("from ._contract", "from . import helper\nfrom ._contract")
    double = double.replace(
        "    await asyncio.sleep(input.delay_seconds)",
        "    helper.active += 1\n"
        "    helper.peak = max(helper.peak, helper.active)\n"
        "    await asyncio.sleep(input.delay_seconds)\n"
        "    helper.active -= 1",
    )
    view = await host.save(
        view["revision"],
        edit(
            view,
            {
                INPUT: '{"items":[3,1,2],"delay_seconds":1}',
                DOUBLE: double,
                HELPER: "active = 0\npeak = 0\n",
            },
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_playground_app(host)), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/workspace/executions",
            json={
                "entry": ENTRY,
                "input": INPUT,
                "revision": view["revision"],
            },
        )
        assert result.status_code == 202, result.text
        state = loads(result.text)["workspace"]
        assert state["busy"]
        async with asyncio.timeout(1):
            saved, run, read = await asyncio.gather(
                client.put(
                    "/v1/workspace", json={"revision": state["revision"], "files": edit(state, {})}
                ),
                client.post(
                    "/v1/workspace/executions", json={"entry": ENTRY, "revision": state["revision"]}
                ),
                client.get("/v1/workspace"),
            )
        assert saved.status_code == run.status_code == 409
        assert "busy" in saved.text
        assert read.status_code == 200 and loads(read.text)["busy"]
        final = await wait_for(host, state["execution_id"])
        assert final["result"]["output"]["total"] == 12
        assert sys.modules["playground_demo.double.helper"].peak == 2


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_waits_for_local_cleanup_before_next_run(playground, terminate):
    host = playground
    view = host.view()
    double = """import asyncio
from . import helper
from ._contract import Input, Output, ReturnType

async def execute(input: Input) -> ReturnType:
    helper.started.set()
    try:
        await asyncio.sleep(input.delay_seconds)
    except asyncio.CancelledError:
        helper.cleaning.set()
        await helper.release.wait()
        raise
    return Output(value=input.value * 2)
"""
    view = await host.save(
        view["revision"],
        edit(
            view,
            {
                DOUBLE: double,
                HELPER: (
                    "import asyncio\nstarted = asyncio.Event()\n"
                    "cleaning = asyncio.Event()\nrelease = asyncio.Event()\n"
                ),
                INPUT: '{"items":[1],"delay_seconds":30}',
            },
        ),
    )
    result = await host.submit(ENTRY, INPUT, view["revision"])
    execution_id = result["execution"]["execution_id"]
    helper = None
    try:
        async with asyncio.timeout(5):
            while "playground_demo.double.helper" not in sys.modules:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            helper = sys.modules["playground_demo.double.helper"]
            await helper.started.wait()
        await host.executions.cancel(execution_id)
        await asyncio.wait_for(helper.cleaning.wait(), 5)
        if terminate:
            # Force a terminal root while business cleanup is still blocked.
            await host.client.get_workflow_handle(execution_id).terminate()
            assert (await host.executions.wait(execution_id))["status"] == "failed"
        assert host.view()["busy"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_playground_app(host)), base_url="http://test"
        ) as client:
            assert (
                await client.put(
                    "/v1/workspace",
                    json={
                        "revision": host.view()["revision"],
                        "files": edit(host.view(), {}),
                    },
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/v1/workspace/executions",
                    json={
                        "entry": ENTRY,
                        "revision": host.view()["revision"],
                    },
                )
            ).status_code == 409
    finally:
        if helper is not None:
            helper.release.set()
    final = await wait_for(host, execution_id)
    assert final["status"] == ("failed" if terminate else "cancelled")
    view = await host.save(
        host.view()["revision"],
        edit(
            host.view(),
            {
                INPUT: '{"items":[1],"delay_seconds":0}',
            },
        ),
    )
    next_run = await host.submit(ENTRY, INPUT, view["revision"])
    assert (await wait_for(host, next_run["execution"]["execution_id"]))["status"] == "completed"


async def test_shutdown_cancels_demo_and_restart_uses_fresh_queue(server, tmp_path, templates):
    host = Playground(server.client, Workspace(tmp_path / "workspace", templates))
    await host.start()
    view = host.view()
    queue = host.queue
    result = await host.submit(ENTRY, INPUT, view["revision"])
    await host.close()
    assert (await host.executions.wait(result["execution"]["execution_id"]))[
        "status"
    ] == "cancelled"
    restarted = Playground(server.client, Workspace(tmp_path / "workspace", templates))
    await restarted.start()
    try:
        assert restarted.queue != queue
        assert not restarted.view()["busy"]
        assert restarted.view()["revision"] == result["workspace"]["revision"]
        assert not (host.workspace.root / "runs").exists()
    finally:
        await restarted.close()


async def test_ambiguous_submission_retains_id_without_automatic_resubmission(
    playground, monkeypatch
):
    host = playground
    real_submit = host.executions.submit
    submitted_ids = []

    async def lose_ack(entry, packages, input=None, execution_id=None):
        submitted_ids.append(execution_id)
        await real_submit(entry, packages, input, execution_id)
        raise RuntimeUnavailable("lost submission response")

    monkeypatch.setattr(host.executions, "submit", lose_ack)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_playground_app(host)), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/workspace/executions",
            json={
                "entry": ENTRY,
                "input": INPUT,
                "revision": host.view()["revision"],
            },
        )
        assert result.status_code == 503, result.text
        details = loads(result.text)["error"]["details"]
        execution_id = details["execution_id"]
        assert submitted_ids == [execution_id]
        assert details["workspace"]["busy"]
        assert host.execution_id == execution_id
        assert (await client.get(f"/v1/executions/{execution_id}")).status_code == 200
        await host.executions.cancel(execution_id)
        await wait_for(host, execution_id)
        assert submitted_ids == [execution_id]


async def test_execution_routes_preserve_ids_lossless_frames_and_cancellation(
    server, tmp_path, templates
):
    host = Playground(server.client, Workspace(tmp_path / "workspace", templates))
    queue = str(uuid4())
    service = ExecutionService(server.client, queue)
    execution_id = "订单 / a/../b\n% ? # :cancel"
    location = "/v1/executions/" + quote(execution_id, safe="").replace(".", "%2E")
    text = dumps(
        {
            "input": {"n": {"type": "number"}},
            "output": {},
            "run": {
                "executor": "builtin",
                "content": "import asyncio\nawait asyncio.sleep(30)\nreturn Output()",
            },
        }
    )
    async with create_worker(server.client, queue):
        await service.submit(
            "__main__",
            {"__main__": {"format": "json", "text": text}},
            loads('{"n":1.00000000000000000000001}'),
            execution_id,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_playground_app(host)), base_url="http://test"
        ) as client:
            try:
                response = await client.get(location)
                assert response.status_code == 200, response.text
                assert loads(response.text)["execution_id"] == execution_id
                assert "1.00000000000000000000001" in response.text
                cancelled = await client.post(location + ":cancel")
                assert cancelled.status_code in (200, 202), cancelled.text
                assert (await service.wait(execution_id))["status"] == "cancelled"
                assert (await client.post(location + ":cancel")).status_code == 200
                assert (await client.get("/v1/executions/" + str(uuid4()))).status_code == 404
            finally:
                await service.cancel(execution_id)


async def test_playground_signal_route_uses_execution_contract(server, tmp_path, templates):
    host = Playground(server.client, Workspace(tmp_path / "data", templates))
    queue = str(uuid4())
    service = ExecutionService(server.client, queue)
    execution_id = "订单 / a/../b\n% ? # :signal"
    location = "/v1/executions/" + quote(execution_id, safe="").replace(".", "%2E")
    doc = {"input": {}, "output": {"value": {"type": "number"}}, "run": {"executor": "signal"}}
    async with create_worker(server.client, queue):
        await service.submit(
            "__main__",
            {"__main__": {"format": "json", "text": dumps(doc)}},
            execution_id=execution_id,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_playground_app(host)), base_url="http://test"
        ) as client:
            try:
                schema = loads((await client.get("/openapi.json")).text)
                assert (
                    schema["paths"]["/v1/executions/{execution_id}:signal"]["post"]["operationId"]
                    == "signalExecution"
                )
                for body, status, code in [
                    ("{", 400, "invalid_json"),
                    (dumps({"address": "__main__", "output": None}), 400, "invalid_request"),
                    (dumps({"address": "__main__", "output": {}}), 422, "signal_validation_failed"),
                    (dumps({"address": "elsewhere"}), 409, "signal_not_waiting"),
                ]:
                    response = await client.post(location + ":signal", content=body)
                    assert response.status_code == status, response.text
                    assert loads(response.text)["error"]["code"] == code
                response = await client.post(
                    location + ":signal",
                    content=('{"address":"__main__","output":{"value":1.000000000000000000001}}'),
                )
                assert response.status_code in (200, 202), response.text
                final = await service.wait(execution_id)
                assert dumps(final["result"]["output"]) == '{"value":1.000000000000000000001}'
                response = await client.post(location + ":signal", content='{"address":"__main__"}')
                assert response.status_code == 409
            finally:
                await service.cancel(execution_id)


async def test_independent_console_entry_starts_http_serves_static_and_runs_modules(
    server, tmp_path, templates
):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>Independent Playground</h1>", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("w+") as log:
        process = await asyncio.create_subprocess_exec(
            str(Path(sys.executable).with_name("axcutor-playground")),
            "--address",
            server.client.service_client.config.target_host,
            "--namespace",
            server.client.namespace,
            "--port",
            str(port),
            "--workspace",
            str(tmp_path / "workspace"),
            "--examples",
            str(templates),
            "--static",
            str(static),
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                async with asyncio.timeout(15):
                    while True:
                        if process.returncode is not None:
                            log.seek(0)
                            pytest.fail(log.read())
                        try:
                            response = await client.get("/v1/workspace")
                            if response.status_code == 200:
                                break
                        except httpx.RequestError:
                            pass
                        await asyncio.sleep(0.05)
                assert (await client.get("/")).text == "<h1>Independent Playground</h1>"
                view = loads(response.text)
                saved = await client.put(
                    "/v1/workspace",
                    content=dumps(
                        {
                            "revision": view["revision"],
                            "files": edit(view, {INPUT: '{"items":[3,1,2],"delay_seconds":0}'}),
                        }
                    ),
                )
                assert saved.status_code == 200, saved.text
                submitted = await client.post(
                    "/v1/workspace/executions",
                    content=dumps(
                        {
                            "entry": ENTRY,
                            "input": INPUT,
                            "revision": loads(saved.text)["revision"],
                        }
                    ),
                )
                assert submitted.status_code == 202, submitted.text
                execution_id = loads(submitted.text)["execution"]["execution_id"]
                final = await asyncio.wait_for(
                    ExecutionService(server.client).wait(execution_id), 15
                )
                assert final["result"]["output"] == {"doubled": [6, 2, 4], "total": 12}
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 30)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        assert process.returncode in (0, -signal.SIGTERM)  # Uvicorn re-raises handled signals.
