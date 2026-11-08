"""Host contracts exercised against the compiler and real Temporal boundaries."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from axcutor_host import (
    CompilationError,
    ExecutionAlreadyExists,
    ExecutionNotFound,
    ExecutionService,
    InputValidationError,
    InvalidRequest,
    RuntimeUnavailable,
)
from axcutor_runtime import AxcutorInterpreterWorkflow
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, N, action, array, assign, builtin, call, graph, inline, literal
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


@workflow.defn
class OtherWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


def packages(document, **extra):
    return {
        "demo.main": {"format": "json", "text": dumps(document)},
        **{name: {"format": "json", "text": dumps(value)} for name, value in extra.items()},
    }


def empty():
    return graph({"noop": action("return Output()")}, ["start -> noop", "noop -> end"])


def service(client, queue=None, **kwargs):
    return ExecutionService(
        client,
        queue or str(uuid4()),
        options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000},
        **kwargs,
    )


async def assert_not_public(host, handle):
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    for method in (host.get, host.cancel):
        with pytest.raises(ExecutionNotFound):
            await method(handle.id)
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    history = await handle.fetch_history()
    assert not any(
        event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
        for event in history.events
    )


async def test_static_preflight_runs_without_backend_and_creates_no_execution():
    client = await Client.connect("127.0.0.1:1", lazy=True)
    host = service(client, rpc_timeout=timedelta(milliseconds=100))
    with pytest.raises(CompilationError) as error:
        await host.submit("demo.main", packages({"input": {}, "output": {}, "graph": {}}))
    assert error.value.diagnostics
    assert all("code" in diagnostic for diagnostic in error.value.diagnostics)
    doc = empty()
    doc["input"] = {"n": INTEGER}
    with pytest.raises(InputValidationError):
        await host.submit("demo.main", packages(doc), {"n": Decimal("1.0")})
    with pytest.raises(InputValidationError):
        await host.submit("demo.main", packages(doc), {})
    with pytest.raises(InvalidRequest):
        await host.submit("demo.main", {"demo.main": {"format": "xml", "text": ""}})
    with pytest.raises(RuntimeUnavailable):
        await host.submit("demo.main", packages(empty()))


async def test_input_preflight_does_not_reserve_id(server):
    host = service(server.client)
    execution_id = str(uuid4())
    doc = empty()
    doc["input"] = {"n": INTEGER}
    with pytest.raises(InputValidationError):
        await host.submit(
            "demo.main",
            packages(doc),
            {},
            execution_id,
        )
    with pytest.raises(ExecutionNotFound):
        await host.get(execution_id)


async def test_lossless_result_named_branch_and_read_after_worker_stops(server):
    queue, execution_id = str(uuid4()), str(uuid4())
    host = service(server.client, queue)
    schema = {
        "type": "object",
        "properties": {
            "huge": INTEGER,
            "decimal": N,
            "extreme": N,
            "ordered": {"type": "object", "additionalProperties": INTEGER},
        },
    }
    doc = graph(
        {"copy": assign("value", "result")},
        ["start -> copy", "copy -> end.ok"],
        inputs={"value": schema},
        outputs={"result": schema},
        branches={"ok": "done"},
    )
    value = loads(
        '{"huge":' + "9" * 5000 + ',"decimal":1e0,"extreme":1e9999999999999999999999999,'
        '"ordered":{"z":3,"a":1,"m":2}}'
    )
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", packages(doc), {"value": value}, execution_id)
        assert first["execution_id"] == execution_id
        final = await host.wait(execution_id)
    assert final["status"] == "completed"
    assert final["frames"] == [] and final["error"] is None
    assert final["started_at"].endswith("Z") and final["finished_at"].endswith("Z")
    assert final["result"]["branch"] == "ok"
    encoded = dumps(final["result"]["output"]["result"])
    assert encoded.replace("e+", "e") == dumps(value).replace("e+", "e")
    assert '"decimal":1.0' in encoded
    assert list(final["result"]["output"]["result"]["ordered"]) == ["z", "a", "m"]
    fresh = service(server.client)
    assert dumps(await fresh.get(execution_id)) == dumps(final)
    assert dumps(await fresh.cancel(execution_id)) == dumps(final)
    with pytest.raises(ExecutionAlreadyExists):
        await fresh.submit("demo.main", packages(empty()), execution_id=execution_id)


async def test_duplicate_id_while_running_and_generated_uuid(server):
    queue = str(uuid4())
    host = service(server.client, queue)
    release = asyncio.Event()

    async def work(request, signal):
        await release.wait()
        return {"output": {}}

    doc = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "wait"}}
    async with create_worker(server.client, queue, {"builtin": work}):
        try:
            first = await host.submit("demo.main", packages(doc))
            assert len(first["execution_id"]) == 36
            with pytest.raises(ExecutionAlreadyExists):
                await host.submit("demo.main", packages(doc), execution_id=first["execution_id"])
            release.set()
            assert (await host.wait(first["execution_id"]))["status"] == "completed"
        finally:
            release.set()


@pytest.mark.parametrize("offloaded", [False, True])
async def test_live_frame_tree_and_cooperative_cancel_include_all_descendants(server, offloaded):
    queue = str(uuid4())
    host = ExecutionService(
        server.client,
        queue,
        options={
            "heartbeat_interval_ms": 20,
            "heartbeat_timeout_ms": 1000,
            "child_workflow_history_events": 1 if offloaded else None,
        },
    )
    entered, cleanup = asyncio.Event(), asyncio.Event()
    signals, cleaned, inputs = [], [], []

    async def work(request, signal):
        signals.append(signal)
        inputs.append(request["input_snapshot"])
        if len(signals) == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert signal.aborted
            await cleanup.wait()
            cleaned.append(request["input_snapshot"]["n"])

    leaf = {
        "input": {"n": N},
        "output": {"result": N},
        "run": {"executor": "builtin", "content": "wait"},
    }
    sub = graph(
        {"invoke": call("leaf", {"n": "item"}, {"result": "value"})},
        ["start -> invoke", "invoke -> end"],
        outputs={"value": N},
    )
    doc = graph(
        {"batch": inline(sub, for_each={"source": "items", "item": "item", "concurrency": 2})},
        ["start -> batch", "batch -> end"],
        inputs={"items": array(N)},
        outputs={"value": array(N)},
        variables={"private": {**INTEGER, "default": 17}, "unbound": INTEGER},
    )
    async with create_worker(server.client, queue, {"builtin": work}):
        try:
            first = await host.submit(
                "demo.main", packages(doc, leaf=leaf), {"items": [Decimal("1.0"), Decimal("2.0")]}
            )
            await asyncio.wait_for(entered.wait(), 10)
            view = await host.get(first["execution_id"])
            root = view["frames"][0]
            assert view["status"] == "running"
            assert view["finished_at"] is view["result"] is view["error"] is None
            assert root["address"] == "demo.main" and root["cursor"] == "batch"
            assert root["kind"] == "function" and root["steps"] == 2 and root["max_steps"] == 1000
            assert list(root["variables"]) == ["items", "private"]
            assert len(root["children"]) == 2
            for i, iteration in enumerate(root["children"]):
                assert iteration["kind"] == "iteration" and iteration["call_depth"] == 1
                assert iteration["max_steps"] is None and iteration["cursor"] == "batch"
                # Observers see every bound parent variable plus injected item;
                # neither the inline block nor its executor gains those inputs.
                assert iteration["variables"] == {**root["variables"], "item": Decimal(i + 1)}
                block = iteration["children"][0]
                assert block["kind"] == "block" and block["call_depth"] == 2
                assert block["variables"] == {"item": Decimal(i + 1)}
                function = block["children"][0]
                assert function == {
                    "kind": "function",
                    "address": f"demo.main/batch[{i}]/invoke",
                    "cursor": "run",
                    "call_depth": 3,
                    "steps": 0,
                    "max_steps": None,
                    "variables": {"n": Decimal(f"{i + 1}.0")},
                    "children": [],
                }
            assert all(list(value) == ["n"] for value in inputs)
            assert sorted(value["n"] for value in inputs) == [Decimal(1), Decimal(2)]
            if offloaded:
                snapshot = await server.client.get_workflow_handle(first["execution_id"]).query(
                    AxcutorInterpreterWorkflow.get_runtime_snapshot
                )
                for iteration in snapshot["frames"][0]["children"]:
                    remote = iteration["children"][0]["children"][0]["child_execution"]
                    await assert_not_public(
                        host,
                        server.client.get_workflow_handle(
                            remote["workflow_id"], run_id=remote["run_id"]
                        ),
                    )
            cancelling = await host.cancel(first["execution_id"])
            assert cancelling["status"] == "running"
            cleanup.set()
            final = await host.wait(first["execution_id"])
            assert final["status"] == "cancelled"
            assert final["frames"] == [] and final["result"] is final["error"] is None
            assert all(signal.aborted for signal in signals) and len(cleaned) == 2
            assert await host.cancel(first["execution_id"]) == final
        finally:
            cleanup.set()


@pytest.mark.parametrize(
    "selector",
    [
        "execution",
        "timeout",
        "contract",
        "max_steps",
        "StackOverflowError",
        "MaxStepsExceededError",
        "EngineError",
    ],
)
async def test_failure_categories_from_real_interpreter(server, selector):
    queue = str(uuid4())
    host = service(server.client, queue)
    inputs = {}
    if selector in ("max_steps", "MaxStepsExceededError"):
        doc = graph(
            {"a": assign(1, "value"), "b": assign(2, "value")},
            ["start -> a", "a -> b", "b -> end"],
            outputs={"value": INTEGER},
            max_steps=1,
        )
        if selector == "max_steps":
            doc = graph(
                {"sub": inline(doc, [])}, ["start -> sub", "sub -> end"], outputs={"value": INTEGER}
            )
    elif selector == "StackOverflowError":
        host = ExecutionService(server.client, queue, options={"max_call_depth": 1})
        doc = graph({"sub": inline(empty())}, ["start -> sub", "sub -> end"])
    elif selector == "EngineError":
        doc = graph(
            {
                "math": builtin(
                    "math", {"op": literal("mul"), "left": "n", "right": "n"}, {"result": "value"}
                )
            },
            ["start -> math", "math -> end"],
            inputs={"n": N},
            outputs={"value": N},
        )
        inputs = {"n": Decimal("1e308")}
    else:
        content = {
            "execution": "raise RuntimeError('business failed')",
            "timeout": "import asyncio\nawait asyncio.sleep(10)",
            "contract": "return {}",
        }[selector]
        doc = {
            "input": {},
            "output": {"value": INTEGER},
            "run": {"executor": "builtin", "content": content},
            **({"timeout_ms": 40} if selector == "timeout" else {}),
        }
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", packages(doc), inputs)
        final = await host.wait(first["execution_id"])
    assert final["status"] == "failed"
    assert final["frames"] == [] and final["result"] is None
    assert final["error"]["type"] == selector
    assert final["error"]["category"] == ("recoverable" if selector[0].islower() else "panic")
    assert final["error"]["message"]


async def test_no_worker_is_bounded_and_accepted_execution_is_durable(server):
    host = service(server.client, rpc_timeout=timedelta(milliseconds=150))
    execution_id = str(uuid4())
    with pytest.raises(RuntimeUnavailable):
        await asyncio.wait_for(
            host.submit("demo.main", packages(empty()), execution_id=execution_id), 3
        )
    with pytest.raises(RuntimeUnavailable):
        await asyncio.wait_for(host.get(execution_id), 3)
    with pytest.raises(ExecutionAlreadyExists):
        await host.submit("demo.main", packages(empty()), execution_id=execution_id)
    await server.client.get_workflow_handle(execution_id).terminate()
    final = await host.get(execution_id)
    assert final["status"] == "failed" and final["error"]["type"] == "EngineError"


async def test_missing_execution_get_and_cancel(server):
    host = service(server.client)
    for method in (host.get, host.cancel):
        with pytest.raises(ExecutionNotFound):
            await method(str(uuid4()))


async def test_other_workflow_is_not_a_public_execution(server):
    queue = str(uuid4())
    host = service(server.client, queue)
    async with Worker(
        server.client,
        task_queue=queue,
        workflows=[OtherWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await server.client.start_workflow(
            OtherWorkflow.run, id=str(uuid4()), task_queue=queue
        )
        try:
            await assert_not_public(host, handle)
        finally:
            await handle.terminate()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_wait_long_polls_one_run_through_worker_absence(server, monkeypatch, cancel_waiter):
    queue, execution_id = str(uuid4()), str(uuid4())
    host = service(server.client, queue, rpc_timeout=timedelta(milliseconds=250))
    with pytest.raises(RuntimeUnavailable):
        await host.submit("demo.main", packages(empty()), execution_id=execution_id)
    handle = server.client.get_workflow_handle(execution_id)
    describes, queries = [], []
    original_describe, original_query = WorkflowHandle.describe, WorkflowHandle.query

    async def describe(target, **kwargs):
        describes.append(target.run_id)
        return await original_describe(target, **kwargs)

    async def query(target, *args, **kwargs):
        queries.append(target.run_id)
        return await original_query(target, *args, **kwargs)

    monkeypatch.setattr(WorkflowHandle, "describe", describe)
    monkeypatch.setattr(WorkflowHandle, "query", query)
    waiter = asyncio.create_task(host.wait(execution_id))
    try:
        # Several per-operation deadlines pass while history remains open and
        # no Worker can answer queries. The business wait must stay pending.
        await asyncio.sleep(0.75)
        assert not waiter.done()
        if cancel_waiter:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (await original_describe(handle)).status == WorkflowExecutionStatus.RUNNING
            history = await handle.fetch_history()
            assert not any(
                event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
                for event in history.events
            )
            waiter = asyncio.create_task(host.wait(execution_id))
        async with create_worker(server.client, queue):
            final = await asyncio.wait_for(waiter, 10)
        assert final["status"] == "completed"
        assert final["frames"] == [] and final["error"] is None
        assert not queries
        assert len(describes) <= (3 if cancel_waiter else 2)
        # The first lookup resolves the run once; all subsequent reads pin it.
        assert describes[-1] == (await original_describe(handle)).run_id
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        if (await original_describe(handle)).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate()


@pytest.mark.parametrize("close_before_query", [False, True])
async def test_completion_racing_live_query_returns_coherent_terminal_view(
    server, monkeypatch, close_before_query
):
    queue, release = str(uuid4()), asyncio.Event()
    host = service(server.client, queue)

    async def work(request, signal):
        await release.wait()
        return {"output": {}}

    doc = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "wait"}}
    async with create_worker(server.client, queue, {"builtin": work}):
        try:
            first = await host.submit("demo.main", packages(doc))
            original_query = WorkflowHandle.query

            async def close_during_query(handle, *args, **kwargs):
                if close_before_query:
                    release.set()
                    await handle.result()
                snapshot = await original_query(handle, *args, **kwargs)
                if not close_before_query:
                    release.set()
                    await handle.result()
                return snapshot

            # Delay the real SDK boundary, allowing an actual completion both before
            # the server's NOT_OPEN check and after a valid running snapshot.
            monkeypatch.setattr(WorkflowHandle, "query", close_during_query)
            final = await host.get(first["execution_id"])
            assert final["status"] == "completed"
            assert final["frames"] == [] and final["error"] is None
            assert final["result"] == {"branch": None, "output": {}}
        finally:
            release.set()


async def test_completion_racing_cancel_is_idempotent(server, monkeypatch):
    queue, release = str(uuid4()), asyncio.Event()
    host = service(server.client, queue)

    async def work(request, signal):
        await release.wait()
        return {"output": {}}

    doc = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "wait"}}
    async with create_worker(server.client, queue, {"builtin": work}):
        try:
            first = await host.submit("demo.main", packages(doc))
            original_cancel = WorkflowHandle.cancel

            async def close_before_cancel(handle, **kwargs):
                release.set()
                await handle.result()
                return await original_cancel(handle, **kwargs)

            monkeypatch.setattr(WorkflowHandle, "cancel", close_before_cancel)
            final = await host.cancel(first["execution_id"])
            assert final["status"] == "completed" and final["frames"] == []
        finally:
            release.set()
