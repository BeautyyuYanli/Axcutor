"""Trace observations use real Temporal, including closed queries and replay."""

import asyncio

import pytest
from axcutor_runtime.codec import dumps, loads
from helpers import INTEGER, N, action, array, assign, builtin, graph, literal
from temporalio.client import WorkflowFailureError


async def test_closed_trace_keeps_fast_loop_calls_exact_payloads_and_zero_durations(execution):
    document = graph(
        {
            "shift": builtin("array_shift", {"source": "queue"}, {"item": "item", "rest": "queue"}),
            "copy": assign("item", "last"),
        },
        ["start -> shift", "shift.some -> copy", "copy -> shift", "shift.empty -> end"],
        inputs={"queue": array(N)},
        outputs={"last": N},
        variables={"item": N, "last": {**N, "default": 0}},
    )
    value = loads("1.00000000000000000000001")
    async with execution({}) as run:
        handle = await run.start(
            document, {"queue": [value, value]}, options={"record_trace": True}
        )
        await run.finish(handle)  # No live polling: all calls finish in one Workflow task.
        trace = await handle.query("get_execution_trace")
        assert len(trace["spans"]) == 6  # Root, three shifts, two copies.
        copies = [span for span in trace["spans"] if span["address"] == "main/copy"]
        assert len({span["id"] for span in copies}) == 2
        assert all(span["input_json"] == dumps({"source": value}) for span in copies)
        assert all(span["output_json"] == dumps({"last": value}) for span in copies)
        assert all(span["status"] == "completed" for span in trace["spans"])
        assert all(span["started_at"] == span["finished_at"] for span in copies)
        assert trace["spans"][-1]["branch"] == "empty"
        ordinary = await run.start(document, {"queue": [value]})
        await run.finish(ordinary)
        assert (await ordinary.query("get_execution_trace"))["spans"] == []


@pytest.mark.parametrize("native", [False, True])
async def test_trace_independent_parallel_calls_and_retry(execution, native):
    seen = 0

    async def work(request, signal):
        nonlocal seen
        seen += 1
        if seen == 1:
            raise ValueError("first attempt")
        await asyncio.sleep(0.01)
        return {"output": {"value": request["input_snapshot"]["item"]}}

    document = graph(
        {
            "work": action(
                "work",
                ["item"],
                {"value": INTEGER},
                for_each={"source": "items", "item": "item", "concurrency": 2},
                retry={
                    "max_attempts": 2,
                    "backoff_ms": 10 if native else 0,
                    "on": ["execution", "timeout"] if native else ["execution"],
                },
            )
        },
        ["start -> work", "work -> end"],
        inputs={"items": array()},
        outputs={"value": array()},
    )
    async with execution({"work": work}) as run:
        handle = await run.start(document, {"items": [1, 2]}, options={"record_trace": True})
        assert (await run.finish(handle))["output"] == {"value": [1, 2]}
        spans = (await handle.query("get_execution_trace"))["spans"]
        attempts = [span for span in spans if span["kind"] == "attempt"]
        assert seen == 3
        assert len(attempts) == (0 if native else 3)
        if not native:
            assert sum(span["status"] == "failed" for span in attempts) == 1
        else:
            assert (
                sum(
                    event["kind"] == "activity_retry_policy"
                    for span in spans
                    for event in span["events"]
                )
                == 2
            )
        assert len({span["id"] for span in spans}) == len(spans)
        iterations = [
            span
            for span in spans
            if span["address"].endswith(("[0]", "[1]")) and span["kind"] == "run"
        ]
        assert len(iterations) == 2 and iterations[0]["parent_id"] == iterations[1]["parent_id"]
        assert {span["input_json"] for span in iterations} == {'{"item":1}', '{"item":2}'}


@pytest.mark.parametrize("cancel", [False, True])
async def test_closed_failed_cancelled_trace_retains_partial_work_and_wait_events(
    execution, cancel
):
    document = graph(
        {"copy": assign(literal(9), "value"), "wait": action("", ["value"])},
        ["start -> copy", "copy -> wait", "wait -> end"],
        variables={"value": INTEGER},
    )
    document["graph"]["nodes"]["wait"]["run"] = (
        {"executor": "signal"}
        if cancel
        else {"executor": "builtin", "content": 'raise ValueError("failed")'}
    )
    async with execution({}) as run:
        handle = await run.start(document, options={"record_trace": True})
        if cancel:
            async with asyncio.timeout(10):
                while True:
                    trace = await handle.query("get_execution_trace")
                    if any(span["events"] for span in trace["spans"]):
                        break
                    await asyncio.sleep(0.01)
            await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        spans = (await handle.query("get_execution_trace"))["spans"]
        assert spans[1]["output_json"] == '{"value":9}'
        assert spans[-1]["input_json"] == '{"value":9}'
        assert spans[-1]["output_json"] is None
        assert spans[-1]["status"] == ("cancelled" if cancel else "failed")
        if cancel:
            assert spans[-1]["events"][0]["kind"] == "signal_wait"
        else:
            assert "failed" in spans[-1]["error"]["message"]
