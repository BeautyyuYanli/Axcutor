"""Controlled roots compile, generate contracts, execute resources and replay."""

import asyncio
import importlib
import sys
from uuid import uuid4

from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.contracts import generate_contracts
from axcutor_runtime.packages import PackageMount, snapshot_mounts
from axcutor_runtime.worker import create_worker, workflow_runner
from temporalio.worker import Replayer


async def test_restricted_caller_uses_library_resources_without_runtime_headers(
    server, tmp_path, monkeypatch
):
    physical = "ax_library_" + uuid4().hex
    library = tmp_path / physical
    library.mkdir()
    (library / "__init__.py").write_text("")
    pool = library / "pool"
    pool.mkdir()
    (pool / "__init__.py").write_text("from ._tx import Tx\n")
    (pool / "_tx.py").write_text(
        "from axcutor_runtime import resource\n"
        "disposed = []\n"
        "@resource\n"
        "class Tx:\n"
        "    value = 7\n"
        "    async def dispose(self):\n"
        "        disposed.append(self.value)\n"
    )
    header = pool / "__ax_resources__.yaml"
    header.write_text("resources: {Tx: null}\n")
    handle = {"type": "handle", "kind": "std.pool:Tx"}
    integer = {"type": "integer"}
    for name, inputs, outputs, body in [
        (
            "make",
            {},
            {"tx": handle},
            "from ..pool import Tx\n    return Output(tx=await resources.put(Tx()))\n",
        ),
        (
            "read",
            {"tx": handle},
            {"value": integer},
            "return Output(value=(await resources.get(input.tx)).value)\n",
        ),
    ]:
        directory = library / name
        directory.mkdir()
        (directory / "__init__.py").write_text("")
        (directory / "__axcutor__.yaml").write_text(
            dumps(
                {
                    "input": inputs,
                    "output": outputs,
                    "run": {"executor": "module", "content": ".impl"},
                }
            )
        )
        (directory / "impl.py").write_text(
            "from axcutor_runtime import resources\n"
            "from .impl_contract import Input, Output, ReturnType\n"
            "async def execute(input: Input) -> ReturnType:\n    " + body
        )
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("raise AssertionError('graph must not import Python')\n")
    (app / "__axcutor__.yaml").write_text(
        dumps(
            {
                "input": {},
                "output": {"value": integer},
                "graph": {
                    "variables": {"tx": handle},
                    "nodes": {
                        "make": {"use": "workflow:std.make", "with": {"output": {"tx": "tx"}}},
                        "read": {
                            "use": "workflow:std.read",
                            "with": {
                                "input": {"tx": "tx"},
                                "output": {"value": "value"},
                            },
                        },
                    },
                    "edges": ["start -> make", "make -> read", "read -> end"],
                },
            }
        )
    )
    mounts = [PackageMount("app", app, ("signal",)), PackageMount("std", library, ("module",))]
    snapshot = snapshot_mounts(mounts)
    mapping = {"std": physical}
    bundle = compile_bundle("app", snapshot)
    assert len(generate_contracts("app", snapshot, mounts, module_mounts=mapping)) == 2
    assert generate_contracts("app", snapshot, mounts, check=True, module_mounts=mapping)
    assert physical not in sys.modules
    header.unlink()
    monkeypatch.syspath_prepend(str(tmp_path))
    queue = str(uuid4())
    try:
        async with create_worker(server.client, queue, module_mounts=mapping):
            execution = await server.client.start_workflow(
                AxcutorInterpreterWorkflow.run,
                {"bundle_json": bundle, "input_json": "{}"},
                id=str(uuid4()),
                task_queue=queue,
            )
            assert loads(await asyncio.wait_for(execution.result(), 20)) == {
                "branch": None,
                "output": {"value": 7},
            }
            assert importlib.import_module(physical + ".pool._tx").disposed == [7]
        await Replayer(
            workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
        ).replay_workflow(await execution.fetch_history())
    finally:
        for key in list(sys.modules):
            if key == physical or key.startswith(physical + "."):
                del sys.modules[key]
        importlib.invalidate_caches()
