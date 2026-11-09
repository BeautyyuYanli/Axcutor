"""Runtime and host integration tests share the real extension, sandbox and Server."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.codec import dumps, loads
from axcutor_runtime.worker import create_worker, workflow_runner
from temporalio import exceptions
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer


@pytest.fixture(scope="session")
async def server():
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


class Execution:
    def __init__(self, server, queue):
        self.server, self.queue = server, queue
        self.handles = []

    async def start(self, document, inputs=None, *, packages=None, options=None):
        source_map = {"main": {"format": "json", "text": dumps(document)}}
        source_map.update(packages or {})
        request = {
            "bundle_json": compile_bundle("main", source_map),
            "input_json": dumps({} if inputs is None else inputs),
            "options": {
                "heartbeat_interval_ms": 20,
                "heartbeat_timeout_ms": 1000,
                **(options or {}),
            },
        }
        handle = await self.server.client.start_workflow(
            AxcutorInterpreterWorkflow.run, request, id=str(uuid4()), task_queue=self.queue
        )
        self.handles.append(handle)
        return handle

    async def finish(self, handle):
        result = await asyncio.wait_for(handle.result(), 30)
        assert await handle.query(AxcutorInterpreterWorkflow.get_runtime_snapshot) == {"frames": []}
        return loads(result)

    async def execute(self, *args, **kwargs):
        return await self.finish(await self.start(*args, **kwargs))

    async def failed(self, *args, **kwargs):
        handle = await self.start(*args, **kwargs)
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), 30)
        assert await handle.query(AxcutorInterpreterWorkflow.get_runtime_snapshot) == {"frames": []}
        assert isinstance(failure.value.cause, exceptions.ApplicationError)
        return failure.value.cause


@pytest.fixture
def execution(server):
    @asynccontextmanager
    async def run(executors=None, **worker_options):
        queue = str(uuid4())
        state = Execution(server, queue)
        # These lifecycle tests inject a single executor backend with several
        # scripted payloads. Builtin conformance tests use the real default.
        registry = None
        if executors:

            async def dispatch(request, signal):
                return await executors[request["run_spec"]["content"]](request, signal)

            registry = {"builtin": dispatch}
        # Fast application heartbeats above need matching SDK throttling; these
        # lifecycle test values must not become production Worker defaults.
        worker_options.setdefault("max_heartbeat_throttle_interval", timedelta(milliseconds=20))
        async with create_worker(server.client, queue, registry, **worker_options):
            try:
                yield state
            finally:
                for handle in state.handles:
                    await handle.cancel()
        # Every completed history is replayed with the same real sandbox runner.
        replayer = Replayer(
            workflows=[AxcutorInterpreterWorkflow], workflow_runner=workflow_runner()
        )
        for handle in state.handles:
            pending = [await handle.fetch_history()]
            while pending:
                history = pending.pop(0)
                await replayer.replay_workflow(history)
                for event in history.events:
                    if event.event_type == EventType.EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_STARTED:
                        attributes = event.child_workflow_execution_started_event_attributes
                        child = attributes.workflow_execution
                        child_handle = server.client.get_workflow_handle(
                            child.workflow_id, run_id=child.run_id
                        )
                        pending.append(await child_handle.fetch_history())

    return run
