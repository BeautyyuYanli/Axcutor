"""Host-side worker assembly; execution options and queue observations are recorded.

Only deterministic native/JSON modules pass through the Workflow sandbox;
business registries live on RunActivities, outside it. Worker kwargs tune SDK
resources, not DSL execution semantics.
"""

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any
from uuid import uuid4

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .activities import Executor, RunActivities, _bounded
from .codec import JsonObject, dumps
from .resource_model import CLEANUP_SECONDS
from .resources import ResourceStore
from .workflow import AxcutorInterpreterWorkflow


def workflow_runner() -> SandboxedWorkflowRunner:
    """Use this same runner for Workers and offline History Replayers."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "axcutor_core", "simplejson"
        )
    )


def create_worker(
    client: Client,
    task_queue: str,
    executors: Mapping[str, Executor] | None = None,
    *,
    module_mounts: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> "ResourceWorker":
    """Register executors and pass resource/throttle kwargs to the SDK unchanged.

    SDK defaults apply unless the host overrides them. Fast cancellation needs
    both a short application heartbeat interval in the execution request and a
    short max_heartbeat_throttle_interval here.
    """
    worker_queue = task_queue + "-worker-" + uuid4().hex
    store = ResourceStore(worker_queue, module_mounts)

    async def update(run_id: str, payload: JsonObject) -> None:
        from temporalio import activity

        info = activity.info()
        if info.workflow_id is not None and run_id:
            handle = client.get_workflow_handle(info.workflow_id, run_id=run_id)
            await handle.signal(
                "resource_update",
                dumps({**payload, "source_run_id": run_id}),
                rpc_timeout=timedelta(seconds=CLEANUP_SECONDS),
            )

    activities = RunActivities(
        executors, module_mounts=module_mounts, store=store, resource_update=update
    )
    primary = Worker(
        client,
        task_queue=task_queue,
        workflows=[AxcutorInterpreterWorkflow],
        activities=[activities.execute_run, activities.dispose_resources],
        workflow_runner=workflow_runner(),
        **kwargs,
    )
    # Workflow-only settings are accepted by an Activity Worker too; no second
    # Workflow poller or isolated instance table is created on the sticky queue.
    sticky = Worker(
        client,
        task_queue=worker_queue,
        activities=[activities.execute_run, activities.dispose_resources],
        **kwargs,
    )
    return ResourceWorker(primary, sticky, store)


class ResourceWorker:
    """Two SDK pollers, one Store; SDK contexts propagate either member's failure."""

    def __init__(self, primary: Worker, sticky: Worker, store: ResourceStore) -> None:
        self.primary, self.sticky, self.store = primary, sticky, store
        self._stack: AsyncExitStack | None = None
        self._stopped = asyncio.Event()
        self._store_close: asyncio.Task[None] | None = None
        self._store_close_expires: float | None = None

    def config(self) -> Any:
        return self.primary.config()

    async def run(self) -> None:
        async with self:
            await self._stopped.wait()

    async def shutdown(self) -> None:
        if self._stack is None:
            self._stopped.set()
            return
        await asyncio.gather(self.primary.shutdown(), self.sticky.shutdown())
        await self._close_store()
        self._stopped.set()

    async def __aenter__(self) -> "ResourceWorker":
        if self._stack is not None:
            raise RuntimeError("Worker is already started")
        self._stack = AsyncExitStack()
        try:
            await self._stack.enter_async_context(self.primary)
            await self._stack.enter_async_context(self.sticky)
        except BaseException:
            await self._stack.aclose()
            await self._close_store()
            raise
        return self

    async def __aexit__(self, *args: Any) -> None:
        assert self._stack is not None
        try:
            # Request both shutdowns together before draining either poller.
            await asyncio.gather(self.primary.shutdown(), self.sticky.shutdown())
            await self._stack.__aexit__(*args)
        finally:
            await self._close_store()

    async def _close_store(self) -> None:
        if self._store_close is None:
            self._store_close = asyncio.create_task(self.store.shutdown())
            self._store_close_expires = asyncio.get_running_loop().time() + CLEANUP_SECONDS
            self.store.retain(self._store_close)
        await _bounded(self._store_close, expires=self._store_close_expires)
