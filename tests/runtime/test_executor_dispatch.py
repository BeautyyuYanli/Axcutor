"""Extension RunSpec crosses the real compiler, Temporal routing and Activity ABI."""

from decimal import Decimal

import pytest
from axcutor_runtime.worker import create_worker
from helpers import INTEGER, graph


@pytest.mark.parametrize("executor", ["remote", "class", "start"])
@pytest.mark.parametrize("root", [True, False])
async def test_extension_configuration_and_executor_routing(execution, executor, root):
    spec = {
        "executor": executor,
        "url": "http://localhost",
        "options": {
            "2": 123456789012345678901234567890,
            "0": Decimal("1.000000000000000000000000001"),
            "class": [None, True],
        },
    }
    node = {"run": spec, "input": ["n"], "output": {"value": INTEGER}}
    if root:
        document = {**node, "input": {"n": INTEGER}}
    else:
        document = graph(
            {"work": node},
            ["start -> work", "work -> end"],
            inputs={"n": INTEGER},
            outputs={"value": INTEGER},
        )
    calls = []

    async def execute(request, signal):
        assert request["run_spec"] == spec
        assert list(request["run_spec"]["options"]) == ["2", "0", "class"]
        assert request["input_schema"] == {"n": INTEGER}
        assert request["input_snapshot"] == {"n": 7}
        assert not signal.aborted
        calls.append(request)
        return {"branch": None, "output": {"value": request["input_snapshot"]["n"] * 2}}

    async with execution() as run:
        queue = run.queue + "-extension"
        async with create_worker(run.server.client, queue, {executor: execute}):
            result = await run.execute(
                document, {"n": 7}, options={"task_queues": {executor: queue}}
            )
        assert result["output"] == {"value": 14}
        assert len(calls) == 1
