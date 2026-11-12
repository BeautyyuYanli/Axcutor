"""External locator boundaries, ownership and observations through real Workers."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, Handle, resources
from axcutor_runtime.activities import RunActivities
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.resource_model import ResourceRegistry, description, same_identity
from axcutor_runtime.resources import ResourceContext, ResourceStore
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, action, array, assign, builtin, call, graph, inline, literal
from temporalio import activity, exceptions
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from tests.runtime.test_resources import (
    envelope,
    handle_type,
    request,
    resource_mounts,
    store,
)
from tests.runtime.test_resources import (
    resource_package as resource_package,
)


@pytest.fixture
def public_resources(resource_package):
    module = resource_package
    declarations = {
        name: {"locator": {"type": "object", "properties": {"value": INTEGER}}}
        for name in ("File", "Conn")
    }
    declarations["Tx"] = None
    (Path(module.__file__).parent / "__ax_resources__.yaml").write_text(
        dumps({"resources": declarations})
    )
    return module


@pytest.fixture
def execution(execution, public_resources):
    @asynccontextmanager
    async def run(*args, **kwargs):
        async with execution(*args, **kwargs) as state:
            state.mounts = resource_mounts(public_resources)
            yield state

    return run


def external(module, value=7, cls="File"):
    return {"$handle": f"{module.__name__}:{cls}", "locator": {"value": value}}


def created_files(module, queue, first_value=1):
    kind = handle_type(module, "File")["kind"]
    return {
        "result": {"output": {"files": [{"$handle": rid, "kind": kind} for rid in ("one", "two")]}},
        "resources": {
            "created": [
                {
                    "rid": rid,
                    "kind": kind,
                    "locator": {"value": value},
                    "persistent": True,
                    "creator_queue": queue,
                    "external": False,
                    "resident_queues": [queue],
                }
                for rid, value in (("one", first_value), ("two", 1))
            ],
            "worker_queue": queue,
            "resident_rids": ["one", "two"],
        },
    }


async def waiting(handle, address):
    async with asyncio.timeout(10):
        while True:
            view = await handle.query(AxcutorInterpreterWorkflow.get_runtime_snapshot)

            def contains(frames):
                return any(address in f["waiting"] or contains(f["children"]) for f in frames)

            if contains(view["frames"]):
                return view
            await asyncio.sleep(0.01)


def test_descriptor_invariants_identity_and_external_canonical_mapping():
    kind = "infra:File"
    schema = {"f": {"type": "handle", "kind": kind}}
    registry = ResourceRegistry({kind: {"locator": INTEGER}}, "run")
    one = registry.materialize_external_inputs({"f": {"$handle": kind, "locator": 1}}, schema)
    two = registry.materialize_external_inputs(
        {"f": {"$handle": kind, "locator": Decimal("1.0")}}, schema
    )
    assert one == two
    rid = one["f"]["$handle"]
    d = registry.entries[rid].descriptor
    assert d["persistent"] is None and d["creator_queue"] is None and d["external"]
    for changes in [
        {"persistent": True},
        {"creator_queue": "pool-worker-" + "a" * 32},
        {"external": False},
        {"locator": None},
        {"external": 1},
    ]:
        with pytest.raises(ValueError):
            description({**d, **changes})
    created = {
        **d,
        "persistent": True,
        "creator_queue": "pool-worker-" + "a" * 32,
        "external": False,
    }
    assert same_identity(
        created, {**created, "external": True, "resident_queues": [created["creator_queue"]]}
    )
    assert not same_identity(d, created)
    registry.entries[rid].pending = True
    assert (
        registry.materialize_external_inputs({"f": {"$handle": kind, "locator": 1}}, schema) != one
    )


@pytest.mark.parametrize("cls", ["File", "Conn"])
async def test_external_store_protocol_and_one_way_delete_revocation(public_resources, cls):
    module = public_resources
    kind = f"{module.__name__}:{cls}"
    registry = ResourceRegistry(
        {kind: {"locator": {"type": "object", "properties": {"value": INTEGER}}}}
    )
    value = registry.materialize_external_inputs(
        {"f": external(module, cls=cls)}, {"f": handle_type(module, cls)}
    )
    raw = value["f"]
    d = registry.entries[raw["$handle"]].descriptor
    physical = store()
    context = ResourceContext(physical, [d])
    assert (await context.get(Handle(rid=raw["$handle"], kind=kind))).value == 7
    assert physical.entries[raw["$handle"]].descriptor["persistent"] is None
    assert not physical.entries[raw["$handle"]].delete_external
    assert (await physical.dispose(d, deleted=True))["status"] == "unconfirmed"
    assert (await physical.dispose(d, deleted=False))["status"] == "unconfirmed"
    await context.close(set())
    assert physical.entries == {}
    assert module.events == [("restore", 7)] + ([] if cls == "File" else [("dispose", 7)])


async def test_transferred_internal_descriptor_cannot_regain_deletion(public_resources):
    module = public_resources
    physical = store()
    creator = ResourceContext(physical, [])
    handle = await creator.put(module.File(9))
    old = description(creator.allowed[handle.rid])
    await creator.close({handle.rid})
    borrower = ResourceContext(physical, [{**old, "external": True}])
    await borrower.get(handle)
    await physical.dispose({**old, "external": True}, deleted=True)
    await physical.dispose(old, deleted=False)
    await borrower.close(set())
    assert (await physical.dispose(old, restore=True))["status"] == "missing"
    assert physical.entries == {} and module.events == []


async def test_close_publishes_all_orphans_before_waiting_for_resource_locks(
    public_resources, monkeypatch
):
    module = public_resources
    physical = store()
    left, right = ResourceContext(physical, []), ResourceContext(physical, [])
    blocker = await left.put(module.File(201))
    shared_left = await left.put(module.File(202))
    await right.put(module.File(202))
    borrower = ResourceContext(physical, [left.allowed[blocker.rid], left.allowed[shared_left.rid]])
    lock_held = {201: asyncio.Event(), 202: asyncio.Event()}
    release_lock, close_entered = asyncio.Event(), asyncio.Event()
    original_locator = module.File.locator

    async def locator(self):
        lock_held[self.value].set()
        await release_lock.wait()
        return original_locator(self)

    async def close_left():
        # Setting the event does not yield: the caller resumes only after close
        # has reached a held resource lock.
        close_entered.set()
        await left.close(set())

    monkeypatch.setattr(module.File, "locator", locator)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        # Hold either possible first lock through the public locator protocol,
        # without depending on cleanup's resource iteration order.
        borrowed = [tasks.create_task(borrower.get(h)) for h in (blocker, shared_left)]
        try:
            await asyncio.gather(*(event.wait() for event in lock_held.values()))
            left_close = tasks.create_task(close_left())
            await close_entered.wait()
            assert not left_close.done()
            await right.close(set())
        finally:
            release_lock.set()
            await asyncio.gather(*borrowed)
            await borrower.close(set())
    assert sorted(module.events) == [("dispose", 201), ("dispose", 202)]


@pytest.mark.parametrize("cls", ["File", "Conn"])
@pytest.mark.parametrize("child", [False, True])
async def test_root_input_borrowing_projects_child_trace_and_preserves_host_entity(
    execution, public_resources, cls, child
):
    module = public_resources
    h = handle_type(module, cls)
    seen = []

    async def consume(payload, signal):
        raw = payload["input_snapshot"]["f"]
        (d,) = payload["resource_context"]["resources"]
        assert d["external"] and d["persistent"] is None and d["creator_queue"] is None
        seen.append(raw)
        assert (await resources.get(Handle(rid=raw["$handle"], kind=raw["kind"]))).value == 23
        return {"output": {"f": raw}}

    sub = {
        "input": {"f": h},
        "output": {"f": h},
        "run": {"executor": "builtin", "content": "consume"},
    }
    document = graph(
        {"sub": call("sub", {"f": "f"}, {"f": "f"})},
        ["start -> sub", "sub -> end"],
        inputs={"f": h},
        outputs={"f": h},
    )
    inputs = {"f": external(module, 23, cls)}
    async with execution({"consume": consume}) as run:
        handle = await run.start(
            document,
            inputs,
            packages={"sub": {"format": "json", "text": dumps(sub)}},
            options={"record_trace": True, "child_workflow_history_events": 1 if child else None},
        )
        assert (await run.finish(handle))["output"] == inputs
        trace = await handle.query("get_execution_trace")
        for span in trace["spans"]:
            for key in ("input_json", "output_json"):
                if span[key] is not None:
                    assert seen[0]["$handle"] not in span[key]
        if child:
            span = next(s for s in trace["spans"] if s.get("child_execution"))
            ref = span["child_execution"]
            child_trace = await run.server.client.get_workflow_handle(
                ref["workflow_id"], run_id=ref["run_id"]
            ).query("get_execution_trace")
            root_span = child_trace["spans"][0]
            assert loads(root_span["input_json"]) == inputs
            assert loads(root_span["output_json"]) == inputs
        assert module.events == [("restore", 23)] + ([] if cls == "File" else [("dispose", 23)])


@pytest.mark.parametrize("returned", [True, False])
async def test_created_root_output_transfers_only_on_success(execution, public_resources, returned):
    module = public_resources
    h = handle_type(module, "File")

    async def make(payload, signal):
        assert set(payload["resource_context"]["locator_schemas"]) == {h["kind"]}
        return {"output": {"f": envelope(await resources.put(module.File(31)))}}

    document = graph(
        {"make": action("make", outputs={"f": h})},
        ["start -> make", "make -> end"],
        outputs={"f": h} if returned else {},
    )
    async with execution({"make": make}) as run:
        result = await run.execute(document)
        assert result["output"] == ({"f": external(module, 31)} if returned else {})
        assert module.events == ([] if returned else [("dispose", 31)])


async def test_worker_public_locator_contract_failure_cleans_allocation(public_resources):
    module = public_resources
    physical = store()

    async def make(payload, signal):
        return {"output": {"f": envelope(await resources.put(module.File("wrong")))}}

    payload = request({"f": handle_type(module, "File")})
    payload["resource_context"]["locator_schemas"] = {
        f"{module.__name__}:File": {"type": "object", "properties": {"value": INTEGER}}
    }
    with pytest.raises(exceptions.ApplicationError) as error:
        await ActivityEnvironment().run(
            RunActivities({"custom": make}, store=physical).execute_run, dumps(payload)
        )
    assert error.value.type == "contract"
    assert physical.entries == {} and module.events == [("dispose", "wrong")]


def test_registry_validates_batch_before_accepting_and_canonicalizing(public_resources):
    module = public_resources
    kind = handle_type(module, "File")["kind"]
    schemas = {kind: {"locator": {"type": "object", "properties": {"value": INTEGER}}}}
    registry = ResourceRegistry(schemas, "parent")
    queue = "pool-worker-" + "a" * 32
    response = created_files(module, queue, "wrong")
    output = dict.fromkeys(("one", "two"), kind)
    with pytest.raises(ValueError, match="literal"):
        registry.accept(response["resources"], [], output)
    assert registry.entries == {}
    response["resources"]["created"][0]["locator"]["value"] = 1
    assert registry.accept(response["resources"], [], output) == {"two": "one"}
    assert not registry.entries["one"].descriptor["external"]
    duplicate = registry.entries["two"]
    assert duplicate.pending and duplicate.owned and duplicate.descriptor["external"]


@pytest.mark.parametrize("malformed", [False, True])
async def test_workflow_checks_custom_executor_locators_and_rewrites_output_batch(
    execution, public_resources, malformed
):
    module = public_resources
    h = handle_type(module, "File")
    observed, cleaned = [], []

    @activity.defn(name="execute_run")
    async def remote(request_json: str) -> str:
        payload = loads(request_json)
        assert payload["resource_context"]["locator_schemas"] == {
            h["kind"]: {
                "type": "object",
                "properties": {"value": INTEGER},
                "required": ["value"],
            }
        }
        return dumps(created_files(module, remote_queue, "wrong" if malformed else 1))

    @activity.defn(name="dispose_resources")
    async def dispose(request_json: str) -> str:
        payload = loads(request_json)
        cleaned.extend(payload["resources"])
        return dumps(
            {"results": [{"rid": d["rid"], "status": "disposed"} for d in payload["resources"]]}
        )

    async def consume(payload, signal):
        left, right = payload["input_snapshot"]["files"]
        observed.append((left, right))
        return {"output": {}}

    document = graph(
        {
            "remote": {
                **action("remote", outputs={"files": array(h)}),
                "run": {"executor": "custom"},
            },
            "consume": action("consume", ["files"]),
        },
        ["start -> remote", "remote -> consume", "consume -> end"],
    )
    async with execution({"consume": consume}) as run:
        remote_queue = run.queue + "-remote-worker-" + "a" * 32
        async with Worker(run.server.client, task_queue=remote_queue, activities=[remote, dispose]):
            options = {"task_queues": {"custom": remote_queue}}
            if malformed:
                failure = await run.failed(document, options=options)
                assert failure.type == "contract" and "literal" in failure.message
                assert observed == [] and cleaned == []
            else:
                assert (await run.execute(document, options=options))["output"] == {}
                assert observed == [({"$handle": "one", "kind": h["kind"]},) * 2]
                assert {d["rid"]: d["external"] for d in cleaned} == {
                    "one": False,
                    "two": True,
                }


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("origin", ["external", "internal"])
async def test_signal_keeps_external_response_and_merges_public_identity_across_child(
    execution, public_resources, child, origin
):
    module = public_resources
    h = handle_type(module, "File")
    received = []

    async def observe(payload, signal):
        received.append(payload["input_snapshot"]["f"])
        return {"output": {}}

    async def make(payload, signal):
        return {"output": {"f": envelope(await resources.put(module.File(41)))}}

    sub = {"input": {}, "output": {"f": h}, "run": {"executor": "signal"}}
    document = graph(
        {
            "observe": action("observe", ["f"]),
            "sub": call("sub", outputs={"f": "selected"}),
            "compare": builtin(
                "compare",
                {"op": literal("eq"), "left": "f", "right": "selected"},
                {"result": "same"},
            ),
        },
        ["start -> observe", "observe -> sub", "sub -> compare", "compare -> end"],
        inputs={"f": h} if origin == "external" else {},
        outputs={"same": {"type": "boolean"}},
        variables={"selected": h},
    )
    if origin == "internal":
        document["graph"]["nodes"]["make"] = action("make", outputs={"f": h})
        document["graph"]["edges"][0] = "start -> make"
        document["graph"]["edges"].append("make -> observe")
    async with execution({"observe": observe, "make": make}) as run:
        handle = await run.start(
            document,
            {"f": external(module, 41)} if origin == "external" else {},
            packages={"sub": {"format": "json", "text": dumps(sub)}},
            options={"record_trace": True, "child_workflow_history_events": 1 if child else None},
        )
        destination = handle
        if child:
            async with asyncio.timeout(10):
                while True:
                    snapshot = await handle.query("get_runtime_snapshot")
                    children = snapshot["frames"][0]["children"] if snapshot["frames"] else []
                    if children and children[0].get("child_execution"):
                        break
                    await asyncio.sleep(0.01)
            remote = children[0]["child_execution"]
            destination = run.server.client.get_workflow_handle(
                remote["workflow_id"], run_id=remote["run_id"]
            )
        snapshot = await waiting(destination, "sub" if child else "main/sub")
        assert received[0]["$handle"] not in dumps(snapshot)
        response = loads(
            await destination.execute_update(
                "deliver_signal",
                dumps(
                    {
                        "address": "sub" if child else "main/sub",
                        "output": {"f": external(module, 41)},
                    }
                ),
            )
        )
        assert response["output"] == {"f": external(module, 41)}
        assert (await run.finish(handle))["output"] == {"same": True}
        assert module.events == ([] if origin == "external" else [("dispose", 41)])


async def test_invalid_signal_does_not_claim_or_materialize_pending_deletion(
    execution, public_resources, monkeypatch
):
    module = public_resources
    h = handle_type(module, "File")
    disposing, release = asyncio.Event(), asyncio.Event()
    original_dispose = module.File.dispose

    async def dispose(self):
        disposing.set()
        await release.wait()
        await original_dispose(self)

    monkeypatch.setattr(module.File, "dispose", dispose)

    async def make(payload, signal):
        return {"output": {"f": envelope(await resources.put(module.File(51)))}}

    branch = graph({"make": action("make", outputs={"f": h})}, ["start -> make", "make -> end"])

    document = graph(
        {
            "create": inline(branch),
            "wait": {"run": {"executor": "signal"}, "output": {"selected": h}},
        },
        ["start -> [create, wait] -> end"],
    )
    async with execution({"make": make}) as run:
        handle = await run.start(document)
        await waiting(handle, "main/wait")
        await asyncio.wait_for(disposing.wait(), 5)
        before = await handle.query("get_runtime_snapshot")
        history_before = await handle.fetch_history()
        for value in [
            external(module, 51),
            {"$handle": "private", "kind": h["kind"]},
            {"$handle": h["kind"], "locator": {"value": "wrong"}},
        ]:
            with pytest.raises(WorkflowUpdateFailedError) as error:
                await handle.execute_update(
                    "deliver_signal", dumps({"address": "main/wait", "output": {"selected": value}})
                )
            assert error.value.cause.type == "SignalValidationError"
        assert await handle.query("get_runtime_snapshot") == before
        history_after = await handle.fetch_history()
        accepted = EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
        assert sum(e.event_type == accepted for e in history_before.events) == sum(
            e.event_type == accepted for e in history_after.events
        )
        release.set()
        await handle.execute_update(
            "deliver_signal",
            dumps({"address": "main/wait", "output": {"selected": external(module, 52)}}),
        )
        assert (await run.finish(handle))["output"] == {}
        assert module.events == [("dispose", 51)]


@pytest.mark.parametrize("child", [False, True])
async def test_all_trace_payloads_project_before_gc_including_builtin_and_foreach(
    execution, public_resources, child
):
    module = public_resources
    # A Worker restore locator is private unless the static header exposes it.
    header = Path(module.__file__).parent / "__ax_resources__.yaml"
    declarations = loads(header.read_text())
    declarations["resources"]["Conn"] = None
    header.write_text(dumps(declarations))
    file_type, tx_type = handle_type(module, "File"), handle_type(module, "Conn")
    private_ids = []

    async def make(payload, signal):
        f = await resources.put(module.File(61))
        tx = await resources.put(module.Conn(62))
        private_ids.extend([f.rid, tx.rid])
        return {"output": {"f": envelope(f), "tx": envelope(tx)}}

    async def echo(payload, signal):
        return {"output": payload["input_snapshot"]}

    async def item(payload, signal):
        return {"output": {"files": payload["input_snapshot"]["copy"]}}

    sub = {
        "input": {"f": file_type, "tx": tx_type},
        "output": {"f": file_type, "tx": tx_type},
        "run": {"executor": "builtin", "content": "echo"},
    }
    document = graph(
        {
            "make": action("make", outputs={"f": file_type, "tx": tx_type}),
            "copy": assign("f", "copy"),
            "sub": call("sub", {"f": "copy", "tx": "tx"}, {"f": "copy", "tx": "tx"}),
            "map": action(
                "item",
                ["copy", "tx", "item"],
                {"files": file_type},
                for_each={"source": "items", "item": "item", "concurrency": 2},
            ),
            "wait": {"run": {"executor": "signal"}, "input": ["files", "tx"]},
        },
        [
            "start -> make",
            "make -> copy",
            "copy -> sub",
            "sub -> map",
            "map -> wait",
            "wait -> end",
        ],
        inputs={"items": array()},
        variables={"copy": file_type, "files": array(file_type)},
    )
    async with execution({"make": make, "echo": echo, "item": item}) as run:
        handle = await run.start(
            document,
            {"items": [1, 2]},
            packages={"sub": {"format": "json", "text": dumps(sub)}},
            options={"record_trace": True, "child_workflow_history_events": 1 if child else None},
        )
        view = await waiting(handle, "main/wait")
        env = loads(view["frames"][0]["env_json"])
        assert env["f"] == external(module, 61)
        assert env["tx"] == {"$handle": tx_type["kind"]}
        assert env["files"] == [external(module, 61)] * 2
        await handle.execute_update("deliver_signal", dumps({"address": "main/wait", "output": {}}))
        await run.finish(handle)
        trace = await handle.query("get_execution_trace")
        for rid in private_ids:
            assert rid not in dumps(trace) and rid not in dumps(view)
        spans = trace["spans"]
        copy_span = next(s for s in spans if s["address"] == "main/copy")
        assert loads(copy_span["input_json"]) == {"source": external(module, 61)}
        assert loads(copy_span["output_json"]) == {"copy": external(module, 61)}
        each = next(s for s in spans if s["kind"] == "for_each")
        assert loads(each["output_json"]) == {"files": [external(module, 61)] * 2}
        assert sorted(module.events) == [("dispose", 61), ("dispose", 62)]


@pytest.mark.parametrize("after_lock", [False, True])
async def test_root_cancellation_before_transfer_deletes_but_after_lock_keeps_success(
    execution, public_resources, monkeypatch, after_lock
):
    module = public_resources
    entered, release = asyncio.Event(), asyncio.Event()
    original = ResourceStore.dispose

    async def dispose(self, descriptor, **kwargs):
        if (
            descriptor["locator"] == {"value": 71 if after_lock else 72}
            and descriptor["external"] is after_lock
        ):
            entered.set()
            await release.wait()
        return await original(self, descriptor, **kwargs)

    monkeypatch.setattr(ResourceStore, "dispose", dispose)
    h = handle_type(module, "File")

    async def make(payload, signal):
        return {
            "output": {
                "f": envelope(await resources.put(module.File(71))),
                "orphan": envelope(await resources.put(module.File(72))),
            }
        }

    document = graph(
        {"make": action("make", outputs={"f": h, "orphan": h})},
        ["start -> make", "make -> end"],
        outputs={"f": h},
    )
    async with execution({"make": make}) as run:
        handle = await run.start(document)
        await asyncio.wait_for(entered.wait(), 5)
        await handle.cancel()
        release.set()
        if after_lock:
            assert (await run.finish(handle))["output"] == {"f": external(module, 71)}
            assert module.events == [("dispose", 72)]
        else:
            with pytest.raises(WorkflowFailureError) as error:
                await handle.result()
            assert isinstance(error.value.cause, exceptions.CancelledError)
            assert sorted(module.events) == [("dispose", 71), ("dispose", 72)]


async def test_late_resource_update_after_export_cannot_resurrect_external_deletion(
    execution, public_resources, monkeypatch
):
    module = public_resources
    entered, release = asyncio.Event(), asyncio.Event()
    late_cleaned = asyncio.Event()
    original = ResourceStore.dispose
    captured = {}

    async def dispose(self, descriptor, **kwargs):
        if descriptor["external"] and descriptor["locator"] == {"value": 82}:
            entered.set()
            await release.wait()
        result = await original(self, descriptor, **kwargs)
        if "-late-export-worker-" in self.worker_queue and result["status"] == "disposed":
            late_cleaned.set()
        return result

    monkeypatch.setattr(ResourceStore, "dispose", dispose)
    h = handle_type(module, "File")

    async def make(payload, signal):
        return {
            "output": {
                "a": envelope(await resources.put(module.File(81))),
                "b": envelope(await resources.put(module.File(82))),
            }
        }

    async def use(payload, signal):
        captured.update(
            call_id=payload["resource_context"]["call_id"],
            source_run_id=activity.info().workflow_run_id,
            descriptor=payload["resource_context"]["resources"][0],
        )
        return {"output": {}}

    document = graph(
        {"make": action("make", outputs={"a": h, "b": h}), "use": action("use", ["a"])},
        ["start -> make", "make -> use", "use -> end"],
        outputs={"a": h, "b": h},
    )
    async with execution({"make": make, "use": use}) as run:
        handle = await run.start(document)
        await asyncio.wait_for(entered.wait(), 5)
        d = captured["descriptor"]
        async with create_worker(run.server.client, run.queue + "-late-export") as late:
            context = ResourceContext(late.store, [d])
            await context.get(Handle(rid=d["rid"], kind=d["kind"]))
            await context.close(set())
            await handle.signal(
                "resource_update",
                dumps(
                    {
                        "call_id": captured["call_id"],
                        "source_run_id": captured["source_run_id"],
                        "resources": {
                            "created": [],
                            "worker_queue": late.store.worker_queue,
                            "resident_rids": [d["rid"]],
                        },
                    }
                ),
            )
            await asyncio.wait_for(late_cleaned.wait(), 5)
            assert late.store.entries == {}
            release.set()
            assert (await run.finish(handle))["output"] == {
                "a": external(module, 81),
                "b": external(module, 82),
            }
        assert module.events == [("restore", 81)]


async def test_late_resource_update_before_export_lock_preserves_retained_root_result(
    execution, public_resources, monkeypatch
):
    module = public_resources
    entered, release, late_cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ResourceStore.dispose
    captured = {}

    async def dispose(self, descriptor, **kwargs):
        if descriptor["locator"] == {"value": 122} and not descriptor["external"]:
            info = activity.info()
            trace = await run.server.client.get_workflow_handle(
                info.workflow_id, run_id=info.workflow_run_id
            ).query("get_execution_trace")
            if trace["spans"][0]["status"] != "completed":
                # Earlier GC remains unconfirmed, leaving a real unexported
                # allocation for the root's final cleanup before success locks.
                return {"rid": descriptor["rid"], "status": "unconfirmed"}
            entered.set()
            await release.wait()
        result = await original(self, descriptor, **kwargs)
        if "-late-root-worker-" in self.worker_queue and result["status"] == "disposed":
            late_cleaned.set()
        return result

    monkeypatch.setattr(ResourceStore, "dispose", dispose)
    h = handle_type(module, "File")

    async def make(payload, signal):
        return {
            "output": {
                "f": envelope(await resources.put(module.File(121))),
                "unused": envelope(await resources.put(module.File(122))),
            }
        }

    async def use(payload, signal):
        captured.update(
            call_id=payload["resource_context"]["call_id"],
            source_run_id=activity.info().workflow_run_id,
            descriptor=payload["resource_context"]["resources"][0],
        )
        return {"output": {}}

    document = graph(
        {"make": action("make", outputs={"f": h, "unused": h}), "use": action("use", ["f"])},
        ["start -> make", "make -> use", "use -> end"],
        outputs={"f": h},
    )
    async with execution({"make": make, "use": use}) as run:
        handle = await run.start(document, options={"record_trace": True})
        try:
            await asyncio.wait_for(entered.wait(), 10)
            d = captured["descriptor"]
            assert not d["external"]
            async with create_worker(run.server.client, run.queue + "-late-root") as late:
                context = ResourceContext(late.store, [d])
                await context.get(Handle(rid=d["rid"], kind=d["kind"]))
                await context.close(set())
                await handle.signal(
                    "resource_update",
                    dumps(
                        {
                            "call_id": captured["call_id"],
                            "source_run_id": captured["source_run_id"],
                            "resources": {
                                "created": [],
                                "worker_queue": late.store.worker_queue,
                                "resident_rids": [d["rid"]],
                            },
                        }
                    ),
                )
                assert await handle.query("get_runtime_snapshot") == {"frames": []}
                assert d["rid"] in late.store.entries and not late_cleaned.is_set()
                assert module.events == [("restore", 121)]
                release.set()
                assert (await run.finish(handle))["output"] == {"f": external(module, 121)}
                assert late_cleaned.is_set() and late.store.entries == {}
            assert module.events == [("restore", 121), ("dispose", 122)]
        finally:
            release.set()


async def test_activity_duplicate_locators_evict_wrappers_and_delete_entity_once(
    execution, public_resources
):
    module = public_resources
    h = handle_type(module, "File")
    observed = []

    async def make(payload, signal):
        return {
            "output": {
                "files": [
                    envelope(await resources.put(module.File(91))),
                    envelope(await resources.put(module.File(91))),
                ]
            }
        }

    async def consume(payload, signal):
        left, right = payload["input_snapshot"]["files"]
        assert left == right
        assert module.events == []
        for value in (left, right):
            observed.append(await resources.get(Handle(rid=value["$handle"], kind=value["kind"])))
        return {"output": {}}

    document = graph(
        {
            "make": action("make", outputs={"files": array(h)}),
            "consume": action("consume", ["files"]),
        },
        ["start -> make", "make -> consume", "consume -> end"],
    )
    async with execution({"make": make, "consume": consume}) as run:
        assert (await run.execute(document))["output"] == {}
        assert observed[0] is observed[1]
        assert module.events == [("dispose", 91)]


@pytest.mark.parametrize("outcome", ["return_input", "export_one", "fail"])
async def test_activity_orphan_aliases_preserve_survivors_and_delete_new_entity_once(
    execution, public_resources, outcome
):
    module = public_resources
    h = handle_type(module, "File")

    async def make(payload, signal):
        one = await resources.put(module.File(101))
        if outcome == "return_input":
            # The input authorization protects its entity even without get().
            return {"output": payload["input_snapshot"]}
        await resources.put(module.File(101))
        if outcome == "fail":
            raise RuntimeError("both new wrappers are unexported")
        return {"output": {"f": envelope(one)}}

    document = {
        "input": {"f": h} if outcome == "return_input" else {},
        "output": {"f": h},
        "run": {"executor": "builtin", "content": "make"},
    }
    async with execution({"make": make}) as run:
        if outcome == "fail":
            failure = await run.failed(document)
            assert failure.type == "execution"
            assert module.events == [("dispose", 101)]
        else:
            result = await run.execute(
                document, {"f": external(module, 101)} if outcome == "return_input" else {}
            )
            assert result["output"] == {"f": external(module, 101)}
            assert module.events == []


@pytest.mark.parametrize("export_later", [False, True])
async def test_concurrent_orphan_aliases_retain_one_entity_owner(
    execution, public_resources, monkeypatch, export_later
):
    module = public_resources
    h = handle_type(module, "File")
    first_allocated, second_allocated = asyncio.Event(), asyncio.Event()
    slow_started, later_cleanup, release_slow = asyncio.Event(), asyncio.Event(), asyncio.Event()
    later_activity_id = None
    original_dispose, original_store_dispose = module.File.dispose, ResourceStore.dispose

    async def dispose(self):
        if self.value == 131:
            slow_started.set()
            await release_slow.wait()
        await original_dispose(self)

    async def store_dispose(self, descriptor, **kwargs):
        if activity.in_activity() and activity.info().activity_id == later_activity_id:
            # Local alias adjudication precedes every disposal request. The
            # earlier Activity still holds its slow destructor and shared alias.
            later_cleanup.set()
        return await original_store_dispose(self, descriptor, **kwargs)

    monkeypatch.setattr(module.File, "dispose", dispose)
    monkeypatch.setattr(ResourceStore, "dispose", store_dispose)

    async def first(payload, signal):
        await resources.put(module.File(131))
        await resources.put(module.File(132))
        first_allocated.set()
        await second_allocated.wait()
        return {"output": {}}

    async def second(payload, signal):
        nonlocal later_activity_id
        later_activity_id = activity.info().activity_id
        await first_allocated.wait()
        f = await resources.put(module.File(132))
        second_allocated.set()
        await slow_started.wait()
        return {"output": {"f": envelope(f)} if export_later else {}}

    document = graph(
        {
            "first": action("first"),
            "second": action("second", outputs={"f": h} if export_later else {}),
        },
        ["start -> [first, second] -> end"],
        outputs={"f": h} if export_later else {},
    )
    async with execution({"first": first, "second": second}) as run:
        handle = await run.start(document)
        try:
            await asyncio.wait_for(later_cleanup.wait(), 10)
            assert module.events == []
        finally:
            release_slow.set()
        assert (await run.finish(handle))["output"] == (
            {"f": external(module, 132)} if export_later else {}
        )
        assert sorted(module.events) == [("dispose", 131)] + (
            [] if export_later else [("dispose", 132)]
        )


async def test_retry_attempt_trace_projects_handles_before_later_gc(execution, public_resources):
    module = public_resources
    header = Path(module.__file__).parent / "__ax_resources__.yaml"
    declarations = loads(header.read_text())
    declarations["resources"]["Conn"] = None
    header.write_text(dumps(declarations))
    file_type, private_type = handle_type(module, "File"), handle_type(module, "Conn")
    ids, attempts = [], []

    async def make(payload, signal):
        f = await resources.put(module.File(111))
        conn = await resources.put(module.Conn(112))
        ids.extend([f.rid, conn.rid])
        return {"output": {"f": envelope(f), "conn": envelope(conn)}}

    async def retry(payload, signal):
        attempts.append(activity.info().attempt)
        if len(attempts) == 1:
            raise RuntimeError("retry the handle-carrying node")
        return {"output": payload["input_snapshot"]}

    document = graph(
        {
            "make": action("make", outputs={"f": file_type, "conn": private_type}),
            "retry": action(
                "retry",
                ["f", "conn"],
                {"f": file_type, "conn": private_type},
                retry={"max_attempts": 2, "backoff_ms": 0, "on": ["execution", "contract"]},
            ),
        },
        ["start -> make", "make -> retry", "retry -> end"],
    )
    async with execution({"make": make, "retry": retry}) as run:
        handle = await run.start(document, options={"record_trace": True})
        assert (await run.finish(handle))["output"] == {}
        assert attempts == [1, 1]  # Separate interpreter Attempts, not SDK-native retries.
        assert sorted(module.events) == [("dispose", 111), ("dispose", 112)]
        trace = await handle.query("get_execution_trace")
        spans = [s for s in trace["spans"] if s["kind"] == "attempt"]
        assert [s["status"] for s in spans] == ["failed", "completed"]
        projected = {"f": external(module, 111), "conn": {"$handle": private_type["kind"]}}
        assert all(loads(s["input_json"]) == projected for s in spans)
        assert spans[0]["output_json"] is None
        assert loads(spans[1]["output_json"]) == projected
        assert all(rid not in dumps(trace) for rid in ids)
