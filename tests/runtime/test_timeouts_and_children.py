"""Real Activity timers, Child Workflow failures and cancellation boundaries."""

import asyncio

import pytest
from axcutor_runtime.activities import RunActivities
from axcutor_runtime.codec import dumps
from helpers import INTEGER, action, assign, call, graph, inline
from temporalio import activity, exceptions
from temporalio.api.enums.v1 import EventType
from temporalio.testing import ActivityEnvironment


@pytest.mark.parametrize("outer_timeout", [None, 100])
async def test_retry_backoff_is_outside_attempt_budget_but_inside_composite_deadline(
    execution, outer_timeout
):
    calls = []

    async def work(request, signal):
        calls.append(request["attempt_timeout_ms"])
        await asyncio.sleep(0.02)
        return {"output": {"value": True if len(calls) == 1 else 7}}

    inner = graph(
        {
            "work": action(
                "work",
                outputs={"value": INTEGER},
                timeout_ms=80,
                retry={"max_attempts": 2, "backoff_ms": 300, "on": ["contract"]},
            )
        },
        ["start -> work", "work -> end"],
        outputs={"value": INTEGER},
    )
    modifiers = {"timeout_ms": outer_timeout} if outer_timeout else {}
    document = graph(
        {"sub": inline(inner, catch={"timed": ["timeout"]}, **modifiers)},
        ["start -> sub", "sub -> end", "sub.timed -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 99}},
    )
    async with execution({"work": work}) as run:
        assert (await run.execute(document))["output"] == {"value": 99 if outer_timeout else 7}
    assert calls == [80] * (1 if outer_timeout else 2)


@pytest.mark.parametrize("composite", [False, True])
async def test_attempt_timeout_retries_then_catches_and_discards_late_output(execution, composite):
    calls, signals = [], []
    cleaned = asyncio.Event()

    async def work(request, signal):
        calls.append(activity.info().attempt)
        signals.append(signal)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert signal.aborted
            cleaned.set()
            return {"output": {"value": 999}}

    modifiers = {
        "timeout_ms": 80,
        "retry": {"max_attempts": 2, "backoff_ms": 0, "on": ["timeout"]},
        "catch": {"late": ["timeout"]},
    }
    if composite:
        node = inline(
            graph(
                {"work": action("work", outputs={"value": INTEGER})},
                ["start -> work", "work -> end"],
                outputs={"value": INTEGER},
            ),
            **modifiers,
        )
    else:
        node = action("work", outputs={"value": INTEGER}, **modifiers)
    document = graph(
        {"work": node},
        ["start -> work", "work -> end", "work.late -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 5}},
        max_steps=1,
    )
    async with execution({"work": work}) as run:
        assert (await run.execute(document))["output"] == {"value": 5}
        await asyncio.wait_for(cleaned.wait(), 2)
    assert calls == [1, 1]
    assert all(signal.aborted for signal in signals)


@pytest.mark.parametrize("retry", [None, {"max_attempts": 3, "backoff_ms": 0, "on": ["execution"]}])
async def test_host_activity_deadline_is_finite_and_not_retried_when_timeout_excluded(
    execution, retry
):
    calls = []

    async def work(request, signal):
        calls.append(1)
        await asyncio.Event().wait()

    modifiers = {"retry": retry} if retry else {}
    document = {
        "input": {},
        "output": {},
        "run": {"executor": "builtin", "content": "work"},
        **modifiers,
    }
    async with execution({"work": work}) as run:
        assert (await run.failed(document, options={"activity_timeout_ms": 60})).type == "timeout"
    assert len(calls) == 1


async def test_native_activity_timeout_retry_and_nonretryable_execution(execution):
    attempts, signals = [], []

    async def work(request, signal):
        attempts.append(activity.info().attempt)
        signals.append(signal)
        if len(attempts) == 1:
            await asyncio.Event().wait()
        return {"output": {"value": 7}}

    document = {
        "input": {},
        "output": {"value": INTEGER},
        "run": {"executor": "builtin", "content": "work"},
        "timeout_ms": 60,
        "retry": {"max_attempts": 2, "backoff_ms": 10, "on": ["timeout"]},
    }
    async with execution({"work": work}) as run:
        assert (await run.execute(document))["output"] == {"value": 7}
    assert attempts == [1, 2]
    assert signals[0].aborted and not signals[1].aborted

    rejected = []

    async def fail(request, signal):
        rejected.append(activity.info().attempt)
        raise RuntimeError("execution is not retryable under this policy")

    async with execution({"work": fail}) as run:
        assert (await run.failed(document)).type == "execution"
    assert rejected == [1]


@pytest.mark.parametrize("late_error", [False, True])
async def test_real_activity_environment_local_watchdog_owns_cleanup_and_late_results(late_error):
    signals, cleaned, beats = [], [], []

    async def work(request, signal):
        signals.append(signal)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert signal.aborted and signal.reason == "timeout"
            await asyncio.sleep(0.01)
            cleaned.append(True)
            if late_error:
                raise RuntimeError("late failure") from None
            return {"output": {"value": "invalid late result"}}

    environment = ActivityEnvironment()
    environment.on_heartbeat = lambda *details: beats.append(details)
    request = {
        "run_spec": {"executor": "work", "content": "unused"},
        "input_schema": {},
        "input_snapshot": {},
        "output_schema": {"value": INTEGER},
        "branches_contract": {},
        "attempt_timeout_ms": 30,
        "heartbeat_interval_ms": 5,
    }
    with pytest.raises(exceptions.ApplicationError) as failure:
        await environment.run(RunActivities({"work": work}).execute_run, dumps(request))
    assert failure.value.type == "timeout"
    assert cleaned == [True] and signals[0].aborted
    assert len(beats) >= 2
    count = len(beats)
    await asyncio.sleep(0.02)
    assert len(beats) == count


async def test_activity_distinguishes_invalid_business_json_from_broken_host_schema():
    cycle = {}
    cycle["cycle"] = cycle

    async def cyclic_response(request, signal):
        return {"output": cycle}

    async def valid_response(request, signal):
        return {"output": {}}

    request = {
        "run_spec": {"executor": "work", "content": "unused"},
        "input_schema": {},
        "input_snapshot": {},
        "output_schema": {},
        "branches_contract": {},
        "attempt_timeout_ms": 1000,
        "heartbeat_interval_ms": 20,
    }
    with pytest.raises(exceptions.ApplicationError) as failure:
        await ActivityEnvironment().run(
            RunActivities({"work": cyclic_response}).execute_run, dumps(request)
        )
    assert failure.value.type == "contract"
    request["output_schema"] = {"bad": {"type": "unknown"}}
    with pytest.raises(exceptions.ApplicationError) as failure:
        await ActivityEnvironment().run(
            RunActivities({"work": valid_response}).execute_run, dumps(request)
        )
    assert failure.value.type == "EngineError" and failure.value.non_retryable


def child_events(history):
    return [
        event
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]


@pytest.mark.parametrize("kind", ["inline", "workflow", "child"])
async def test_composite_deadline_allows_timely_normal_branch_and_output(execution, kind):
    async def work(request, signal):
        values = request["input_snapshot"]
        return {"branch": "ready", "output": {"value": values["n"] + values["bonus"]}}

    callee = graph(
        {
            "work": action(
                "work", ["n", "bonus"], {"value": INTEGER}, branches={"ready": "computed"}
            )
        },
        ["start -> work", "work.ready -> end.ready"],
        inputs={"n": INTEGER, "bonus": INTEGER},
        outputs={"value": INTEGER},
        branches={"ready": "computed"},
    )
    modifiers = {"timeout_ms": 5000, "catch": {"timed": ["timeout"]}}
    packages = {}
    if kind == "inline":
        node = inline(callee, ["n", "bonus"], **modifiers)
    else:
        node = call("callee", {"n": "n", "bonus": "bonus"}, {"value": "value"}, **modifiers)
        packages["callee"] = {"format": "json", "text": dumps(callee)}
    document = graph(
        {"sub": node},
        ["start -> sub", "sub.ready -> end.ok", "sub.timed -> end.timed"],
        inputs={"n": INTEGER, "bonus": INTEGER},
        outputs={"value": INTEGER},
        branches={
            "ok": {"description": "completed", "output": ["value"]},
            "timed": {"description": "timed out", "output": []},
        },
    )
    async with execution({"work": work}) as run:
        handle = await run.start(
            document,
            {"n": 40, "bonus": 2},
            packages=packages,
            options={"child_workflow_history_events": 1} if kind == "child" else {},
        )
        assert await run.finish(handle) == {"branch": "ok", "output": {"value": 42}}
        assert len(child_events(await handle.fetch_history())) == int(kind == "child")


@pytest.mark.parametrize("child", [False, True])
async def test_call_success_preserves_values_and_runs_child_only_at_history_boundary(
    execution, child
):
    async def work(request, signal):
        return {"output": {"value": request["input_snapshot"]["n"]}}

    callee = {
        "input": {"n": INTEGER},
        "output": {"value": INTEGER},
        "run": {"executor": "builtin", "content": "work"},
    }
    document = graph(
        {"call": call("callee", {"n": "n"}, {"value": "answer"})},
        ["start -> call", "call -> end"],
        inputs={"n": INTEGER},
        outputs={"answer": INTEGER},
    )
    async with execution({"work": work}) as run:
        handle = await run.start(
            document,
            {"n": 999999999999999999999999},
            packages={"callee": {"format": "json", "text": dumps(callee)}},
            options={"child_workflow_history_events": 1} if child else {},
        )
        assert (await run.finish(handle))["output"] == {"answer": 999999999999999999999999}
        events = child_events(await handle.fetch_history())
        assert len(events) == int(child)
        if child:
            assert (
                events[
                    0
                ].start_child_workflow_execution_initiated_event_attributes.retry_policy.maximum_attempts
                == 1
            )


@pytest.mark.parametrize("selector", ["execution", "contract", "max_steps"])
async def test_child_selectors_survive_boundary_parent_owns_retry_and_catch(execution, selector):
    calls = []

    async def work(request, signal):
        calls.append(1)
        if selector == "execution":
            raise RuntimeError("business failure")
        return {"output": {"value": True}}

    if selector == "max_steps":
        callee = graph(
            {"a": assign(1, "value"), "b": assign(2, "value")},
            ["start -> a", "a -> b", "b -> end"],
            outputs={"value": INTEGER},
            max_steps=1,
        )
    else:
        callee = {
            "input": {},
            "output": {"value": INTEGER},
            "run": {"executor": "builtin", "content": "work"},
        }
    document = graph(
        {
            "call": call(
                "callee",
                outputs={"value": "value"},
                retry={"max_attempts": 2, "backoff_ms": 0, "on": [selector]},
                catch={"failed": [selector]},
            )
        },
        ["start -> call", "call -> end", "call.failed -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 7}},
    )
    async with execution({"work": work}) as run:
        handle = await run.start(
            document,
            packages={"callee": {"format": "json", "text": dumps(callee)}},
            options={"child_workflow_history_events": 1},
        )
        assert (await run.finish(handle))["output"] == {"value": 7}
        assert len(child_events(await handle.fetch_history())) == 2
    assert len(calls) == (0 if selector == "max_steps" else 2)


async def test_child_timeout_cancellation_waits_for_descendant_cleanup(execution):
    cleaned = asyncio.Event()

    async def work(request, signal):
        try:
            await asyncio.Event().wait()
        finally:
            assert signal.aborted
            await asyncio.sleep(0.02)
            cleaned.set()

    async def check(request, signal):
        assert cleaned.is_set()
        return {"output": {}}

    callee = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "work"}}
    document = graph(
        {
            "call": call("callee", timeout_ms=150, catch={"timed": ["timeout"]}),
            "check": action("check"),
        },
        ["start -> call", "call -> end", "call.timed -> check", "check -> end"],
    )
    async with execution({"work": work, "check": check}) as run:
        handle = await run.start(
            document,
            packages={"callee": {"format": "json", "text": dumps(callee)}},
            options={"child_workflow_history_events": 1},
        )
        assert (await run.finish(handle))["output"] == {}
        assert len(child_events(await handle.fetch_history())) == 1
    assert cleaned.is_set()


async def test_child_depth_panic_bypasses_catch_and_dsl_retry(execution):
    middle = graph({"next": call("leaf")}, ["start -> next", "next -> end"])
    leaf = {"input": {}, "output": {}, "run": {"executor": "builtin", "content": "unused"}}
    document = graph(
        {
            "call": call(
                "middle",
                retry={"max_attempts": 3, "backoff_ms": 0, "on": ["execution", "max_steps"]},
                catch={"caught": "*"},
            )
        },
        ["start -> call", "call -> end", "call.caught -> end"],
    )
    async with execution({}) as run:
        failure = await run.failed(
            document,
            packages={
                "middle": {"format": "json", "text": dumps(middle)},
                "leaf": {"format": "json", "text": dumps(leaf)},
            },
            options={"child_workflow_history_events": 1, "max_call_depth": 2},
        )
        assert failure.type == "StackOverflowError"
        assert len(child_events(await run.handles[-1].fetch_history())) == 1
