"""Real resource protocols, Activity ownership, Temporal handoffs and replay."""

import asyncio
import importlib
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, Handle, compile_bundle, resource, resources
from axcutor_runtime.activities import RunActivities
from axcutor_runtime.builtin import compute_contract_sha256, synthesize_contract_module
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.resource_model import ResourceRegistry, description, scan
from axcutor_runtime.resources import ResourceContext, ResourceStore
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, action, array, assign, call, graph, inline, literal
from pydantic import ValidationError
from temporalio import activity, exceptions
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def resource_package(tmp_path, monkeypatch):
    name = "resource_test_" + uuid4().hex
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text(
        "from typing import Any\n"
        "from axcutor_runtime import resource\n"
        "events: list[tuple[str, Any]] = []\n"
        "@resource\n"
        "class Tx:\n"
        "    def __init__(self, value: int = 7) -> None:\n"
        "        self.value = value\n"
        "        self.fail = False\n"
        "    async def dispose(self) -> None:\n"
        "        events.append(('dispose', self.value))\n"
        "        if self.fail:\n"
        "            raise RuntimeError('dispose failed')\n"
        "@resource\n"
        "class Conn(Tx):\n"
        "    def locator(self) -> Any:\n"
        "        return {'value': self.value}\n"
        "    @classmethod\n"
        "    async def restore(cls, locator: Any) -> 'Conn':\n"
        "        events.append(('restore', locator['value']))\n"
        "        return cls(locator['value'])\n"
        "@resource(persistent=True)\n"
        "class File(Conn):\n"
        "    pass\n"
        "class Undecorated(Tx):\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(name)
    yield module
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    importlib.invalidate_caches()


def handle_type(module, cls="Tx"):
    return {"type": "handle", "kind": f"{module.__name__}:{cls}"}


def store(queue="pool", mounts=None):
    return ResourceStore(queue + "-worker-" + uuid4().hex, mounts)


def envelope(handle):
    return {"$handle": handle.rid, "kind": handle.kind}


def request(outputs=None, inputs=None, values=None, allowed=None):
    return {
        "workflow_id": "main",
        "run_spec": {"executor": "custom"},
        "input_schema": inputs or {},
        "input_snapshot": values or {},
        "output_schema": outputs or {},
        "branches_contract": {},
        "attempt_timeout_ms": 2000,
        "heartbeat_interval_ms": 10,
        "resource_context": {"resources": allowed or [], "call_id": "test"},
    }


def test_public_handle_is_frozen_and_hashes_by_id():
    one = Handle(rid="res_one", kind="infra:Tx")
    assert one == Handle(rid="res_one", kind="infra:Other")
    assert len({one, Handle(rid="res_one", kind="infra:Tx")}) == 1
    with pytest.raises(ValidationError):
        one.rid = "changed"
    for kwargs in ({"rid": "", "kind": "infra:Tx"}, {"rid": "r", "kind": "infra:_Tx"}):
        with pytest.raises(ValidationError):
            Handle(**kwargs)
    with pytest.raises(TypeError):

        @resource
        class Local:
            pass


async def test_pure_activity_calls_release_records_and_decode_each_envelope_once(monkeypatch):
    import axcutor_runtime.workflow as interpreter_module

    document = graph({"work": action("work")}, ["start -> work", "work -> end"])
    bundle = loads(compile_bundle("main", {"main": {"format": "json", "text": dumps(document)}}))
    node = bundle["workflows"]["main"]["body"]["graph"]["nodes"]["work"]
    response = dumps(
        {
            "result": {"output": {}},
            "resources": {
                "created": [],
                "worker_queue": "pool-worker-" + "a" * 32,
                "resident_rids": [],
            },
        }
    )
    decoded = 0

    def decode(value):
        nonlocal decoded
        if value == response:
            decoded += 1
        return loads(value)

    def start(*args, **kwargs):
        result = asyncio.get_running_loop().create_future()
        result.set_result(response)
        return result

    monkeypatch.setattr(interpreter_module, "loads", decode)
    monkeypatch.setattr(interpreter_module.workflow, "start_activity", start)
    monkeypatch.setattr(
        interpreter_module.workflow,
        "info",
        lambda: SimpleNamespace(task_queue="pool", run_id="run"),
    )
    interpreter = AxcutorInterpreterWorkflow()
    interpreter._options = interpreter_module.parse_runtime_options({})
    for _ in range(1000):
        assert await interpreter._run_activity(node, {}, False) == {"branch": None, "output": {}}
    assert decoded == 1000
    assert interpreter._registry.entries == {}
    assert interpreter._resource_calls == {}


async def test_store_rejects_unauthorized_handles_and_disposes_only_after_all_users(
    resource_package,
):
    module = resource_package
    physical = store()
    creator = ResourceContext(physical, [])
    instance = module.Tx()
    handle = await creator.put(instance)
    with pytest.raises(ValueError, match="twice"):
        await creator.put(instance)
    with pytest.raises(TypeError):
        await creator.put(module.Undecorated())
    descriptor = creator.allowed[handle.rid]
    borrower = ResourceContext(physical, [descriptor])
    assert await borrower.get(handle) is instance
    with pytest.raises(PermissionError):
        await ResourceContext(physical, []).get(handle)
    with pytest.raises(TypeError):
        await borrower.get(envelope(handle))
    assert (await physical.dispose(descriptor))["status"] == "unconfirmed"
    with pytest.raises(RuntimeError, match="pending"):
        await ResourceContext(physical, [descriptor]).get(handle)
    await creator.close({handle.rid})
    assert module.events == []
    await borrower.close(set())
    assert module.events == [("dispose", 7)]
    assert physical.entries == {}


async def test_failed_disposal_preserves_instance_and_shutdown_preserves_owned_external_objects(
    resource_package,
):
    module = resource_package
    physical = store()
    context = ResourceContext(physical, [])
    instance = module.Tx()
    instance.fail = True
    handle = await context.put(instance)
    await context.close(set())
    assert physical.entries[handle.rid].value is instance
    assert physical.entries[handle.rid].pending
    await physical.release(context, context.allocated_rids)
    assert module.events == [("dispose", 7)]
    instance.fail = False
    assert (await physical.dispose(context.allowed[handle.rid]))["status"] == "disposed"
    external = ResourceContext(physical, [])
    file = await external.put(module.File(8))
    await external.close({file.rid})
    await physical.shutdown()
    assert module.events == [("dispose", 7), ("dispose", 7)]
    assert physical.entries == {}


async def test_recovery_uses_copied_lossless_locator_and_exact_class(resource_package):
    module = resource_package
    source = store()
    creator = ResourceContext(source, [])
    handle = await creator.put(module.Conn(2**100))
    descriptor = description(creator.allowed[handle.rid])
    target = store()
    left, right = ResourceContext(target, [descriptor]), ResourceContext(target, [descriptor])
    a, b = await asyncio.gather(left.get(handle), right.get(handle))
    assert a is b and a.value == 2**100
    assert module.events == [("restore", 2**100)]
    descriptor["locator"]["value"] = 0
    assert left.allowed[handle.rid]["locator"]["value"] == 2**100
    await left.close(set())
    await right.close(set())
    assert (await target.dispose(creator.allowed[handle.rid]))["status"] == "disposed"
    transient = await creator.put(module.Tx())
    with pytest.raises(LookupError, match="transient"):
        await ResourceContext(target, [creator.allowed[transient.rid]]).get(transient)
    await creator.close(set())


async def test_revoked_restore_never_deletes_the_borrowed_persistent_object(resource_package):
    module = resource_package
    source, target = store(), store()
    creator = ResourceContext(source, [])
    handle = await creator.put(module.File(67))
    borrower = ResourceContext(target, [creator.allowed[handle.rid]])
    entered, release = asyncio.Event(), asyncio.Event()
    original_restore = module.File.restore

    async def restore(cls, locator):
        entered.set()
        await release.wait()
        return await original_restore(locator)

    module.File.restore = classmethod(restore)
    task = asyncio.create_task(borrower.get(handle))
    await entered.wait()
    await borrower.close(set())
    release.set()
    with pytest.raises(RuntimeError, match="closed during restore"):
        await task
    assert target.entries == {}
    assert module.events == [("restore", 67)]
    await creator.close({handle.rid})
    await source.shutdown()
    assert module.events == [("restore", 67)]


async def test_resource_mounts_are_bijective_and_protocol_validation_precedes_takeover(
    resource_package,
):
    module = resource_package
    physical = store(mounts={"logical": module.__name__})
    context = ResourceContext(physical, [])
    handle = await context.put(module.Tx())
    assert handle.kind == "logical:Tx"
    assert physical.resolve_resource_class(handle.kind) is module.Tx
    with pytest.raises(ValueError, match="overlap"):
        store(mounts={"a": module.__name__, "b": module.__name__ + ".sub"})
    module.Tx.locator = lambda self: {"value": object()}
    module.Tx.restore = classmethod(lambda cls, locator: cls())
    with pytest.raises(TypeError):
        await context.put(module.Tx())
    assert len(physical.entries) == 1
    await context.close(set())


@pytest.mark.parametrize("outcome", ["success", "failure", "contract", "cancel"])
async def test_activity_orphans_and_failure_residencies_keep_original_selectors(
    resource_package, outcome
):
    module = resource_package
    physical = store()

    async def execute(payload, signal):
        handle = await resources.put(module.Tx())
        if outcome == "failure":
            raise RuntimeError("business failed")
        if outcome == "contract":
            return {"output": {"value": envelope(Handle(rid="invented", kind=handle.kind))}}
        if outcome == "cancel":
            raise asyncio.CancelledError
        return {"output": {}}

    activities = RunActivities({"custom": execute}, store=physical)
    payload = request({"value": handle_type(module)} if outcome == "contract" else {})
    if outcome == "success":
        result = loads(await ActivityEnvironment().run(activities.execute_run, dumps(payload)))
        assert result["result"] == {"branch": None, "output": {}}
        assert result["resources"]["created"] == []
    else:
        expected = asyncio.CancelledError if outcome == "cancel" else exceptions.ApplicationError
        with pytest.raises(expected) as error:
            await ActivityEnvironment().run(activities.execute_run, dumps(payload))
        if outcome != "cancel":
            assert error.value.type == ("execution" if outcome == "failure" else "contract")
            assert loads(error.value.details[0])["drained"]
    assert module.events == [("dispose", 7)]
    assert physical.entries == {}


@pytest.mark.parametrize("orphan_delay", [0, 0.1])
async def test_successful_activity_transfers_only_validated_complete_output(
    resource_package, monkeypatch, orphan_delay
):
    module = resource_package
    physical = store()
    original_dispose = module.Tx.dispose

    async def dispose(self):
        if self.value == 2:
            await asyncio.sleep(orphan_delay)
        await original_dispose(self)

    monkeypatch.setattr(module.Tx, "dispose", dispose)

    async def execute(payload, signal):
        exported = await resources.put(module.Tx(1))
        await resources.put(module.Tx(2))
        return {"output": {"value": envelope(exported)}}

    activities = RunActivities({"custom": execute}, store=physical)
    payload = request({"value": handle_type(module)})
    payload["attempt_timeout_ms"] = 50
    response = loads(await ActivityEnvironment().run(activities.execute_run, dumps(payload)))
    rid = response["result"]["output"]["value"]["$handle"]
    assert [d["rid"] for d in response["resources"]["created"]] == [rid]
    assert set(physical.entries) == {rid}
    assert module.events == [("dispose", 2)]
    await physical.shutdown()
    assert module.events == [("dispose", 2), ("dispose", 1)]


async def test_slow_local_cleanup_does_not_extend_server_attempt_timeout(
    execution, resource_package, monkeypatch
):
    module = resource_package
    cleaned = asyncio.Event()
    original_dispose = module.Tx.dispose

    async def dispose(self):
        await asyncio.sleep(0.15)
        await original_dispose(self)
        cleaned.set()

    monkeypatch.setattr(module.Tx, "dispose", dispose)

    async def execute(payload, signal):
        await resources.put(module.Tx(13))
        return {"output": {}}

    async with execution({"make": execute}) as run:
        document = graph({"make": action("make", timeout_ms=50)}, ["start -> make", "make -> end"])
        assert (await run.failed(document)).type == "timeout"
        scheduled = next(
            e.activity_task_scheduled_event_attributes
            for e in (await run.handles[0].fetch_history()).events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        )
        assert scheduled.start_to_close_timeout.ToTimedelta() == timedelta(milliseconds=50)
        await asyncio.wait_for(cleaned.wait(), 2)
    assert module.events == [("dispose", 13)]


def test_contracts_reference_real_mapped_classes_without_runtime_imports(
    resource_package, tmp_path
):
    module = resource_package
    schema = {"value": {"type": "handle", "kind": "logical:Tx"}}
    source = synthesize_contract_module(
        schema, schema, {}, module_mounts={"logical": module.__name__}
    )
    namespace = {"__name__": "test_contract"}
    exec(compile(source, "<contract>", "exec"), namespace)
    value = {"$handle": "res_1", "kind": "logical:Tx"}
    input_model = namespace["Input"].model_validate({"value": value})
    assert type(input_model.value) is Handle
    assert namespace["Output"](value=input_model.value).value is input_model.value
    with pytest.raises(ValidationError):
        namespace["Output"](value=value)
    assert compute_contract_sha256(schema, schema, {}) == namespace["__axcutor_contract_sha256__"]
    filename = tmp_path / "contract.py"
    filename.write_text(source + "\ndef wrong(value: Input) -> int:\n    return value.value\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyrefly",
            "check",
            "--config",
            str(Path(__file__).resolve().parents[2] / "pyproject.toml"),
            "--search-path",
            str(tmp_path),
            str(filename),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "bad-return" in result.stdout + result.stderr
    assert "import-error" not in result.stdout + result.stderr
    filename.write_text(
        source + "\nfrom axcutor_runtime import resources\n"
        "async def typed_get(value: Input) -> int:\n"
        "    instance = await resources.get(value.value)\n"
        "    return instance.value\n"
    )
    command = [
        sys.executable,
        "-m",
        "pyrefly",
        "check",
        "--config",
        str(Path(__file__).resolve().parents[2] / "pyproject.toml"),
        "--search-path",
        str(tmp_path),
        str(filename),
    ]
    good = subprocess.run(command, capture_output=True, text=True)
    assert good.returncode == 0, good.stdout + good.stderr
    filename.write_text(
        filename.read_text().replace("return instance.value", "return instance.nope")
    )
    bad = subprocess.run(command, capture_output=True, text=True)
    assert bad.returncode != 0 and "missing-attribute" in bad.stdout + bad.stderr
    assert "import-error" not in bad.stdout + bad.stderr


def test_schema_guided_scanning_and_registry_identity_do_not_interpret_business_maps():
    assert (
        scan(
            {"data": {"$handle": "customer-value"}},
            {"data": {"type": "object", "additionalProperties": {"type": "string"}}},
        )
        == {}
    )
    d = {
        "rid": "res_1",
        "kind": "infra:File",
        "locator": {"v": Decimal("1.0")},
        "persistent": True,
        "creator_queue": "pool-worker-" + "a" * 32,
        "resident_queues": ["pool-worker-" + "a" * 32],
    }
    registry = ResourceRegistry()
    metadata = {"created": [d], "worker_queue": d["creator_queue"], "resident_rids": ["res_1"]}
    registry.accept(metadata, [], {"res_1": "infra:File"})
    mutated = description(d)
    mutated["locator"]["v"] = 2
    with pytest.raises(ValueError, match="identity"):
        registry.accept({**metadata, "created": []}, [mutated], None)
    assert registry.route("other", [d], False) == "other"
    transient = {**d, "locator": None, "persistent": False}
    with pytest.raises(ValueError, match="pool"):
        registry.route("other", [transient], False)


@pytest.mark.parametrize("child", [False, True])
async def test_temporal_child_borrowing_full_output_transfer_and_mapping_cleanup(
    execution, resource_package, child
):
    module = resource_package
    h = handle_type(module)

    async def allocate(payload, signal):
        handle = await resources.put(module.Tx(11))
        return {"output": {"tx": envelope(handle)}}

    async def use(payload, signal):
        raw = payload["input_snapshot"]["tx"]
        instance = await resources.get(Handle(rid=raw["$handle"], kind=raw["kind"]))
        assert instance.value == 11
        return {"output": {"value": instance.value}}

    producer = graph(
        {"allocate": action("allocate", outputs={"tx": h})},
        ["start -> allocate", "allocate -> end"],
        outputs={"tx": h},
    )
    consumer = {
        "input": {"tx": h},
        "output": {"value": INTEGER},
        "run": {"executor": "builtin", "content": "use"},
    }
    document = graph(
        {
            "make": call("producer", outputs={"tx": "tx"}),
            "use": call("consumer", {"tx": "tx"}, {"value": "value"}),
            "drop": assign(literal(None), "tx"),
        },
        ["start -> make", "make -> use", "use -> drop", "drop -> end"],
        outputs={"value": INTEGER},
        variables={"tx": {**h, "type": ["handle", "null"]}},
    )
    # Explicit node declarations and call targets must agree on nullable type.
    producer["output"]["tx"] = document["graph"]["variables"]["tx"]
    producer["graph"]["nodes"]["allocate"]["output"]["tx"] = producer["output"]["tx"]
    consumer["input"]["tx"] = producer["output"]["tx"]
    packages = {
        name: {"format": "json", "text": dumps(doc)}
        for name, doc in (("producer", producer), ("consumer", consumer))
    }
    async with execution({"allocate": allocate, "use": use}) as run:
        result = await run.execute(
            document,
            packages=packages,
            options={"child_workflow_history_events": 1} if child else {},
        )
        assert result["output"] == {"value": 11}
        assert module.events == [("dispose", 11)]


async def test_overwritten_resources_are_released_before_later_activity_and_replay(
    execution, resource_package
):
    module = resource_package
    h = {**handle_type(module), "type": ["handle", "null"]}

    async def allocate(payload, signal):
        handle = await resources.put(module.Tx(5))
        return {"output": {"tx": envelope(handle)}}

    async def observe(payload, signal):
        assert module.events == [("dispose", 5)]
        return {"output": {}}

    document = graph(
        {
            "make": action("make", outputs={"tx": h}),
            "drop": assign(literal(None), "tx"),
            "observe": action("observe"),
        },
        ["start -> make", "make -> drop", "drop -> observe", "observe -> end"],
    )
    async with execution({"make": allocate, "observe": observe}) as run:
        assert (await run.execute(document))["output"] == {}


async def test_builtin_handle_inputs_outputs_and_context_share_the_public_class(
    execution, resource_package
):
    module = resource_package
    h = handle_type(module)
    document = graph(
        {
            "make": action(
                f"from {module.__name__} import Tx\nreturn Output(tx=await resources.put(Tx(19)))",
                outputs={"tx": h},
            ),
            "read": action(
                "assert isinstance(input.tx, Handle)\n"
                "return Output(value=(await resources.get(input.tx)).value)",
                ["tx"],
                {"value": INTEGER},
            ),
        },
        ["start -> make", "make -> read", "read -> end"],
        outputs={"value": INTEGER},
    )
    async with execution() as run:
        assert (await run.execute(document))["output"] == {"value": 19}
    assert module.events == [("dispose", 19)]


@pytest.mark.parametrize("position", ["input", "output"])
async def test_real_root_interfaces_reject_nullable_handles_even_with_forged_depth(
    execution, resource_package, position
):
    document = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "pass"}}
    document[position] = {"tx": {**handle_type(resource_package), "type": ["handle", "null"]}}
    async with execution() as run:
        failure = await run.failed(document, {"tx": None} if position == "input" else {})
        assert failure.type == "EngineError" and "pure values" in failure.message


async def test_concurrent_put_accepts_the_same_instance_only_once(resource_package):
    module = resource_package
    physical = store()
    left, right = ResourceContext(physical, []), ResourceContext(physical, [])

    async def locator(self):
        await asyncio.sleep(0)
        return None

    module.Tx.locator = locator
    module.Tx.restore = classmethod(lambda cls, locator: cls())
    value = module.Tx()
    results = await asyncio.gather(left.put(value), right.put(value), return_exceptions=True)
    assert sum(isinstance(result, Handle) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(physical.entries) == 1
    await left.close(set())
    await right.close(set())

    assert physical.entries == {}


async def test_resource_hooks_hide_and_restore_the_bound_activity_context(resource_package):
    module = resource_package
    physical = store()
    checked = []

    async def execute(payload, signal):
        value = module.Tx(17)
        handle = await resources.put(value)
        assert await resources.get(handle) is value

        async def locator(self):
            with pytest.raises(RuntimeError, match="only available"):
                await resources.get(handle)
            with pytest.raises(RuntimeError, match="only available"):
                await resources.put(module.Tx())
            checked.append(self.value)
            return {"value": self.value}

        module.Conn.locator = locator
        connection = module.Conn(23)
        borrowed = await resources.put(connection)
        assert await resources.get(borrowed) is connection
        assert await resources.get(handle) is value
        return {"output": {}}

    await ActivityEnvironment().run(
        RunActivities({"custom": execute}, store=physical).execute_run, dumps(request())
    )
    assert checked and set(checked) == {23}
    assert sorted(module.events) == [("dispose", 17), ("dispose", 23)]
    assert physical.entries == {}


@pytest.mark.parametrize("user", ["business", "descendant"])
async def test_budget_expiry_keeps_local_users_until_actual_drainage(
    resource_package, monkeypatch, user
):
    import axcutor_runtime.activities as activity_module

    monkeypatch.setattr(activity_module, "CLEANUP_SECONDS", 0.05)
    module = resource_package
    physical = store()
    entered, release = asyncio.Event(), asyncio.Event()
    descendant = None

    async def consume(handle):
        assert (await resources.get(handle)).value == 31
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    async def execute(payload, signal):
        nonlocal descendant
        handle = await resources.put(module.Tx(31))
        if user == "descendant":
            descendant = asyncio.create_task(consume(handle))
            await asyncio.Future()
        else:
            await consume(handle)
        return {"output": {}}

    activities = RunActivities({"custom": execute}, store=physical)
    environment = ActivityEnvironment()
    task = asyncio.create_task(environment.run(activities.execute_run, dumps(request())))
    await asyncio.wait_for(entered.wait(), 1)
    environment.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    try:
        (entry,) = physical.entries.values()
        assert entry.value.value == 31
        assert entry.pending and entry.users and module.events == []
        if descendant is not None:
            assert not descendant.done()
    finally:
        release.set()
        if descendant is not None:
            await asyncio.wait_for(descendant, 1)
        await asyncio.wait_for(asyncio.gather(*physical.settlements), 1)
    assert physical.entries == {} and module.events == [("dispose", 31)]


async def test_foreach_handle_results_survive_the_barrier_then_clear(execution, resource_package):
    module = resource_package
    h = handle_type(module)

    async def allocate(payload, signal):
        number = payload["input_snapshot"]["item"]
        if number == 2:
            await asyncio.sleep(0.03)
        handle = await resources.put(module.Tx(number))
        return {"output": {"handles": envelope(handle)}}

    async def read(payload, signal):
        assert module.events == []
        total = 0
        for raw in payload["input_snapshot"]["handles"]:
            total += (await resources.get(Handle(rid=raw["$handle"], kind=raw["kind"]))).value
        return {"output": {"total": total}}

    async def observe(payload, signal):
        assert sorted(module.events) == [("dispose", 1), ("dispose", 2), ("dispose", 3)]
        return {"output": {}}

    document = graph(
        {
            "make": action(
                "allocate",
                ["item"],
                {"handles": h},
                for_each={"source": "items", "item": "item", "concurrency": 3},
            ),
            "read": action("read", ["handles"], {"total": INTEGER}),
            "clear": assign(literal([]), "handles"),
            "observe": action("observe"),
        },
        ["start -> make", "make -> read", "read -> clear", "clear -> observe", "observe -> end"],
        inputs={"items": array()},
        outputs={"total": INTEGER},
        variables={"handles": array(h)},
    )
    async with execution({"allocate": allocate, "read": read, "observe": observe}) as run:
        assert (await run.execute(document, {"items": [1, 2, 3]}))["output"] == {"total": 6}


async def test_failed_fork_attempt_reclaims_completed_results_before_retry(
    execution, resource_package
):
    module = resource_package
    h = handle_type(module)
    allocated = asyncio.Event()
    attempts = 0

    async def allocate(payload, signal):
        nonlocal attempts
        if attempts:
            assert module.events == [("dispose", attempts)]
        attempts += 1
        handle = await resources.put(module.Tx(attempts))
        allocated.set()
        return {"output": {"tx": envelope(handle)}}

    async def fail(payload, signal):
        await allocated.wait()
        allocated.clear()
        await asyncio.sleep(0.03)
        raise RuntimeError("sibling failed")

    inner = graph(
        {"allocate": action("allocate", outputs={"tx": h}), "failer": action("fail")},
        ["start -> [allocate, failer] -> end"],
    )
    document = graph(
        {
            "block": inline(
                inner, retry={"max_attempts": 2, "on": ["execution"]}, catch={"done": ["execution"]}
            )
        },
        ["start -> block", "block -> end", "block.done -> end"],
    )
    async with execution({"allocate": allocate, "fail": fail}) as run:
        assert (await run.execute(document))["output"] == {}
        assert module.events == [("dispose", 1), ("dispose", 2)]


@pytest.mark.parametrize("outcome", ["failure", "cancel"])
async def test_borrowed_child_residencies_return_from_real_failure_and_cancellation(
    execution, resource_package, outcome
):
    module = resource_package
    h = {**handle_type(module, "Conn"), "type": ["handle", "null"]}
    entered = asyncio.Event()
    observed = {}

    async def allocate(payload, signal):
        handle = await resources.put(module.Conn(61))
        return {"output": {"tx": envelope(handle)}}

    async def borrow(payload, signal):
        raw = payload["input_snapshot"]["tx"]
        assert (await resources.get(Handle(rid=raw["$handle"], kind=raw["kind"]))).value == 61
        observed.update(
            rid=raw["$handle"],
            workflow_id=activity.info().workflow_id,
            run_id=activity.info().workflow_run_id,
        )
        entered.set()
        if outcome == "failure":
            raise RuntimeError("borrowed access failed")
        await asyncio.Future()

    child = {"input": {"tx": h}, "output": {}, "run": {"executor": "remote", "content": "borrow"}}
    document = graph(
        {
            "make": action("make", outputs={"tx": h}),
            "borrow": call("child", {"tx": "tx"}, catch={"failed": ["execution"]}),
            "drop": assign(literal(None), "tx"),
        },
        [
            "start -> make",
            "make -> borrow",
            "borrow -> drop",
            "borrow.failed -> drop",
            "drop -> end",
        ],
    )
    async with execution({"make": allocate}) as run:
        queue = run.queue + "-borrower"
        async with create_worker(
            run.server.client,
            queue,
            {"remote": borrow},
            max_heartbeat_throttle_interval=timedelta(milliseconds=20),
        ) as borrower:
            handle = await run.start(
                document,
                packages={"child": {"format": "json", "text": dumps(child)}},
                options={"child_workflow_history_events": 1, "task_queues": {"remote": queue}},
            )
            await asyncio.wait_for(entered.wait(), 5)
            if outcome == "cancel":
                assert observed["rid"] in borrower.store.entries
                await handle.cancel()
                with pytest.raises(WorkflowFailureError) as failure:
                    await asyncio.wait_for(handle.result(), 10)
                assert isinstance(failure.value.cause, exceptions.CancelledError)
            else:
                assert (await run.finish(handle))["output"] == {}
            # The owner must reclaim the restored copy through the real handoff,
            # while this Worker is still serving, rather than at Worker shutdown.
            assert borrower.store.entries == {}
            assert module.events == [("restore", 61), ("dispose", 61), ("dispose", 61)]
            child_handle = run.server.client.get_workflow_handle(
                observed["workflow_id"], run_id=observed["run_id"]
            )
            history = await child_handle.fetch_history()
            if outcome == "failure":
                failed = next(
                    e.activity_task_failed_event_attributes
                    for e in history.events
                    if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED
                )
                [raw_details] = await run.server.client.data_converter.decode(
                    failed.failure.application_failure_info.details.payloads
                )
                metadata = loads(raw_details)["resources"]
            else:
                updates = [
                    e.workflow_execution_signaled_event_attributes
                    for e in history.events
                    if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
                    and e.workflow_execution_signaled_event_attributes.signal_name
                    == "resource_update"
                ]
                assert len(updates) == 1
                [raw_update] = await run.server.client.data_converter.decode(
                    updates[0].input.payloads
                )
                metadata = loads(raw_update)["resources"]
            assert metadata["worker_queue"] == borrower.store.worker_queue
            assert observed["rid"] in metadata["resident_rids"]


@pytest.mark.parametrize(
    "cls,consume", [("Tx", True), ("Conn", True), ("File", True), ("Conn", False), ("File", False)]
)
async def test_worker_replacement_routes_restores_and_cleans_by_resource_category(
    execution, resource_package, cls, consume
):
    module = resource_package
    h = {**handle_type(module, cls), "type": ["handle", "null"]}
    calls = []

    async def remote(payload, signal):
        calls.append(payload["run_spec"]["content"])
        if payload["run_spec"]["content"] == "allocate":
            handle = await resources.put(getattr(module, cls)(41))
            return {"output": {"resource": envelope(handle)}}
        raw = payload["input_snapshot"]["resource"]
        instance = await resources.get(Handle(rid=raw["$handle"], kind=raw["kind"]))
        return {"output": {"value": instance.value}}

    nodes = {
        "allocate": {
            "run": {"executor": "remote", "content": "allocate"},
            "output": {"resource": h},
        },
        "wait": {"run": {"executor": "signal"}},
        "drop": assign(literal(None), "resource"),
    }
    edges = ["start -> allocate", "allocate -> wait"]
    if consume:
        nodes["read"] = {
            "run": {"executor": "remote", "content": "read"},
            "input": ["resource"],
            "output": {"value": INTEGER},
            "catch": {"lost": ["execution"]},
        }
        edges += ["wait -> read", "read -> drop", "read.lost -> drop"]
    else:
        edges += ["wait -> drop"]
    edges += ["drop -> end"]
    document = graph(
        nodes,
        edges,
        outputs={"value": INTEGER} if consume else {},
        variables={"value": {**INTEGER, "default": -1}} if consume else {},
    )
    async with execution() as run:
        queue = run.queue + "-resources"
        async with create_worker(run.server.client, queue, {"remote": remote}):
            handle = await run.start(document, options={"task_queues": {"remote": queue}})
            for _ in range(500):
                snapshot = await handle.query("get_runtime_snapshot")
                if snapshot["frames"] and snapshot["frames"][0]["waiting"]:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Workflow did not reach signal barrier")
        # The first Worker has drained. Its persistent cache must not delete the
        # external object; nonpersistent caches have released their own instance.
        assert module.events == ([] if cls == "File" else [("dispose", 41)])
        async with create_worker(run.server.client, queue, {"remote": remote}):
            await handle.execute_update(
                "deliver_signal", dumps({"address": "main/wait", "output": {}})
            )
            result = await run.finish(handle)
        if consume:
            assert result["output"] == {"value": -1 if cls == "Tx" else 41}
        if cls == "Tx":
            assert calls == ["allocate"]
        elif cls == "Conn" and not consume:
            assert module.events == [("dispose", 41)]
        else:
            assert ("restore", 41) in module.events
            assert module.events[-1] == ("dispose", 41)


async def test_forged_child_depth_cannot_bypass_actual_root_interface(server, resource_package):
    h = {**handle_type(resource_package), "type": ["handle", "null"]}
    doc = {
        "input": {"tx": h},
        "output": {},
        "run": {"executor": "builtin", "content": "return Output()"},
    }
    bundle = compile_bundle("main", {"main": {"format": "json", "text": dumps(doc)}})
    queue = "root-boundary-" + uuid4().hex
    async with create_worker(server.client, queue):
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": bundle,
                "input_json": dumps({"tx": None}),
                "_call_depth": 20,
                "resource_context": {"parent_run_id": "invented", "resources": []},
            },
            id=str(uuid4()),
            task_queue=queue,
        )
        from temporalio.client import WorkflowFailureError

        with pytest.raises(WorkflowFailureError) as failure:
            await handle.result()
        assert "pure values" in str(failure.value.cause)


@pytest.mark.parametrize("valid_source", [False, True])
async def test_duplicate_and_late_residencies_do_not_repeat_external_deletion(
    execution, resource_package, valid_source
):
    module = resource_package
    h = {**handle_type(module, "File"), "type": ["handle", "null"]}
    observed = {}

    async def allocate(payload, signal):
        handle = await resources.put(module.File(53))
        return {"output": {"file": envelope(handle)}}

    async def read(payload, signal):
        observed.update(
            {
                "call_id": payload["resource_context"]["call_id"],
                "source_run_id": activity.info().workflow_run_id,
                "descriptor": payload["resource_context"]["resources"][0],
            }
        )
        return {"output": {}}

    document = graph(
        {
            "make": action("make", outputs={"file": h}),
            "read": action("read", ["file"]),
            "before": {"run": {"executor": "signal"}},
            "drop": assign(literal(None), "file"),
            "after": {"run": {"executor": "signal"}},
        },
        [
            "start -> make",
            "make -> read",
            "read -> before",
            "before -> drop",
            "drop -> after",
            "after -> end",
        ],
    )

    async def waiting(handle, address):
        for _ in range(500):
            snapshot = await handle.query("get_runtime_snapshot")
            if snapshot["frames"] and address in snapshot["frames"][0]["waiting"]:
                return
            await asyncio.sleep(0.01)
        pytest.fail("Workflow did not reach expected signal barrier")

    async with execution({"make": allocate, "read": read}) as run:
        handle = await run.start(document)
        await waiting(handle, "main/before")
        d = observed["descriptor"]
        async with create_worker(run.server.client, run.queue + "-late") as late_worker:
            # Model a late-restored local copy still covered by the original
            # input authorization. It remains unknown to the owner until update.
            context = ResourceContext(late_worker.store, [d])
            await context.get(Handle(rid=d["rid"], kind=d["kind"]))
            await context.close(set())
            await handle.execute_update(
                "deliver_signal", dumps({"address": "main/before", "output": {}})
            )
            await waiting(handle, "main/after")
            assert module.events == [("restore", 53), ("dispose", 53)]
            original = {
                "created": [],
                "worker_queue": d["creator_queue"],
                "resident_rids": [d["rid"]],
            }
            for _ in range(2):
                await handle.signal(
                    "resource_update",
                    dumps(
                        {
                            "call_id": observed["call_id"],
                            "source_run_id": observed["source_run_id"],
                            "resources": original,
                        }
                    ),
                )
            await handle.signal(
                "resource_update",
                dumps(
                    {
                        "call_id": observed["call_id"],
                        "source_run_id": observed["source_run_id"] if valid_source else "wrong-run",
                        "resources": {**original, "worker_queue": late_worker.store.worker_queue},
                    }
                ),
            )
            await handle.execute_update(
                "deliver_signal", dumps({"address": "main/after", "output": {}})
            )
            assert (await run.finish(handle))["output"] == {}
            # Run completion waits for its Signal handlers and their cleanup.
            # Assert before shutdown, which would independently drop this cache.
            assert (d["rid"] not in late_worker.store.entries) is valid_source
        assert module.events == [("restore", 53), ("dispose", 53)]
