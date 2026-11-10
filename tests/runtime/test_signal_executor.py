"""Signal lifecycle, lossless Updates, isolated barriers and real History replay."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker, workflow_runner
from helpers import INTEGER, N, action, array, graph, inline
from temporalio import exceptions, workflow
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType, UpdateWorkflowExecutionLifecycleStage
from temporalio.api.update.v1 import UpdateRef
from temporalio.api.workflowservice.v1 import PollWorkflowExecutionUpdateRequest
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker, WorkflowRunner
from temporalio.worker._workflow_instance import _WorkflowLogicFlag


@pytest.fixture(scope="module")
async def update_server():
    # The time-skipping test server does not preserve cancel/Update sequencing
    # when both arrive in one task. Exercise that replay boundary on a real Server.
    async with await WorkflowEnvironment.start_local(
        dev_server_download_version="v1.6.1"
    ) as environment:
        yield environment


def signal_node(inputs=None, outputs=None, **modifiers):
    return {
        "run": {"executor": "signal"},
        "input": inputs or [],
        "output": outputs or {},
        **modifiers,
    }


async def snapshot_when(handle, predicate):
    async with asyncio.timeout(10):
        while True:
            snapshot = await handle.query(AxcutorInterpreterWorkflow.get_runtime_snapshot)
            if predicate(snapshot):
                return snapshot
            await asyncio.sleep(0.01)


async def deliver(handle, address, output=None, branch=None, step=None):
    return loads(
        await handle.execute_update(
            AxcutorInterpreterWorkflow.deliver_signal,
            dumps({"address": address, "output": output or {}, "branch": branch, "step": step}),
        )
    )


def assert_no_activity(history):
    assert not any(
        event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED for event in history.events
    )


class _ActivationRunner(WorkflowRunner):
    """Gate polling at the first command boundary and observe batched input jobs.

    Delegate execution to the project sandbox. Stopping Core polling before the
    initial completion is sent leaves timers/Updates for the next Worker without
    guessing how long a sub-second Timer takes to fire.
    """

    def __init__(self):
        self.inner = workflow_runner()
        self.stop_polling = None
        self.batched_claim_and_deadline = False

    def prepare_workflow(self, definition):
        self.inner.prepare_workflow(definition)

    def set_worker_level_failure_exception_types(self, types):
        self.inner.set_worker_level_failure_exception_types(types)

    def create_instance(self, details):
        instance = self.inner.create_instance(details)
        original = instance.activate

        def activate(activation):
            if any(j.HasField("do_update") for j in activation.jobs) and any(
                j.HasField("fire_timer") for j in activation.jobs
            ):
                self.batched_claim_and_deadline = True
            completion = original(activation)
            if (
                self.stop_polling
                and not activation.is_replaying
                and any(j.HasField("initialize_workflow") for j in activation.jobs)
            ):
                self.stop_polling()
            return completion

        instance.activate = activate
        return instance


async def wait_for_history_event(handle, event_type):
    async with asyncio.timeout(10):
        async for event in handle.fetch_history_events(wait_new_event=True):
            if event.event_type == event_type:
                return event
    raise AssertionError(f"history closed without {event_type}")


async def admit_signal(server, handle, address, output):
    """Observe server admission before restarting polling, without a race sleep."""
    update_id = str(uuid4())
    update = asyncio.create_task(
        handle.execute_update(
            AxcutorInterpreterWorkflow.deliver_signal,
            dumps({"address": address, "output": output}),
            id=update_id,
        )
    )
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    response = await server.client.workflow_service.poll_workflow_execution_update(
                        PollWorkflowExecutionUpdateRequest(
                            namespace=server.client.namespace,
                            update_ref=UpdateRef(
                                workflow_execution=WorkflowExecution(
                                    workflow_id=handle.id, run_id=handle.result_run_id
                                ),
                                update_id=update_id,
                            ),
                        )
                    )
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise
                    continue
                assert response.stage == (
                    UpdateWorkflowExecutionLifecycleStage.UPDATE_WORKFLOW_EXECUTION_LIFECYCLE_STAGE_ADMITTED
                )
                return update
    except BaseException:
        update.cancel()
        await asyncio.gather(update, return_exceptions=True)
        raise


@pytest.mark.parametrize("root", [False, True])
async def test_waiting_addresses_distinguish_root_run_from_graph_node_named_run(execution, root):
    types = {
        "number": N,
        "integer": INTEGER,
        "ordered": {"type": "object", "additionalProperties": N},
    }
    if root:
        document = {"input": {}, "output": types, "run": {"executor": "signal"}}
    else:
        document = graph(
            {"run": signal_node(outputs=types)}, ["start -> run", "run -> end"], outputs=types
        )
    value = loads(
        '{"number":1.000000000000000000000001,"integer":'
        + "9" * 5000
        + ',"ordered":{"z":1e99999999999999999999999999,"a":1e0}}'
    )
    async with execution() as run:
        handle = await run.start(document, options={"activity_timeout_ms": 1})
        address = "main" if root else "main/run"
        snapshot = await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == [address])
        assert snapshot["frames"][0]["steps"] == (0 if root else 1)
        # The Activity default is irrelevant even after it expires in virtual time.
        await run.server.sleep(1)
        assert (await handle.query("get_runtime_snapshot")) == snapshot
        ack = await deliver(handle, address, value)
        final = await run.finish(handle)
        assert dumps(ack) == dumps(final)
        assert dumps(final["output"]).replace("e+", "e") == dumps(value).replace("e+", "e")
        assert_no_activity(await handle.fetch_history())


async def test_validator_rejections_leave_waiter_steps_and_history_unchanged(execution):
    document = graph(
        {"decision": signal_node(outputs={"value": INTEGER}, branches={"ok": "approved"})},
        ["start -> decision", "decision.ok -> end"],
        outputs={"value": INTEGER},
    )
    async with execution() as run:
        handle = await run.start(document)
        address = "main/decision"
        snapshot = await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == [address])
        await run.server.sleep(0.01)
        before = await handle.fetch_history()
        for request, error_type in [
            ("{", "SignalValidationError"),
            ("[]", "SignalValidationError"),
            (dumps({"address": address, "output": {}, "extra": 1}), "SignalValidationError"),
            (dumps({"address": address, "output": {}, "step": True}), "SignalValidationError"),
            (dumps({"address": "main/missing", "output": {}}), "SignalNotWaitingError"),
            (dumps({"address": address, "output": {}, "step": 2}), "SignalNotWaitingError"),
            (dumps({"address": address, "output": {"value": 1}}), "SignalValidationError"),
            (dumps({"address": address, "branch": "bad", "output": {}}), "SignalValidationError"),
            (dumps({"address": address, "branch": "ok", "output": {}}), "SignalValidationError"),
            (
                dumps({"address": address, "branch": "ok", "output": {"value": True}}),
                "SignalValidationError",
            ),
        ]:
            with pytest.raises(WorkflowUpdateFailedError) as failure:
                await handle.execute_update(AxcutorInterpreterWorkflow.deliver_signal, request)
            assert isinstance(failure.value.cause, exceptions.ApplicationError)
            assert failure.value.cause.type == error_type
            assert await handle.query("get_runtime_snapshot") == snapshot
        after = await handle.fetch_history()
        # Temporal may persist Workflow Task bookkeeping for rejected Updates;
        # no Update is accepted/completed and no business command is emitted.
        assert all(
            e.event_type
            in (
                EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED,
                EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED,
                EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
            )
            for e in after.events[len(before.events) :]
        )
        assert await deliver(handle, address, {"value": 7, "ignored": True}, "ok", 1) == {
            "branch": "ok",
            "output": {"value": 7},
        }
        assert (await run.finish(handle))["output"] == {"value": 7}
        assert_no_activity(await handle.fetch_history())


async def test_step_lock_rejects_stale_and_duplicate_updates_across_loop_rounds(execution):
    document = graph(
        {"again": signal_node(branches={"repeat": "next round", "stop": "finish"})},
        ["start -> again", "again.repeat -> again", "again.stop -> end"],
    )
    async with execution() as run:
        handle = await run.start(document)
        address = "main/again"
        await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == [address])
        await deliver(handle, address, branch="repeat", step=1)
        await snapshot_when(handle, lambda s: s["frames"][0]["steps"] == 2)
        with pytest.raises(WorkflowUpdateFailedError) as failure:
            await deliver(handle, address, branch="repeat", step=1)
        assert failure.value.cause.type == "SignalNotWaitingError"
        responses = await asyncio.gather(
            deliver(handle, address, branch="repeat", step=2),
            deliver(handle, address, branch="repeat", step=2),
            return_exceptions=True,
        )
        assert sum(isinstance(response, dict) for response in responses) == 1
        rejected = next(r for r in responses if isinstance(r, WorkflowUpdateFailedError))
        assert rejected.cause.type == "SignalNotWaitingError"
        await snapshot_when(handle, lambda s: s["frames"][0]["steps"] == 3)
        await deliver(handle, address, branch="stop", step=3)
        await run.finish(handle)


async def test_for_each_holds_permits_and_commits_ordered_results_at_barrier(execution):
    document = graph(
        {
            "batch": signal_node(
                ["item"],
                {"values": INTEGER},
                for_each={"source": "items", "item": "item", "concurrency": 2},
            )
        },
        ["start -> batch", "batch -> end"],
        inputs={"items": array()},
        outputs={"values": array()},
        variables={"values": {**array(), "default": [99]}},
    )
    async with execution() as run:
        handle = await run.start(document, {"items": [10, 20, 30]})
        snapshot = await snapshot_when(handle, lambda s: len(s["frames"][0]["children"]) == 2)
        root = snapshot["frames"][0]
        assert root["waiting"] == [] and root["steps"] == 2
        assert [c["waiting"] for c in root["children"]] == [["main/batch[0]"], ["main/batch[1]"]]
        await deliver(handle, "main/batch[1]", {"values": 2})
        snapshot = await snapshot_when(handle, lambda s: s["frames"][0]["steps"] == 3)
        assert snapshot["frames"][0]["env_json"] == dumps({"items": [10, 20, 30], "values": [99]})
        await deliver(handle, "main/batch[2]", {"values": 3})
        await deliver(handle, "main/batch[0]", {"values": 1})
        assert (await run.finish(handle))["output"] == {"values": [1, 2, 3]}
        assert_no_activity(await handle.fetch_history())


async def test_fork_join_exposes_multiple_waiters_and_buffers_until_all_complete(execution):
    document = graph(
        {
            "left": signal_node(outputs={"a": INTEGER}),
            "middle": signal_node(outputs={"b": INTEGER}),
            "right": signal_node(outputs={"c": INTEGER}),
        },
        ["start -> [left, middle, right] -> end"],
        outputs={"a": INTEGER, "b": INTEGER, "c": INTEGER},
        variables={
            "a": {**INTEGER, "default": 1},
            "b": {**INTEGER, "default": 2},
            "c": {**INTEGER, "default": 3},
        },
    )
    async with execution() as run:
        handle = await run.start(document)
        await snapshot_when(handle, lambda s: len(s["frames"][0]["waiting"]) == 3)
        await deliver(handle, "main/right", {"c": 30}, step=3)
        await deliver(handle, "main/middle", {"b": 20}, step=3)
        snapshot = await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == ["main/left"])
        assert loads(snapshot["frames"][0]["env_json"]) == {"a": 1, "b": 2, "c": 3}
        assert snapshot["frames"][0]["cursor"] == "start"
        await deliver(handle, "main/left", {"a": 10}, step=3)
        assert (await run.finish(handle))["output"] == {"a": 10, "b": 20, "c": 30}
        history = await handle.fetch_history()
        assert sum(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events) == 1
        assert_no_activity(history)


async def test_fail_fast_removes_waiters_and_discards_delivered_sibling_output(execution):
    release = asyncio.Event()

    async def fail(request, signal):
        await release.wait()
        raise RuntimeError("sibling failed")

    sub = graph(
        {
            "value": signal_node(outputs={"value": INTEGER}),
            "hold": signal_node(),
            "bad": action("bad"),
        },
        ["start -> [value, hold, bad] -> end"],
        outputs={"value": INTEGER},
    )
    document = graph(
        {"sub": inline(sub, catch={"failed": ["execution"]})},
        ["start -> sub", "sub -> end", "sub.failed -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 99}},
    )
    async with execution({"bad": fail}) as run:
        handle = await run.start(document)
        await snapshot_when(handle, lambda s: len(s["frames"][0]["children"][0]["waiting"]) == 2)
        await deliver(handle, "main/sub/value", {"value": 7})
        release.set()
        assert (await run.finish(handle))["output"] == {"value": 99}


async def test_timeout_retries_keep_one_step_and_leave_no_expired_waiter(execution):
    document = graph(
        {
            "timed": signal_node(
                timeout_ms=30, retry={"max_attempts": 3, "backoff_ms": 10}, catch={"late": "*"}
            ),
            "after": signal_node(),
        },
        ["start -> timed", "timed -> end", "timed.late -> after", "after -> end"],
        max_steps=2,
    )
    async with execution() as run:
        handle = await run.start(document)
        await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == ["main/timed"])
        await run.server.sleep(0.3)
        snapshot = await snapshot_when(
            handle, lambda s: s["frames"][0]["waiting"] == ["main/after"]
        )
        assert snapshot["frames"][0]["steps"] == 2
        with pytest.raises(WorkflowUpdateFailedError) as failure:
            await deliver(handle, "main/timed")
        assert failure.value.cause.type == "SignalNotWaitingError"
        await deliver(handle, "main/after", step=2)
        await run.finish(handle)
        history = await handle.fetch_history()
        timers = [
            e.timer_started_event_attributes.start_to_fire_timeout.ToMilliseconds()
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
        ]
        assert timers.count(30) == 3 and timers.count(10) == 2
        assert len(timers) == 5  # Only attempt deadlines and retry backoffs.
        assert_no_activity(history)


async def test_parent_deadline_discards_child_output_and_cleans_remaining_waiter(execution):
    sub = graph(
        {"decision": signal_node(outputs={"value": INTEGER}), "hold": signal_node()},
        ["start -> decision", "decision -> hold", "hold -> end"],
        outputs={"value": INTEGER},
    )
    document = graph(
        {"sub": inline(sub, timeout_ms=3600000, catch={"late": ["timeout"]})},
        ["start -> sub", "sub -> end", "sub.late -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 99}},
    )
    async with execution() as run:
        handle = await run.start(document)
        await snapshot_when(handle, lambda s: s["frames"][0]["children"][0]["waiting"])
        assert (await deliver(handle, "main/sub/decision", {"value": 7}))["output"] == {"value": 7}
        snapshot = await handle.query("get_runtime_snapshot")
        assert snapshot["frames"][0]["children"][0]["waiting"] == ["main/sub/hold"]
        # Advance virtual time only after observing the committed child value.
        await run.server.sleep(3601)
        assert (await run.finish(handle))["output"] == {"value": 99}
        history = await handle.fetch_history()
        assert sum(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events) == 1
        assert_no_activity(history)


async def test_completed_activity_supplies_initial_boundary_for_a_cold_update(update_server):
    async def prepare(request, signal):
        return {"output": {}}

    document = graph(
        {"prepare": action("prepare"), "decision": signal_node(outputs={"value": INTEGER})},
        ["start -> prepare", "prepare -> decision", "decision -> end"],
        outputs={"value": INTEGER},
    )
    server = update_server
    queue = str(uuid4())
    async with create_worker(server.client, queue, {"builtin": prepare}, max_cached_workflows=0):
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(
                    "main",
                    {"main": {"packages": {"main": {"format": "json", "text": dumps(document)}}}},
                ),
                "input_json": "{}",
            },
            id=str(uuid4()),
            task_queue=queue,
        )
        await snapshot_when(handle, lambda s: s["frames"][0]["waiting"] == ["main/decision"])
        assert (await deliver(handle, "main/decision", {"value": 7}))["output"] == {"value": 7}
        assert loads(await handle.result())["output"] == {"value": 7}
        assert await handle.query("get_runtime_snapshot") == {"frames": []}
    history = await handle.fetch_history()
    assert not any(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events)
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(history)


async def test_one_initialization_timer_survives_four_rounds_and_cold_workers(update_server):
    server = update_server
    document = graph(
        {
            "decision": signal_node(
                outputs={"value": INTEGER}, branches={"repeat": "next round", "stop": "finish"}
            )
        },
        ["start -> decision", "decision.repeat -> decision", "decision.stop -> end"],
        outputs={"value": INTEGER},
    )
    queue = str(uuid4())
    for step in range(1, 5):
        async with create_worker(server.client, queue, max_cached_workflows=0):
            if step == 1:
                handle = await server.client.start_workflow(
                    AxcutorInterpreterWorkflow.run,
                    {
                        "bundle_json": compile_bundle(
                            "main",
                            {
                                "main": {
                                    "packages": {
                                        "main": {"format": "json", "text": dumps(document)}
                                    }
                                }
                            },
                        ),
                        "input_json": "{}",
                    },
                    id=str(uuid4()),
                    task_queue=queue,
                )
            snapshot = await snapshot_when(
                handle,
                lambda s, expected_step=step: (
                    s["frames"][0]["steps"] == expected_step
                    and s["frames"][0]["waiting"] == ["main/decision"]
                ),
            )
            assert snapshot["frames"][0]["steps"] == step
            ack = await deliver(
                handle, "main/decision", {"value": 7}, "repeat" if step < 4 else "stop", step
            )
            assert ack["output"] == {"value": 7}
            if step == 4:
                assert loads(await handle.result())["output"] == {"value": 7}
                assert await handle.query("get_runtime_snapshot") == {"frames": []}
    history = await handle.fetch_history()
    assert sum(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events) == 1
    assert_no_activity(history)
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(history)


@pytest.mark.parametrize("parent_deadline", [None, 100])
async def test_claim_advances_cfg_while_timers_are_delayed(
    update_server, monkeypatch, parent_deadline
):
    server = update_server
    sleep = workflow.sleep

    async def delayed_timer(duration, **kwargs):
        # Model an arbitrarily delayed Timer, not a promised millisecond clock.
        # Neither the initialization Timer nor a still-unexpired parent deadline
        # may hold a claimed signal at its old cursor.
        await sleep(timedelta(days=1), **kwargs)

    monkeypatch.setattr(workflow, "sleep", delayed_timer)
    document = graph(
        {"decision": signal_node(outputs={"value": INTEGER}), "after": signal_node()},
        ["start -> decision", "decision -> after", "after -> end"],
        outputs={"value": INTEGER},
    )
    prefix = "main"
    if parent_deadline:
        document = graph(
            {"sub": inline(document, timeout_ms=parent_deadline, catch={"late": ["timeout"]})},
            ["start -> sub", "sub -> end", "sub.late -> end"],
            outputs={"value": INTEGER},
            variables={"value": {**INTEGER, "default": 99}},
        )
        prefix += "/sub"
    queue = str(uuid4())
    async with create_worker(server.client, queue, max_cached_workflows=0):
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(
                    "main",
                    {"main": {"packages": {"main": {"format": "json", "text": dumps(document)}}}},
                ),
                "input_json": "{}",
            },
            id=str(uuid4()),
            task_queue=queue,
        )
        try:
            await snapshot_when(
                handle,
                lambda s: (
                    s["frames"][0]["children"][0]["waiting"]
                    if parent_deadline
                    else s["frames"][0]["waiting"]
                ),
            )
            assert (await deliver(handle, prefix + "/decision", {"value": 7}))["output"] == {
                "value": 7
            }
            snapshot = await handle.query("get_runtime_snapshot")
            frame = snapshot["frames"][0]
            if parent_deadline:
                frame = frame["children"][0]
            assert frame["cursor"] == "after"
            assert frame["waiting"] == [prefix + "/after"]
            assert loads(frame["env_json"]) == {"value": 7}
            await deliver(handle, prefix + "/after")
            assert loads(await handle.result())["output"] == {"value": 7}
            assert await handle.query("get_runtime_snapshot") == {"frames": []}
        finally:
            await handle.cancel()
    history = await handle.fetch_history()
    assert sum(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events) == 1
    assert not any(e.event_type == EventType.EVENT_TYPE_TIMER_FIRED for e in history.events)
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(history)


@pytest.mark.parametrize("single_batch", [False, True])
async def test_claim_wins_when_its_deadline_is_ready_in_the_same_activation(
    update_server, single_batch
):
    server = update_server
    document = graph(
        {
            "decision": signal_node(
                outputs={"value": INTEGER}, timeout_ms=1, catch={"late": ["timeout"]}
            )
        },
        ["start -> decision", "decision -> end", "decision.late -> end"],
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 99}},
    )
    queue = str(uuid4())
    first_runner = _ActivationRunner()
    first_worker = Worker(
        server.client,
        task_queue=queue,
        workflows=[AxcutorInterpreterWorkflow],
        workflow_runner=first_runner,
        max_cached_workflows=0,
    )
    # Exercise both scheduling modes implemented by SDK 1.35. In single-batch
    # mode the timer resumes before condition checks; the synchronous claim must
    # survive cancellation of the waiting coroutine. The SDK records this flag
    # in History, so an ordinary project Replayer must reproduce the outcome.
    first_worker._set_default_workflow_logic_flag(
        _WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH,
        enabled=single_batch,
    )
    # Same Core shutdown boundary used by Worker.shutdown, synchronously before
    # the first completion reaches the Server and starts any timers.
    first_runner.stop_polling = first_worker._bridge_worker.initiate_shutdown
    async with first_worker:
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(
                    "main",
                    {"main": {"packages": {"main": {"format": "json", "text": dumps(document)}}}},
                ),
                "input_json": "{}",
            },
            id=str(uuid4()),
            task_queue=queue,
        )
        fired = await wait_for_history_event(handle, EventType.EVENT_TYPE_TIMER_FIRED)
    update = await admit_signal(server, handle, "main/decision", {"value": 7})
    runner = _ActivationRunner()
    worker = Worker(
        server.client,
        task_queue=queue,
        workflows=[AxcutorInterpreterWorkflow],
        workflow_runner=runner,
        max_cached_workflows=0,
    )
    worker._set_default_workflow_logic_flag(
        _WorkflowLogicFlag.PROCESS_WORKFLOW_ACTIVATION_JOBS_AS_SINGLE_BATCH,
        enabled=single_batch,
    )
    async with worker:
        assert loads(await update)["output"] == {"value": 7}
        assert loads(await handle.result())["output"] == {"value": 7}
        assert runner.batched_claim_and_deadline
        assert await handle.query("get_runtime_snapshot") == {"frames": []}
    history = await handle.fetch_history()
    accepted = next(
        e
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
    )
    assert fired.event_id < accepted.event_id
    assert_no_activity(history)
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(history)


async def test_cancellation_with_inflight_update_unwinds_all_waiters_after_replay(
    update_server, monkeypatch
):
    sleep = workflow.sleep

    async def delayed_timer(duration, **kwargs):
        # Keep initialization pending through delivery, cancellation and replay.
        await sleep(timedelta(days=1), **kwargs)

    monkeypatch.setattr(workflow, "sleep", delayed_timer)
    server = update_server
    queue = str(uuid4())
    doc = graph(
        {"left": signal_node(), "right": signal_node()},
        ["start -> [left, right] -> end"],
    )
    async with create_worker(server.client, queue, max_cached_workflows=0):
        handle = await server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(
                    "main", {"main": {"packages": {"main": {"format": "json", "text": dumps(doc)}}}}
                ),
                "input_json": "{}",
            },
            id=str(uuid4()),
            task_queue=queue,
        )
        await snapshot_when(handle, lambda s: len(s["frames"][0]["waiting"]) == 2)
    update = await admit_signal(server, handle, "main/left", {})
    await handle.cancel()
    async with create_worker(server.client, queue, max_cached_workflows=0):
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), 10)
        assert isinstance(failure.value.cause, exceptions.CancelledError)
        await asyncio.gather(update, return_exceptions=True)
        assert await handle.query("get_runtime_snapshot") == {"frames": []}
    await Replayer(
        workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
    ).replay_workflow(await handle.fetch_history())
