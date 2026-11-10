"""Runtime ingress, SDK configuration ownership and heartbeat history evolution."""

import asyncio
import inspect
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime import workflow as interpreter
from axcutor_runtime.activities import RunActivities
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker, workflow_runner
from axcutor_runtime.workflow import parse_runtime_options
from helpers import action, call, graph
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker


@pytest.mark.parametrize(
    ("options", "timeout", "interval"),
    [
        ({}, 30000, 10000),
        ({"heartbeat_timeout_ms": 1000}, 1000, 333),
        ({"heartbeat_timeout_ms": 120000}, 120000, 40000),
        ({"heartbeat_timeout_ms": 2}, 2, 1),
        ({"heartbeat_interval_ms": 200}, 30000, 200),
        ({"heartbeat_timeout_ms": 1000, "heartbeat_interval_ms": 7}, 1000, 7),
        ({"heartbeat_timeout_ms": 10000, "heartbeat_interval_ms": 1000}, 10000, 1000),
    ],
)
def test_heartbeat_defaults_and_explicit_values_survive_repeated_parsing(
    options, timeout, interval
):
    original = deepcopy(options)
    expanded = parse_runtime_options(options)
    assert expanded == {
        "activity_timeout_ms": 604800000,
        "heartbeat_timeout_ms": timeout,
        "heartbeat_interval_ms": interval,
        "max_call_depth": 64,
        "task_queues": {},
        "child_workflow_history_events": None,
        "record_trace": False,
    }
    assert parse_runtime_options(expanded) == expanded
    assert options == original


@pytest.mark.parametrize(
    "name",
    ["activity_timeout_ms", "heartbeat_timeout_ms", "heartbeat_interval_ms", "max_call_depth"],
)
@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "1"])
def test_runtime_control_values_require_positive_integers(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        parse_runtime_options({name: value})


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        False,
        "",
        {"max_concurrent_activities": 1},
        {"timeout_ms": 1000},
        {"heartbeat_timeout_ms": 1},
        {"heartbeat_interval_ms": 30000},
        {"heartbeat_timeout_ms": 2, "heartbeat_interval_ms": 2},
        {"task_queues": []},
        {"task_queues": {1: "queue"}},
        {"task_queues": {"builtin": ""}},
        {"task_queues": {"builtin": None}},
        *({"child_workflow_history_events": value} for value in (True, 0, -1, 1.0, "1")),
    ],
)
def test_runtime_options_reject_shapes_unknown_keys_and_invalid_relations(options):
    with pytest.raises(ValueError):
        parse_runtime_options(options)


def test_runtime_options_copy_routes_without_prechecking_unused_backend_durations():
    options = {
        "task_queues": {"builtin": "business"},
        "child_workflow_history_events": 1,
        "activity_timeout_ms": 10**100,
        "heartbeat_timeout_ms": 10**100,
    }
    expanded = parse_runtime_options(options)
    assert expanded["heartbeat_interval_ms"] == 10**100 // 3
    expanded["task_queues"]["builtin"] = "other"
    assert options["task_queues"] == {"builtin": "business"}


async def test_worker_uses_sdk_defaults_and_accepts_resource_and_throttle_overrides(server):
    defaults = create_worker(server.client, str(uuid4())).config()
    for name in ("max_heartbeat_throttle_interval", "default_heartbeat_throttle_interval"):
        assert defaults[name] == inspect.signature(Worker).parameters[name].default
    overrides = {
        "max_heartbeat_throttle_interval": timedelta(milliseconds=200),
        "default_heartbeat_throttle_interval": timedelta(milliseconds=150),
        "max_concurrent_activities": 1,
        "max_concurrent_workflow_tasks": 2,
        "max_cached_workflows": 10,
    }
    configured = create_worker(server.client, str(uuid4()), **overrides).config()
    assert {name: configured[name] for name in overrides} == overrides


@pytest.mark.parametrize(
    "options",
    [None, [], False, {"heartbeat_interval_ms": None}, {"heartbeat_timeout_ms": 1}, {"other": 1}],
)
async def test_direct_workflow_options_fail_terminally_before_dispatch(execution, options):
    document = {
        "input": {},
        "output": {},
        "run": {"executor": "builtin", "content": "return Output()"},
    }
    async with execution() as run:
        handle = await run.server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {
                "bundle_json": compile_bundle(
                    "main",
                    {"main": {"packages": {"main": {"format": "json", "text": dumps(document)}}}},
                ),
                "input_json": "{}",
                "options": options,
            },
            id=str(uuid4()),
            task_queue=run.queue,
        )
        run.handles.append(handle)
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), 10)
        assert failure.value.cause.type == "EngineError"
        assert failure.value.cause.non_retryable
        history = await handle.fetch_history()
        assert not any(
            e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED for e in history.events
        )


@pytest.fixture
async def history_server():
    # Cold Updates need the real server rather than the time-skipping emulator.
    async with await WorkflowEnvironment.start_local(
        dev_server_download_version="v1.6.1"
    ) as environment:
        yield environment


async def test_legacy_heartbeat_history_resumes_with_new_defaults_and_old_child_options(
    history_server, monkeypatch
):
    # Record the previous heartbeat baseline on a real Server. Only the recording
    # Worker is unsandboxed so it can use this scoped legacy default provider;
    # resumed execution and both offline replays use the production sandbox.
    server = history_server
    current_parser = parse_runtime_options

    def legacy_options(options):
        return current_parser(
            {"heartbeat_timeout_ms": 10000, "heartbeat_interval_ms": 1000, **options}
        )

    seen = []

    async def work(request, signal):
        name = request["run_spec"]["content"]
        seen.append((name, activity.info().attempt, request["heartbeat_interval_ms"]))
        if name == "before" and activity.info().attempt == 1:
            raise RuntimeError("exercise the historical native retry policy")
        return {"output": {}}

    child = graph(
        {
            "before": action("child-before"),
            "wait": {"run": {"executor": "signal"}},
            "after": action("child-after"),
        },
        ["start -> before", "before -> wait", "wait -> after", "after -> end"],
    )
    root = graph(
        {
            "before": action(
                "before",
                retry={"max_attempts": 2, "backoff_ms": 10, "on": ["execution", "timeout"]},
            ),
            "child": call("child"),
            "after": action("after"),
        },
        ["start -> before", "before -> child", "child -> after", "after -> end"],
    )
    request = {
        "bundle_json": compile_bundle(
            "main",
            {
                "main": {"packages": {"main": {"format": "json", "text": dumps(root)}}},
                "child": {"packages": {"child": {"format": "json", "text": dumps(child)}}},
            },
        ),
        "input_json": "{}",
        "options": {"child_workflow_history_events": 1},
    }
    queue = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(interpreter, "parse_runtime_options", legacy_options)
        async with Worker(
            server.client,
            task_queue=queue,
            workflows=[AxcutorInterpreterWorkflow],
            activities=[RunActivities({"builtin": work}).execute_run],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await server.client.start_workflow(
                AxcutorInterpreterWorkflow.run, request, id=str(uuid4()), task_queue=queue
            )
            async with asyncio.timeout(15):
                while True:
                    snapshot = await handle.query("get_runtime_snapshot")
                    children = snapshot["frames"][0]["children"]
                    if children and children[0].get("child_execution"):
                        remote = children[0]["child_execution"]
                        child_handle = server.client.get_workflow_handle(
                            remote["workflow_id"], run_id=remote["run_id"]
                        )
                        child_snapshot = await child_handle.query("get_runtime_snapshot")
                        if child_snapshot["frames"][0]["waiting"] == ["child/wait"]:
                            break
                    await asyncio.sleep(0.01)

    async with create_worker(server.client, queue, {"builtin": work}):
        async with asyncio.timeout(15):
            await child_handle.execute_update(
                AxcutorInterpreterWorkflow.deliver_signal,
                dumps({"address": "child/wait", "output": {}}),
            )
        assert loads(await asyncio.wait_for(handle.result(), 15)) == {"branch": None, "output": {}}
    assert seen == [
        ("before", 1, 1000),
        ("before", 2, 1000),
        ("child-before", 1, 1000),
        ("child-after", 1, 1000),
        ("after", 1, 10000),
    ]
    replayer = Replayer(workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner())
    for owner, timeouts in ((handle, [10000, 30000]), (child_handle, [10000, 10000])):
        history = await owner.fetch_history()
        scheduled = [
            e.activity_task_scheduled_event_attributes
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]
        assert [a.heartbeat_timeout.ToMilliseconds() for a in scheduled] == timeouts
        if owner is child_handle:
            started = history.events[0].workflow_execution_started_event_attributes
            [child_request] = await server.client.data_converter.decode(started.input.payloads)
            assert child_request["options"] == legacy_options(request["options"])
        await replayer.replay_workflow(history)
