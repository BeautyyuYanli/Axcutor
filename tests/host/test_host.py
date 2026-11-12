"""Host contracts exercised against the compiler and real Temporal boundaries."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from axcutor_host import (
    AxcutorHost,
    CompilationError,
    ExecutionAlreadyExists,
    ExecutionNotFound,
    InputValidationError,
    InvalidRequest,
    RuntimeUnavailable,
    SignalNotWaiting,
    SignalValidationError,
)
from axcutor_runtime import AxcutorInterpreterWorkflow
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker
from helpers import (
    INTEGER,
    N,
    action,
    array,
    assign,
    builtin,
    call,
    graph,
    inline,
    literal,
    source_mounts,
)
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


@workflow.defn
class OtherWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


def packages(document, **extra):
    return source_mounts(
        {
            "demo.main": {"format": "json", "text": dumps(document)},
            **{name: {"format": "json", "text": dumps(value)} for name, value in extra.items()},
        }
    )


def empty():
    return graph({"noop": action("return Output()")}, ["start -> noop", "noop -> end"])


def service(client, queue=None, **kwargs):
    return AxcutorHost(
        client,
        queue or str(uuid4()),
        options={"heartbeat_interval_ms": 20, "heartbeat_timeout_ms": 1000},
        **kwargs,
    )


@pytest.mark.parametrize(
    "options",
    [
        [],
        "",
        False,
        0,
        {"heartbeat_interval_ms": None},
        {"heartbeat_timeout_ms": True},
        {"heartbeat_timeout_ms": 1},
        {"heartbeat_interval_ms": 30000},
        {"activity_timeout_ms": -1},
        {"max_call_depth": 0},
        {"task_queues": {"builtin": ""}},
        {"child_workflow_history_events": False},
        {"max_heartbeat_throttle_interval": 200},
    ],
)
async def test_invalid_host_options_are_rejected_at_construction_without_backend(options):
    client = await Client.connect("127.0.0.1:1", lazy=True)
    with pytest.raises(ValueError):
        AxcutorHost(client, options=options)


@pytest.mark.parametrize("position", ["input", "output"])
async def test_host_rejects_recursive_handle_interfaces_before_backend_contact(position):
    client = await Client.connect("127.0.0.1:1", lazy=True)
    document = {
        "input": {},
        "output": {},
        "run": {"executor": "builtin", "content": "pass"},
    }
    document[position] = {
        "resources": {
            "type": "array",
            "items": {"type": ["handle", "null"], "kind": "infra:Tx"},
        }
    }
    mounts = packages(document)
    mounts["infra"] = {"resources": {"infra": {"format": "yaml", "text": "resources: {Tx: null}"}}}
    with pytest.raises(InputValidationError, match="public locator"):
        await service(client).submit(
            "demo.main",
            mounts,
            {"resources": []} if position == "input" else {},
        )


async def test_host_preflight_and_signal_share_closed_external_locator_values(server):
    h = {"type": "handle", "kind": "infra:File"}
    document = graph(
        {
            "wait": {"run": {"executor": "signal"}, "output": {"selected": h}},
            "compare": builtin(
                "compare",
                {"op": literal("eq"), "left": "file", "right": "selected"},
                {"result": "same"},
            ),
        },
        ["start -> wait", "wait -> compare", "compare -> end"],
        inputs={"file": h},
        outputs={"same": {"type": "boolean"}, "selected": h},
    )
    mounts = packages(document)
    mounts["infra"] = {
        "resources": {
            "infra": {"format": "yaml", "text": "resources: {File: {locator: {type: string}}}"}
        }
    }
    queue = str(uuid4())
    host = service(server.client, queue)
    public = {"$handle": "infra:File", "locator": "file-1"}
    async with create_worker(server.client, queue):
        for invalid in [
            {"$handle": "rid", "kind": "infra:File"},
            {"$handle": "infra:File", "locator": None},
        ]:
            with pytest.raises(InputValidationError):
                await host.submit("demo.main", mounts, {"file": invalid})
        current = await host.submit("demo.main", mounts, {"file": public})
        assert current["frames"][0]["variables"]["file"] == public
        with pytest.raises(SignalValidationError):
            await host.signal(
                current["execution_id"],
                "demo.main/wait",
                output={"selected": {**public, "extra": 1}},
            )
        await host.signal(current["execution_id"], "demo.main/wait", output={"selected": public})
        final = await host.wait(current["execution_id"])
        assert final["result"]["output"] == {"same": True, "selected": public}


@pytest.mark.parametrize(
    "options", [None, {}, {"heartbeat_timeout_ms": 1000}, {"heartbeat_interval_ms": 200}]
)
async def test_host_persists_raw_overrides_and_worker_receives_derived_defaults(server, options):
    queue = str(uuid4())
    if options:
        options = {**options, "task_queues": {"builtin": queue}}
    original = deepcopy(options or {})
    host = AxcutorHost(server.client, queue, options=options)
    if options:
        options["task_queues"]["builtin"] = "must-not-be-used"
        options["heartbeat_interval_ms"] = 1
    seen = []

    async def work(request, signal):
        seen.append(request)
        return {"output": {}}

    async with create_worker(server.client, queue, {"builtin": work}):
        first = await host.submit("demo.main", packages(empty()))
        assert (await host.wait(first["execution_id"]))["status"] == "completed"
    history = await server.client.get_workflow_handle(first["execution_id"]).fetch_history()
    started = history.events[0].workflow_execution_started_event_attributes
    [request] = await server.client.data_converter.decode(started.input.payloads)
    assert request["options"] == original
    timeout = original.get("heartbeat_timeout_ms", 30000)
    assert seen[0]["heartbeat_interval_ms"] == original.get("heartbeat_interval_ms", timeout // 3)
    assert seen[0]["attempt_timeout_ms"] == 604800000


async def assert_not_public(host, handle):
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    for method in (host.get, host.cancel, lambda execution_id: host.signal(execution_id, "main")):
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
    host = AxcutorHost(
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
                    "waiting": [],
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
        host = AxcutorHost(server.client, queue, options={"max_call_depth": 1})
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


async def signal_view(host, execution_id, addresses):
    async with asyncio.timeout(10):
        while True:
            view = await host.get(execution_id)
            nodes = list(view["frames"])
            waiting = []
            while nodes:
                frame = nodes.pop(0)
                waiting.extend(frame["waiting"])
                nodes.extend(frame["children"])
            if set(waiting) == set(addresses):
                return view
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("child", [False, True])
async def test_signal_routes_multiple_child_levels_and_rewrites_waiting_addresses(server, child):
    queue = str(uuid4())
    host = AxcutorHost(
        server.client, queue, options={"child_workflow_history_events": 1 if child else None}
    )
    leaf = {"input": {"n": INTEGER}, "output": {"value": N}, "run": {"executor": "signal"}}
    middle = graph(
        {"invoke": call("demo.leaf", {"n": "n"}, {"value": "answer"})},
        ["start -> invoke", "invoke -> end"],
        inputs={"n": INTEGER},
        outputs={"answer": N},
    )
    doc = graph(
        {"invoke": call("demo.middle", {"n": "n"}, {"answer": "result"})},
        ["start -> invoke", "invoke -> end"],
        inputs={"n": INTEGER},
        outputs={"result": N},
    )
    sources = packages(doc, **{"demo.middle": middle, "demo.leaf": leaf})
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", sources, {"n": 10**40})
        execution_id = first["execution_id"]
        address = "demo.main/invoke/invoke"
        view = await signal_view(host, execution_id, [address])
        leaf_frame = view["frames"][0]["children"][0]["children"][0]
        assert leaf_frame["address"] == address and leaf_frame["variables"] == {"n": 10**40}
        with pytest.raises(SignalNotWaiting):
            await host.signal(execution_id, "elsewhere")
        with pytest.raises(SignalValidationError):
            await host.signal(execution_id, address, {"value": True})
        assert (await signal_view(host, execution_id, [address]))["frames"] == view["frames"]
        value = Decimal("1.00000000000000000000000000001")
        response = await host.signal(execution_id, address, {"value": value})
        assert response["status"] in ("running", "completed")
        final = await host.wait(execution_id)
        assert final["result"]["output"] == {"result": value}
        with pytest.raises(SignalNotWaiting):
            await host.signal(execution_id, address, {"value": value})


async def test_signal_child_routing_requires_path_segment_boundary(server):
    queue = str(uuid4())
    host = AxcutorHost(server.client, queue, options={"child_workflow_history_events": 1})
    leaf = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    doc = graph(
        {"call": call("leaf"), "call_other": {"run": {"executor": "signal"}}},
        ["start -> [call, call_other] -> end"],
    )
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", packages(doc, leaf=leaf))
        execution_id = first["execution_id"]
        await signal_view(host, execution_id, ["demo.main/call", "demo.main/call_other"])
        response = await host.signal(execution_id, "demo.main/call_other")
        assert response["status"] == "running"
        await signal_view(host, execution_id, ["demo.main/call"])
        with pytest.raises(SignalNotWaiting):
            await host.signal(execution_id, "demo.main/call[0]")
        await host.signal(execution_id, "demo.main/call")
        assert (await host.wait(execution_id))["status"] == "completed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"address": ""},
        {"address": None},
        {"address": 1},
        {"output": []},
        {"branch": ""},
        {"branch": 1},
        {"step": True},
        {"step": 0},
        {"step": -1},
        {"step": Decimal("1.0")},
        {"output": {"bad": float("inf")}},
    ],
)
async def test_signal_preflight_rejects_invalid_shapes_without_backend(kwargs):
    client = await Client.connect("127.0.0.1:1", lazy=True)
    host = service(client, rpc_timeout=timedelta(milliseconds=100))
    with pytest.raises(InvalidRequest):
        await host.signal("missing", **{"address": "main/wait", **kwargs})


async def test_signal_missing_root_is_not_found(server):
    with pytest.raises(ExecutionNotFound):
        await service(server.client).signal(str(uuid4()), "main/wait")


@pytest.mark.parametrize("operation", ["query", "execute_update"])
async def test_close_racing_signal_is_conflict_after_root_existence(server, monkeypatch, operation):
    queue = str(uuid4())
    host = service(server.client, queue)
    doc = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", packages(doc))
        execution_id = first["execution_id"]
        await signal_view(host, execution_id, ["demo.main"])
        original = getattr(WorkflowHandle, operation)

        async def close_before_operation(handle, *args, **kwargs):
            await handle.terminate()
            return await original(handle, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(WorkflowHandle, operation, close_before_operation)
            with pytest.raises(SignalNotWaiting):
                await host.signal(execution_id, "demo.main")
        assert (await host.get(execution_id))["status"] == "failed"


@pytest.mark.parametrize(
    "status",
    [RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION, RPCStatusCode.UNAVAILABLE],
)
async def test_signal_update_rpc_errors_distinguish_close_from_outage(server, monkeypatch, status):
    queue = str(uuid4())
    host = service(server.client, queue)
    doc = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    async with create_worker(server.client, queue):
        first = await host.submit("demo.main", packages(doc))
        execution_id = first["execution_id"]
        await signal_view(host, execution_id, ["demo.main"])

        async def rpc_failure(handle, *args, **kwargs):
            raise RPCError("closed or unavailable", status, b"")

        with monkeypatch.context() as patch:
            patch.setattr(WorkflowHandle, "execute_update", rpc_failure)
            expected = (
                RuntimeUnavailable if status == RPCStatusCode.UNAVAILABLE else SignalNotWaiting
            )
            with pytest.raises(expected):
                await host.signal(execution_id, "demo.main")
        await host.cancel(execution_id)
        assert (await host.wait(execution_id))["status"] == "cancelled"


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_signal_real_update_deadline_and_caller_cancellation(
    server, monkeypatch, cancel_caller
):
    queue = str(uuid4())
    host = service(server.client, queue, rpc_timeout=timedelta(milliseconds=300))
    doc = {"input": {}, "output": {}, "run": {"executor": "signal"}}
    async with create_worker(server.client, queue) as worker:
        first = await host.submit("demo.main", packages(doc))
        execution_id = first["execution_id"]
        await signal_view(host, execution_id, ["demo.main"])
        original_query = WorkflowHandle.query
        stopped = asyncio.Event()

        async def stop_after_query(handle, *args, **kwargs):
            snapshot = await original_query(handle, *args, **kwargs)
            await worker.shutdown()
            stopped.set()
            return snapshot

        with monkeypatch.context() as patch:
            patch.setattr(WorkflowHandle, "query", stop_after_query)
            task = asyncio.create_task(host.signal(execution_id, "demo.main"))
            await asyncio.wait_for(stopped.wait(), 5)
            if cancel_caller:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RuntimeUnavailable):
                    await asyncio.wait_for(task, 5)
        handle = server.client.get_workflow_handle(execution_id)
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        assert not any(
            e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED
            for e in (await handle.fetch_history()).events
        )
        await handle.terminate()
