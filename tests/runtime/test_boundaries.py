"""Ingress, engine failures, routing and observable active-frame ownership."""

import asyncio

import axcutor_core
import pytest
from axcutor_runtime.codec import dumps, loads
from helpers import INTEGER, N, action, array, builtin, graph, inline, literal
from temporalio import activity, exceptions
from temporalio.worker import Worker


async def test_invalid_root_input_is_terminal_before_business_dispatch(execution):
    seen = []

    async def work(request, signal):
        seen.append(request)
        return {"output": {}}

    document = {
        "input": {"n": INTEGER},
        "output": {},
        "run": {"executor": "builtin", "content": "work"},
    }
    async with execution({"work": work}) as run:
        for inputs in ({}, {"n": True}, {"n": 1.0}, {"n": 1, "extra": 2}):
            assert (await run.failed(document, inputs)).type == "EngineError"
    assert seen == []


async def test_nonfinite_builtin_panic_crosses_parent_catch_and_retry(execution):
    sub = graph(
        {
            "multiply": builtin(
                "math", {"op": literal("mul"), "left": "n", "right": "n"}, {"result": "value"}
            )
        },
        ["start -> multiply", "multiply -> end"],
        outputs={"value": N},
    )
    document = graph(
        {
            "sub": inline(
                sub,
                ["n"],
                retry={"max_attempts": 2, "backoff_ms": 0, "on": ["execution", "contract"]},
                catch={"caught": "*"},
            )
        },
        ["start -> sub", "sub -> end", "sub.caught -> end"],
        inputs={"n": N},
        outputs={"value": N},
        variables={"value": {**N, "default": 3}},
    )
    async with execution({}) as run:
        failure = await run.failed(document, {"n": 1e308})
        assert failure.type == "EngineError"
        assert "non-finite" in failure.message


@pytest.mark.parametrize(
    ("responses", "retry_on", "expected_value", "expected_calls"),
    [
        (["invalid", "valid"], ["timeout", "contract"], 42, 2),
        (["invalid", "invalid", "valid"], ["timeout", "contract"], 7, 2),
        (["invalid", "valid"], ["timeout"], 7, 1),
        (["execution", "invalid", "valid"], ["timeout", "execution", "contract"], 7, 2),
    ],
    ids=["contract-retry", "contract-exhausted", "contract-not-retryable", "shared-budget"],
)
async def test_workflow_revalidates_remote_activity_response_and_uses_route(
    execution, server, responses, retry_on, expected_value, expected_calls
):
    seen = []

    @activity.defn(name="execute_run")
    async def remote(request_json: str) -> str:
        seen.append(loads(request_json))
        response = responses[len(seen) - 1]
        if response == "execution":
            raise exceptions.ApplicationError("remote execution failed", type="execution")
        return dumps({"output": {"value": True if response == "invalid" else 42}})

    document = graph(
        {
            "work": action(
                "remote",
                outputs={"value": INTEGER},
                catch={"bad": ["contract"]},
                retry={"max_attempts": 2, "backoff_ms": 10, "on": retry_on},
            )
        },
        ["start -> work", "work -> end", "work.bad -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 7}},
    )
    async with execution({}) as run:
        async with Worker(server.client, task_queue=run.queue + "-remote", activities=[remote]):
            result = await run.execute(
                document, options={"task_queues": {"builtin": run.queue + "-remote"}}
            )
        assert len(seen) == expected_calls
        assert result["output"] == {"value": expected_value}
    assert all(
        request["run_spec"] == {"executor": "builtin", "content": "remote"} for request in seen
    )
    assert all(request["input_schema"] == {} for request in seen)


async def test_query_tree_is_independent_and_iteration_siblings_keep_depth(execution):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work(request, signal):
        calls.append(request["input_snapshot"])
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return {"output": {"results": request["input_snapshot"]["item"]}}

    sub = graph(
        {"work": action("work", ["item"], {"results": INTEGER})},
        ["start -> work", "work -> end"],
        outputs={"results": INTEGER},
        variables={"private": {**INTEGER, "default": 11}},
    )
    document = graph(
        {"map": inline(sub, for_each={"source": "items", "item": "item", "concurrency": 2})},
        ["start -> map", "map -> end"],
        inputs={"items": array()},
        outputs={"results": array()},
        variables={"secret": {**INTEGER, "default": 99}, "results": array()},
    )
    async with execution({"work": work}) as run:
        handle = await run.start(document, {"items": [1, 2, 3]}, options={"max_call_depth": 2})
        await asyncio.wait_for(entered.wait(), 10)
        snapshot = await handle.query("get_runtime_snapshot")
        root = snapshot["frames"][0]
        assert root["steps"] == 2 and root["call_depth"] == 1
        assert len(root["children"]) == 2
        for iteration in root["children"]:
            block = iteration["children"][0]
            assert iteration["kind"] == "iteration" and iteration["call_depth"] == 1
            variables = loads(iteration["env_json"])
            assert variables["items"] == [1, 2, 3] and variables["secret"] == 99
            assert "results" not in variables  # Unbound parent output stays absent.
            assert block["kind"] == "block" and block["call_depth"] == 2
            assert loads(block["env_json"])["private"] == 11
            assert "secret" not in loads(block["env_json"])
        root["env_json"] = "{}"
        fresh = await handle.query("get_runtime_snapshot")
        assert loads(fresh["frames"][0]["env_json"])["secret"] == 99
        assert fresh["frames"][0]["steps"] == 2
        release.set()
        assert (await run.finish(handle))["output"] == {"results": [1, 2, 3]}
    assert all(list(snapshot) == ["item"] for snapshot in calls)


async def test_float_lexical_category_and_extreme_numbers_survive_real_history(execution):
    # Literal 1e0 must remain number in the normalized ArgValue. Narrowing it to
    # integer would make the addition exact instead of f64 rounding.
    document = graph(
        {
            "math": builtin(
                "math",
                {"op": literal("add"), "left": literal(loads("1e0")), "right": 9007199254740992},
                {"result": "value"},
            )
        },
        ["start -> math", "math -> end"],
        outputs={"value": N},
    )
    async with execution({}) as run:
        assert dumps((await run.execute(document))["output"]["value"]) == "9007199254740992.0"

    async def echo(request, signal):
        return {"output": request["input_snapshot"]}

    extreme = {"huge": loads("9" * 5000), "exponent": loads("1e999999999999999999999")}
    echo_document = {
        "input": {"huge": INTEGER, "exponent": N},
        "output": {"huge": INTEGER, "exponent": N},
        "run": {"executor": "builtin", "content": "echo"},
    }
    async with execution({"echo": echo}) as run:
        result = await run.execute(echo_document, extreme)
        expected = axcutor_core.validate_input(dumps(extreme), dumps(echo_document["input"]))
        assert dumps(result["output"]) == expected
