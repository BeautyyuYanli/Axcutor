"""Real files, generated contracts and the native/Temporal module boundary."""

import asyncio
import importlib
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.activities import AbortSignal, RunActivities
from axcutor_runtime.builtin import compute_contract_sha256, synthesize_contract_module
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.contracts import generate_contracts
from axcutor_runtime.module import execute_module, module_executor
from axcutor_runtime.packages import PackageMount, load_packages, snapshot_packages
from axcutor_runtime.worker import create_worker, workflow_runner
from helpers import INTEGER, N, S, array, call, graph
from temporalio import exceptions
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install a unique package on Python's real import path for each test."""
    name = "ax_test_" + uuid4().hex
    root = tmp_path / name
    flow = root / "flow"
    flow.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (flow / "__init__.py").write_text("")
    mount = PackageMount(name, root)
    owner = name + ".flow"
    monkeypatch.syspath_prepend(str(tmp_path))

    def prepare(source, *, inputs=None, outputs=None, branches=None, local=".impl", values=None):
        document = {
            "input": inputs or {},
            "output": outputs or {},
            "branches": branches or {},
            "run": {"executor": "module", "content": local},
        }
        (flow / "__axcutor__.yaml").write_text(dumps(document))
        parts = [] if local == "." else local[1:].split(".")
        implementation = (
            flow.joinpath(*parts[:-1], parts[-1] + ".py") if parts else flow / "__init__.py"
        )
        implementation.parent.mkdir(parents=True, exist_ok=True)
        current = implementation.parent
        while current != flow:
            (current / "__init__.py").touch()
            current = current.parent
        implementation.write_text(source)
        entry, packages = load_packages(flow, [mount])
        generate_contracts(entry, packages, [mount])
        bundle_json = compile_bundle(entry, packages)
        normalized = loads(bundle_json)["workflows"][entry]
        payload = {
            "workflow_id": owner,
            "run_spec": normalized["body"]["run"],
            "input_schema": normalized["input"],
            "output_schema": normalized["output"],
            "branches_contract": normalized["branches"],
            "input_snapshot": values or {},
            "attempt_timeout_ms": 1000,
            "heartbeat_interval_ms": 10,
        }
        return payload, bundle_json, packages

    yield name, owner, root, flow, mount, prepare
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    importlib.invalidate_caches()


async def invoke(payload, activities=None):
    return loads(
        await ActivityEnvironment().run((activities or RunActivities()).execute_run, dumps(payload))
    )


@pytest.mark.parametrize("local", [".", ".__init__", ".impl", ".sub.impl"])
async def test_real_module_contracts_and_relative_package_resolution(installed, local):
    _, _, _, _, _, prepare = installed
    contract = "_contract" if local == "." else local.rsplit(".", 1)[-1] + "_contract"
    payload, _, _ = prepare(
        f"from .{contract} import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    return Output(value=input.value * 2)\n",
        local=local,
        inputs={"value": INTEGER},
        outputs={"value": INTEGER},
        values={"value": 7},
    )
    assert await invoke(payload) == {"branch": None, "output": {"value": 14}}


async def test_input_is_frozen_deep_copied_and_numeric_transport_is_converted(installed):
    *_, prepare = installed
    record = {"type": "object", "properties": {"amount": N, "note": S}, "required": ["amount"]}
    values = {"record": {"amount": Decimal("1.25")}, "items": [1]}
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    assert type(input.record.amount) is float\n"
        "    assert input.record.note is None\n"
        "    try:\n"
        "        input.record.amount = 2.0\n"
        "    except ValueError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('input model was mutable')\n"
        "    input.items.append(99)\n"
        "    return Output(value=input.record.amount)\n",
        inputs={"record": record, "items": array()},
        outputs={"value": N},
        values=values,
    )
    original = deepcopy(payload)
    assert (await execute_module(payload, AbortSignal()))["output"] == {"value": 1.25}
    assert payload == original and values["items"] == [1]


@pytest.mark.parametrize(
    ("body", "category"),
    [
        ("return {'value': 1}", "contract"),
        ("return Output(value=True)", "contract"),
        ("return Output(value=1.0)", "contract"),
        ("return Output.model_construct(value=True)", "contract"),
        ("raise RuntimeError('business failed')", "execution"),
        (
            "from pydantic import BaseModel\n"
            "    class Business(BaseModel):\n        n: int\n"
            "    Business(n='bad')\n    return Output(value=1)",
            "execution",
        ),
    ],
)
async def test_strict_results_and_business_validation_classification(installed, body, category):
    *_, prepare = installed
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        f"async def execute(input: Input) -> ReturnType:\n    {body}\n",
        outputs={"value": INTEGER},
    )
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == category
    if category == "execution":
        assert any(
            Path(frame.tb_frame.f_code.co_filename).name == "impl.py"
            for frame in _traceback(failure.value)
        )


def _traceback(error):
    while error is not None:
        frame = error.__traceback__
        while frame is not None:
            yield frame
            frame = frame.tb_next
        error = error.__cause__


@pytest.mark.parametrize(
    "body", ["return Branch.skip()", "return Branch.ok(value=7)", "return Branch.__module__()"]
)
async def test_real_branch_variants_and_subsets(installed, body):
    *_, prepare = installed
    payload, _, _ = prepare(
        "from .impl_contract import Branch, Input, ReturnType\n"
        f"async def execute(input: Input) -> ReturnType:\n    {body}\n",
        outputs={"value": INTEGER},
        branches={
            "ok": "has value",
            "skip": {"description": "none", "output": []},
            "__module__": {"description": "legal namespace collision", "output": []},
        },
    )
    result = await invoke(payload)
    assert result == (
        {"branch": "__module__" if "__module__" in body else "skip", "output": {}}
        if "ok" not in body
        else {"branch": "ok", "output": {"value": 7}}
    )


async def test_schema_drift_is_rejected_before_business_function(installed):
    *_, prepare = installed
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    raise AssertionError('must not run')\n",
    )
    payload["output_schema"] = {"changed": S}
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "contract"
    assert "contract SHA mismatch" in failure.value.message


@pytest.mark.parametrize("wrong", ["input", "output", "missing", "sync"])
@pytest.mark.parametrize("branched", [False, True])
async def test_contract_types_must_be_from_one_template(installed, wrong, branched):
    name, _, _, flow, _, prepare = installed
    output_name = "Branch" if branched else "Output"
    called = flow.parent / "called"
    canonical = f"from .impl_contract import Input, {output_name} as ResultOutput\n"
    imports = {
        "input": (
            f"from .other_contract import Input, {output_name} as ResultOutput\n"
            f"from .impl_contract import {output_name}\n"
        ),
        "output": canonical + f"from .other_contract import {output_name}\n",
        "missing": canonical,
        "sync": canonical + f"from .impl_contract import {output_name}\n",
    }
    declaration = "def" if wrong == "sync" else "async def"
    constructor = "ResultOutput.ok" if branched else "ResultOutput"
    payload, _, _ = prepare(
        imports[wrong] + "from pathlib import Path\n"
        f"{declaration} execute(input: Input):\n"
        f"    Path({str(called)!r}).write_text('business called')\n"
        f"    return {constructor}(value=input.value * 2)\n",
        inputs={"value": INTEGER},
        outputs={"value": INTEGER},
        values={"value": 7},
        branches={"ok": "success"} if branched else {},
    )
    (flow / "other_contract.py").write_text(
        synthesize_contract_module(
            payload["input_schema"],
            payload["output_schema"],
            payload["branches_contract"],
        )
    )
    contract = importlib.import_module(name + ".flow.impl_contract")
    other = importlib.import_module(name + ".flow.other_contract")
    assert contract.__axcutor_contract_sha256__ == other.__axcutor_contract_sha256__
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "contract"
    expected = (
        "requires async execute"
        if wrong == "sync"
        else f"requires Input and {output_name} from the same generated contract module"
    )
    assert expected in failure.value.message
    assert not called.exists()


async def test_import_errors_are_execution_failures(installed):
    *_, prepare = installed
    payload, _, _ = prepare("raise ImportError('host dependency is missing')\n")
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "execution"


async def test_ambiguous_python_implementation_is_rejected_before_import(installed):
    name, _, _, flow, _, prepare = installed
    imported = flow.parent / "imported"
    body = (
        "from pathlib import Path\n"
        f"Path({str(imported)!r}).write_text('implementation imported')\n"
        "async def execute(input: Input) -> ReturnType:\n    return Output(value=7)\n"
    )
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, ReturnType\n" + body,
        outputs={"value": INTEGER},
    )
    conflicting = flow / "impl"
    conflicting.mkdir()
    (conflicting / "__init__.py").write_text(
        "from ..impl_contract import Input, Output, ReturnType\n" + body
    )
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "execution"
    assert "ambiguous Python package and module" in failure.value.message
    assert not imported.exists()
    assert name + ".flow.impl" not in sys.modules


async def test_nested_output_contracts_and_optional_key_omission(installed):
    *_, prepare = installed
    record = {"type": "object", "properties": {"value": INTEGER, "note": S}, "required": ["value"]}
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, Output_record, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    return Output(record=Output_record(value=7))\n",
        outputs={"record": record},
    )
    assert await invoke(payload) == {"branch": None, "output": {"record": {"value": 7}}}


async def test_module_attempt_deadline_waits_for_cleanup(installed):
    name, _, _, _, _, prepare = installed
    payload, _, _ = prepare(
        "import asyncio\nfrom .impl_contract import Input, Output, ReturnType\n"
        "drained = asyncio.Event()\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    try:\n        await asyncio.Event().wait()\n"
        "    except asyncio.CancelledError:\n"
        "        await asyncio.sleep(0.01)\n        return Output()\n"
        "    finally:\n        drained.set()\n",
    )
    payload["attempt_timeout_ms"] = 30
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "timeout"
    assert importlib.import_module(name + ".flow.impl").drained.is_set()


async def test_abort_cancels_and_drains_even_if_business_swallows_cancellation(installed):
    name, _, _, _, _, prepare = installed
    payload, _, _ = prepare(
        "import asyncio\nfrom .impl_contract import Input, Output, ReturnType\n"
        "started = asyncio.Event()\ndrained = asyncio.Event()\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    started.set()\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    except asyncio.CancelledError:\n"
        "        await asyncio.sleep(0)\n"
        "        return Output()\n"
        "    finally:\n"
        "        drained.set()\n",
    )
    module = importlib.import_module(name + ".flow.impl")
    signal = AbortSignal()
    task = asyncio.create_task(execute_module(payload, signal))
    await asyncio.wait_for(module.started.wait(), 1)
    signal._abort("cancelled")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert module.drained.is_set()


async def test_worker_mapping_is_fixed_and_submission_cannot_change_it(installed):
    name, _, _, _, _, prepare = installed
    payload, _, _ = prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n    return Output()\n",
    )
    mapping = {"logical": name}
    execute = module_executor(mapping)
    mapping["logical"] = "wrong"
    payload["workflow_id"] = "logical.flow"
    assert await execute(payload, AbortSignal()) == {"branch": None, "output": {}}
    with pytest.raises(LookupError):
        await module_executor({})(payload, AbortSignal())
    with pytest.raises(ValueError):
        module_executor({"a": name, "b": name})


def test_contract_fingerprint_preserves_all_normalized_order_and_branch_metadata():
    expected = compute_contract_sha256({"a": INTEGER, "b": N}, {}, {})
    assert len(expected) == 64
    assert expected == compute_contract_sha256({"a": INTEGER, "b": N}, {}, {})
    assert expected != compute_contract_sha256({"b": N, "a": INTEGER}, {}, {})
    assert compute_contract_sha256(
        {}, {}, {"ok": {"description": "a", "output": []}}
    ) != compute_contract_sha256({}, {}, {"ok": {"description": "b", "output": []}})


async def test_module_in_anonymous_entry_calls_named_package_and_replays(server, installed):
    name, owner, _, _, _, prepare = installed
    payload, _, packages = prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    return Output(value=input.value * 2)\n",
        inputs={"value": INTEGER},
        outputs={"value": INTEGER},
        values={"value": 7},
    )
    logical_root = "logical_" + name
    logical_owner = logical_root + ".flow"
    packages[logical_owner] = packages.pop(owner)
    entry = graph(
        {"call": call(logical_owner, {"value": "value"}, {"value": "value"})},
        ["start -> call", "call -> end"],
        inputs={"value": INTEGER},
        outputs={"value": INTEGER},
    )
    packages["__main__"] = {"format": "json", "text": dumps(entry)}
    bundle_json = compile_bundle("__main__", packages)
    queue = str(uuid4())
    async with create_worker(server.client, queue, module_mounts={logical_root: name}):
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {"bundle_json": bundle_json, "input_json": dumps(payload["input_snapshot"])},
            id=str(uuid4()),
            task_queue=queue,
        )
        assert loads(await asyncio.wait_for(handle.result(), 15)) == {
            "branch": None,
            "output": {"value": 14},
        }
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(await handle.fetch_history())


def test_mount_conflicts_and_generation_never_import_python(installed):
    name, owner, root, flow, mount, prepare = installed
    _, _, packages = prepare("raise AssertionError('generation must never import')\n")
    assert name not in sys.modules
    assert generate_contracts(owner, packages, [mount], check=True)
    assert name not in sys.modules
    for mounts in [
        [mount, mount],
        [mount, PackageMount("other", root)],
        [PackageMount("_private", root)],
    ]:
        with pytest.raises(ValueError):
            snapshot_packages(mounts)
    (root / "flow.py").write_text("")
    with pytest.raises(ValueError, match="conflict"):
        snapshot_packages([mount])
    assert flow.is_dir()


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("conflict", ["template", "alias", "package", "implementation"])
def test_contract_generation_plans_conflicts_before_writing(installed, check, conflict):
    _, owner, _, flow, mount, prepare = installed
    prepare(
        "raise AssertionError('must not import')\n",
        local=".ready" if conflict == "package" else ".impl",
    )
    document = graph(
        {
            "fresh": {"run": {"executor": "module", "content": ".fresh"}},
            "a": {"run": {"executor": "module", "content": ".impl"}},
            "b": {"run": {"executor": "module", "content": ".impl"}, "output": {"value": INTEGER}},
        },
        ["start -> fresh", "fresh -> a", "a -> b", "b -> end"],
    )
    expected = "conflicting module contracts"
    if conflict == "alias":
        document["graph"]["nodes"]["a"]["run"]["content"] = "."
        document["graph"]["nodes"]["b"]["run"]["content"] = ".__init__"
    elif conflict == "package":
        package = flow / "impl_contract"
        package.mkdir()
        (package / "__init__.py").write_text("# Existing Python package.\n")
        (package / "__axcutor__.yaml").write_text(
            "input: {}\noutput: {}\nrun: {executor: builtin, content: return Output()}\n"
        )
        expected = "contract target conflicts with a Python package"
    elif conflict == "implementation":
        document["graph"]["nodes"]["b"]["run"]["content"] = ".impl_contract"
        expected = "conflicts with a module implementation"
    packages = {owner: {"format": "json", "text": dumps(document)}}
    before = {
        path.relative_to(flow): path.read_bytes() for path in flow.rglob("*") if path.is_file()
    }
    with pytest.raises(ValueError, match=expected):
        generate_contracts(owner, packages, [mount], check=check)
    assert {
        path.relative_to(flow): path.read_bytes() for path in flow.rglob("*") if path.is_file()
    } == before
    snapshot_packages([mount])


def test_contract_check_reports_missing_without_writing(installed):
    _, owner, _, flow, mount, prepare = installed
    _, _, packages = prepare("raise AssertionError('must not import')\n", local=".new")
    # Generate a different destination from the same valid source snapshot.
    document = loads(packages[owner]["text"])
    document["run"]["content"] = ".missing"
    packages[owner]["text"] = dumps(document)
    with pytest.raises(ValueError, match="missing or stale"):
        generate_contracts(owner, packages, [mount], check=True)
    assert not (flow / "missing_contract.py").exists()


def test_consistent_contracts_for_initializer_aliases_are_allowed(installed):
    _, owner, _, flow, mount, prepare = installed
    prepare("raise AssertionError('must not import')\n")
    document = graph(
        {
            "a": {"run": {"executor": "module", "content": "."}},
            "b": {"run": {"executor": "module", "content": ".__init__"}},
        },
        ["start -> a", "a -> b", "b -> end"],
    )
    packages = {owner: {"format": "json", "text": dumps(document)}}
    implementation = (flow / "__init__.py").read_bytes()
    paths = generate_contracts(owner, packages, [mount])
    assert set(paths) == {flow / "_contract.py", flow / "__init___contract.py"}
    assert (flow / "_contract.py").read_text() == (flow / "__init___contract.py").read_text()
    assert generate_contracts(owner, packages, [mount], check=True) == paths
    assert (flow / "__init__.py").read_bytes() == implementation


async def test_inline_iteration_keeps_owner_and_normalized_contract(server, installed):
    name, owner, _, flow, mount, prepare = installed
    prepare(
        "from .impl_contract import Input, Output, ReturnType\n"
        "async def execute(input: Input) -> ReturnType:\n"
        "    return Output(results=input.item * 2)\n"
    )
    each = {
        "input": ["item"],
        "output": {"results": INTEGER},
        "run": {"executor": "module", "content": ".impl"},
        "for_each": {"source": "items", "item": "item", "concurrency": 2},
    }
    scope = {
        "input": ["items"],
        "output": {"results": array()},
        "graph": {"nodes": {"each": each}, "edges": ["start -> each", "each -> end"]},
    }
    document = graph(
        {"scope": scope},
        ["start -> scope", "scope -> end"],
        inputs={"items": array()},
        outputs={"results": array()},
    )
    (flow / "__axcutor__.yaml").write_text(dumps(document))
    entry, packages = load_packages(flow, [mount])
    generate_contracts(entry, packages, [mount])
    queue = str(uuid4())
    async with create_worker(server.client, queue, module_mounts={name: name}):
        result = await server.client.execute_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(owner, packages),
                "input_json": dumps({"items": [1, 2, 3]}),
            },
            id=str(uuid4()),
            task_queue=queue,
        )
    assert loads(result) == {"branch": None, "output": {"results": [2, 4, 6]}}
