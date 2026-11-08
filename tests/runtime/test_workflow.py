import asyncio
from decimal import Decimal

import pytest
from axcutor_runtime.codec import dumps, loads
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
    mapping,
)
from temporalio import activity


async def test_root_run_exact_values_branch_contract_and_minimal_input(execution):
    seen = []

    async def work(request, signal):
        seen.append(request)
        values = request["input_snapshot"]["value"]
        assert not signal.aborted
        return {"branch": "ok", "output": {"result": values, "unused": False}}

    record = {"type": "object", "properties": {"n": INTEGER, "decimal": N, "map": mapping()}}
    inputs = {
        "n": 99999999999999999999999999999,
        "decimal": Decimal("0.12345678901234567890123456789"),
        "map": {"2": 2, "0": 0},
    }
    document = {
        "input": {"value": record},
        "output": {"result": record},
        "branches": {"ok": "done"},
        "run": {"executor": "builtin", "content": "work"},
    }
    async with execution({"work": work}) as run:
        result = await run.execute(document, {"value": inputs})
        assert result == {"branch": "ok", "output": {"result": inputs}}
    assert list(seen[0]["input_snapshot"]) == ["value"]
    assert seen[0]["attempt_timeout_ms"] == 604800000


async def test_graph_call_projection_defaults_and_static_numeric_types(execution):
    callee = graph(
        {
            "add": builtin(
                "math", {"op": literal("add"), "left": "x", "right": 1}, {"result": "sum"}
            ),
            "keep": assign("x", "unused"),
        },
        ["start -> add", "add -> keep", "keep -> end"],
        inputs={"x": N},
        outputs={"sum": N, "unused": N},
    )
    document = graph(
        {"call": call("child", {"x": "n"}, {"sum": "answer"})},
        ["start -> call", "call -> end"],
        inputs={"n": N},
        outputs={"answer": N},
        variables={"private": {**INTEGER, "default": 42}},
    )
    async with execution({}) as run:
        result = await run.execute(
            document, {"n": 2}, packages={"child": {"format": "json", "text": dumps(callee)}}
        )
        assert dumps(result) == '{"branch":null,"output":{"answer":3.0}}'


@pytest.mark.parametrize(
    "retry",
    [
        None,
        {"max_attempts": 3, "backoff_ms": 0, "on": ["execution"]},
        {"max_attempts": 3, "backoff_ms": 10, "on": ["execution", "timeout"]},
    ],
)
async def test_retry_snapshot_rollback_catch_and_step_accounting(execution, retry):
    seen = []

    async def unstable(request, signal):
        seen.append((list(request["input_snapshot"]["items"]), activity.info().attempt))
        request["input_snapshot"]["items"].append(99)
        if len(seen) < 3:
            raise RuntimeError("business failed")
        return {"output": {"value": 7}}

    modifiers = {"catch": {"failed": ["execution"]}}
    if retry:
        modifiers["retry"] = retry
    document = graph(
        {"work": action("unstable", ["items"], {"value": INTEGER}, **modifiers)},
        ["start -> work", "work -> end", "work.failed -> end"],
        inputs={"items": array()},
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 5}},
        max_steps=1,
    )
    async with execution({"unstable": unstable}) as run:
        result = await run.execute(document, {"items": [1, 2]})
        assert result["output"]["value"] == (7 if retry else 5)
    assert [values for values, _ in seen] == [[1, 2]] * (3 if retry else 1)
    assert [attempt for _, attempt in seen] == (
        [1, 2, 3] if retry and retry["backoff_ms"] else [1] * len(seen)
    )


async def test_contract_retry_and_filtering_run_validation(execution):
    seen = []

    async def work(request, signal):
        seen.append(1)
        # Mutating the request must not weaken the wrapper's own contract.
        request["output_schema"] = {}
        return {"output": {"n": True if len(seen) == 1 else 4, "extra": "ignored"}}

    document = {
        "input": {},
        "output": {"n": INTEGER},
        "run": {"executor": "builtin", "content": "work"},
        "retry": {"max_attempts": 2, "backoff_ms": 10, "on": ["timeout", "contract"]},
    }
    async with execution({"work": work}) as run:
        assert await run.execute(document) == {"branch": None, "output": {"n": 4}}
    assert len(seen) == 2


async def test_subgraph_fail_rolls_back_and_preserves_parent_values(execution):
    sub = graph(
        {"dirty": assign(99, "value"), "branch": builtin("switch", {"source": literal(True)})},
        ["start -> dirty", "dirty -> branch", "branch.true -> fail", "branch.false -> end"],
        outputs={"value": INTEGER},
    )
    document = graph(
        {"sub": inline(sub, catch={"failed": ["execution"]})},
        ["start -> sub", "sub -> end", "sub.failed -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 3}},
    )
    async with execution({}) as run:
        assert (await run.execute(document))["output"] == {"value": 3}


async def test_root_maxsteps_and_depth_are_terminal_subgraph_steps_are_catchable(execution):
    sub = graph(
        {"first": assign(1, "v"), "next": assign(2, "v")},
        ["start -> first", "first -> next", "next -> end"],
        outputs={"v": INTEGER},
        max_steps=1,
    )
    document = graph(
        {
            "sub": inline(
                sub,
                retry={"max_attempts": 2, "backoff_ms": 0, "on": ["max_steps"]},
                catch={"limited": ["max_steps"]},
            )
        },
        ["start -> sub", "sub -> end", "sub.limited -> end"],
        outputs={"v": INTEGER},
        variables={"v": {**INTEGER, "default": 8}},
    )
    async with execution({}) as run:
        assert (await run.execute(document))["output"] == {"v": 8}
        assert (await run.failed(sub)).type == "MaxStepsExceededError"
        assert (
            await run.failed(document, options={"max_call_depth": 1})
        ).type == "StackOverflowError"


async def test_kill_removes_stale_item_but_keeps_shift_rest(execution):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait(request, signal):
        entered.set()
        await release.wait()
        return {"output": {}}

    document = graph(
        {
            "shift": builtin("array_shift", {"source": "items"}, {"item": "item", "rest": "rest"}),
            "wait": action("wait"),
        },
        ["start -> shift", "shift.some -> end", "shift.empty -> wait", "wait -> end"],
        inputs={"items": array()},
        outputs={"rest": array()},
        variables={"item": {**INTEGER, "default": 99}, "rest": {**array(), "default": [5]}},
    )
    async with execution({"wait": wait}) as run:
        handle = await run.start(document, {"items": []})
        await asyncio.wait_for(entered.wait(), 10)
        snapshot = await handle.query("get_runtime_snapshot")
        assert loads(snapshot["frames"][0]["env_json"]) == {"items": [], "rest": [5]}
        assert snapshot["frames"][0]["steps"] == 2
        release.set()
        assert (await run.finish(handle))["output"] == {"rest": [5]}


async def test_fork_reads_same_snapshot_commits_in_declaration_order(execution):
    seen = []

    async def left(request, signal):
        request["input_snapshot"]["items"].append(9)
        await asyncio.sleep(0.03)
        return {"output": {"left": 11}}

    async def right(request, signal):
        seen.append(request["input_snapshot"])
        return {"output": {"right": 22}}

    document = graph(
        {
            "left": action("left", ["items"], {"left": INTEGER}),
            "right": action("right", ["items"], {"right": INTEGER}),
        },
        ["start -> [left, right] -> end"],
        inputs={"items": array()},
        outputs={"left": INTEGER, "right": INTEGER},
        max_steps=2,
    )
    async with execution({"left": left, "right": right}) as run:
        assert (await run.execute(document, {"items": [1]}))["output"] == {"left": 11, "right": 22}
    assert seen == [{"items": [1]}]


async def test_builtin_cycle_counts_every_activation_including_empty_exit(execution):
    document = graph(
        {
            "shift": builtin("array_shift", {"source": "queue"}, {"item": "item", "rest": "queue"}),
            "add": builtin(
                "math", {"op": literal("add"), "left": "sum", "right": "item"}, {"result": "sum"}
            ),
        },
        ["start -> shift", "shift.some -> add", "add -> shift", "shift.empty -> end"],
        outputs={"sum": INTEGER, "queue": array()},
        variables={
            "queue": {**array(), "default": [1, 2, 3]},
            "sum": {**INTEGER, "default": 0},
            "item": INTEGER,
        },
        max_steps=7,
    )
    async with execution({}) as run:
        assert (await run.execute(document))["output"] == {"sum": 6, "queue": []}
        document["graph"]["max_steps"] = 6
        assert (await run.failed(document)).type == "MaxStepsExceededError"
