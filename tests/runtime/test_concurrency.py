"""Barrier commit, bounded dispatch, rollback and cooperative cancellation."""

import asyncio
from copy import deepcopy

import pytest
from axcutor_runtime.codec import dumps, loads
from helpers import INTEGER, action, array, assign, builtin, call, graph, inline, literal, mapping
from temporalio.client import WorkflowFailureError


@pytest.mark.parametrize("kind", ["run", "graph", "call", "partial_call"])
@pytest.mark.parametrize("ordered_map", [False, True])
async def test_for_each_ordered_slots_skips_and_injected_scopes(execution, kind, ordered_map):
    active = 0
    peak = 0
    seen = []

    async def work(request, signal):
        nonlocal active, peak
        value = request["input_snapshot"]["item"]
        active += 1
        peak = max(active, peak)
        seen.append(request["input_snapshot"])
        try:
            await asyncio.sleep(value / 100)
            if value == 1:
                raise RuntimeError("skip this item")
            return {"output": {"results": value * 2}}
        finally:
            active -= 1

    sample = {"2": 3, "0": 1, "a": 2} if ordered_map else [3, 1, 2]
    source_type = mapping() if ordered_map else array()
    each = {"source": "items", "item": "item", "index": "index", "concurrency": 2}
    captures = ["item", "index"]
    if ordered_map:
        each["key"] = "key"
        captures.append("key")
    packages = {}
    if kind == "run":
        node = action(
            "work", captures, {"results": INTEGER}, for_each=each, catch={"skip": ["execution"]}
        )
        expected = {"2": 6, "a": 4} if ordered_map else [6, 4]
    elif kind == "partial_call":
        # The grammar forbids for_each directly on a builtin. Call a graph with
        # a partial output contract to exercise omitted iteration slots.
        inner = graph(
            {
                "get": builtin(
                    "array_get", {"source": literal([9, 8]), "index": "i"}, {"value": "value"}
                )
            },
            ["start -> get", "get.some -> end.some", "get.none -> end.none"],
            inputs={"i": INTEGER},
            outputs={"value": INTEGER},
            branches={
                "some": {"description": "found", "output": ["value"]},
                "none": {"description": "missing", "output": []},
            },
        )
        packages = {"callee": {"format": "json", "text": dumps(inner)}}
        node = call("callee", {"i": "item"}, {"value": "results"}, for_each=each)
        expected = {"0": 8} if ordered_map else [8]
    else:
        inner = graph(
            {"copy": assign("x", "value")},
            ["start -> copy", "copy -> end"],
            inputs={"x": INTEGER},
            outputs={"value": INTEGER},
        )
        if kind == "call":
            packages = {"callee": {"format": "json", "text": dumps(inner)}}
            node = call("callee", {"x": "item"}, {"value": "results"}, for_each=each)
        else:
            inner = graph(
                {"copy": assign("item", "results")},
                ["start -> copy", "copy -> end"],
                outputs={"results": INTEGER},
            )
            node = inline(inner, for_each=each)
        expected = sample
    document = graph(
        {"map": node},
        ["start -> map", "map -> end"],
        inputs={"items": source_type},
        outputs={"results": source_type},
        variables={"results": source_type},
        max_steps=3,
    )
    async with execution({"work": work}) as run:
        result = await run.execute(document, {"items": sample}, packages=packages)
        assert result["output"]["results"] == expected
        if ordered_map:
            assert list(result["output"]["results"]) == list(expected)
        # With one later node and a one-step budget, an empty traversal must
        # initialize the target and consume exactly zero steps.
        empty = {} if ordered_map else []
        empty_document = deepcopy(document)
        empty_document["graph"]["nodes"]["after"] = assign("results", "results")
        empty_document["graph"]["edges"] = ["start -> map", "map -> after", "after -> end"]
        empty_document["graph"]["max_steps"] = 1
        assert (await run.execute(empty_document, {"items": empty}, packages=packages))["output"][
            "results"
        ] == empty
    assert active == 0
    if kind == "run":
        assert peak == 2
        seen.sort(key=lambda item: item["index"])
        assert [item["index"] for item in seen] == [0, 1, 2]
        if ordered_map:
            assert [item["key"] for item in seen] == ["2", "0", "a"]


@pytest.mark.parametrize("kind", ["fork", "for_each"])
async def test_barrier_keeps_owning_graph_env_until_all_members_complete(execution, kind):
    fast_returned, slow_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work(request, signal):
        item = request["input_snapshot"]["item"]
        if item == 1:
            fast_returned.set()
        else:
            slow_started.set()
            await release.wait()
        return {"output": {"value": item * 11}}

    if kind == "fork":

        def member(item, output):
            return inline(
                graph(
                    {
                        "work": action("work", ["item"], {"value": INTEGER}),
                        "copy": assign("value", output),
                    },
                    ["start -> work", "work -> copy", "copy -> end"],
                    outputs={output: INTEGER},
                    variables={"item": {**INTEGER, "default": item}},
                )
            )

        document = graph(
            {"fast": member(1, "left"), "slow": member(2, "right")},
            ["start -> [fast, slow] -> end"],
            outputs={"left": INTEGER, "right": INTEGER},
            variables={"left": {**INTEGER, "default": 1}, "right": {**INTEGER, "default": 2}},
        )
        before, after = {"left": 1, "right": 2}, {"left": 11, "right": 22}
        pending_address = "main/slow"
    else:
        document = graph(
            {
                "map": action(
                    "work",
                    ["item"],
                    {"value": INTEGER},
                    for_each={"source": "items", "item": "item", "concurrency": 2},
                )
            },
            ["start -> map", "map -> end"],
            outputs={"value": array()},
            variables={
                "items": {**array(), "default": [1, 2]},
                "value": {**array(), "default": [7]},
            },
        )
        before, after = {"value": [7]}, {"value": [11, 22]}
        pending_address = "main/map[1]"
    async with execution({"work": work}) as run:
        handle = await run.start(document)
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(fast_returned.wait(), slow_started.wait())
                # A business return event precedes Server completion. Query until
                # the fast activation is unregistered, proving its result is ready
                # while the slow member remains blocked on the explicit gate.
                while True:
                    root = (await handle.query("get_runtime_snapshot"))["frames"][0]
                    if [child["address"] for child in root["children"]] == [pending_address]:
                        break
            env = loads(root["env_json"])
            assert {name: env[name] for name in before} == before
        finally:
            release.set()
        assert await run.finish(handle) == {"branch": None, "output": after}


async def test_for_each_stops_dispatch_on_first_failure_and_rolls_back(execution):
    seen = []

    async def work(request, signal):
        item = request["input_snapshot"]["item"]
        seen.append(item)
        if item == 2:
            raise RuntimeError("stop")
        return {"output": {"results": item}}

    sub = graph(
        {
            "map": action(
                "work",
                ["item"],
                {"results": INTEGER},
                for_each={"source": "items", "item": "item", "concurrency": 1},
            )
        },
        ["start -> map", "map -> end"],
        outputs={"results": array()},
        variables={"results": array()},
    )
    document = graph(
        {"sub": inline(sub, ["items"], catch={"failed": ["execution"]})},
        ["start -> sub", "sub -> end", "sub.failed -> end"],
        inputs={"items": array()},
        outputs={"results": array()},
        variables={"results": {**array(), "default": [99]}},
    )
    async with execution({"work": work}) as run:
        assert (await run.execute(document, {"items": [1, 2, 3, 4]}))["output"] == {"results": [99]}
    assert seen == [1, 2]


@pytest.mark.parametrize("late_error", [False, True])
async def test_fork_failure_cancels_waits_and_discards_late_results(execution, late_error):
    entered, cleaned = asyncio.Event(), asyncio.Event()
    signals = []

    async def slow(request, signal):
        signals.append(signal)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert signal.aborted
            await asyncio.sleep(0.03)
            cleaned.set()
            if late_error:
                raise RuntimeError("late cleanup error") from None
            return {"output": {"left": 999}}

    async def fail(request, signal):
        await entered.wait()
        raise RuntimeError("primary failure")

    async def check(request, signal):
        assert cleaned.is_set()
        return {"output": {}}

    sub = graph(
        {
            "slow": action("slow", outputs={"left": INTEGER}),
            "broken": action("fail", outputs={"right": INTEGER}),
        },
        ["start -> [slow, broken] -> end"],
        outputs={"left": INTEGER, "right": INTEGER},
    )
    document = graph(
        {"sub": inline(sub, catch={"failed": ["execution"]}), "check": action("check")},
        ["start -> sub", "sub -> end", "sub.failed -> check", "check -> end"],
        outputs={"left": INTEGER, "right": INTEGER},
        variables={"left": {**INTEGER, "default": 1}, "right": {**INTEGER, "default": 2}},
    )
    async with execution({"slow": slow, "fail": fail, "check": check}) as run:
        assert (await run.execute(document))["output"] == {"left": 1, "right": 2}
    assert len(signals) == 1 and signals[0].aborted and cleaned.is_set()


async def test_external_cancel_drains_activity_and_unregisters_frames(execution):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def work(request, signal):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert signal.aborted
            cleaned.set()

    document = graph(
        {"work": action("work", catch={"caught": "*"})},
        ["start -> work", "work -> end", "work.caught -> end"],
    )
    async with execution({"work": work}) as run:
        handle = await run.start(document)
        await asyncio.wait_for(entered.wait(), 10)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), 10)
        assert type(failure.value.cause).__name__ == "CancelledError"
        assert cleaned.is_set()
        assert await handle.query("get_runtime_snapshot") == {"frames": []}


async def test_subgraph_step_limit_cancels_inflight_iterations_before_catch(execution):
    active, cleaned = [], []

    async def work(request, signal):
        item = request["input_snapshot"]["item"]
        active.append(item)
        try:
            if item == 1:
                await asyncio.sleep(0.02)
                return {"output": {"results": 1}}
            await asyncio.Event().wait()
        finally:
            cleaned.append(item)

    sub = graph(
        {
            "map": action(
                "work",
                ["item"],
                {"results": INTEGER},
                for_each={"source": "items", "item": "item", "concurrency": 2},
            )
        },
        ["start -> map", "map -> end"],
        outputs={"results": array()},
        variables={"results": array()},
        max_steps=2,
    )
    document = graph(
        {"sub": inline(sub, ["items"], catch={"limited": ["max_steps"]})},
        ["start -> sub", "sub -> end", "sub.limited -> end"],
        inputs={"items": array()},
        outputs={"results": array()},
        variables={"results": {**array(), "default": [99]}},
    )
    async with execution({"work": work}) as run:
        assert (await run.execute(document, {"items": [1, 2, 3, 4]}))["output"] == {"results": [99]}
        assert sorted(cleaned) == [1, 2]
    assert sorted(active) == [1, 2]
