"""Async executor dispatch and shared attempt lifecycle outside the Workflow sandbox.

An attempt owns its business task, local deadline, cancellation watcher and
heartbeat. AbortSignal is process-local and never crosses a Temporal payload.
Executors must cooperate with cancellation. A fixed cleanup budget ends remote
waiting, while Store-owned finalizers retain local users until actual drainage.
Late business results cannot renew scheduling authority. Side effects are theirs.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from uuid import uuid4

import axcutor_core
from temporalio import activity, exceptions

from .codec import JsonObject, dumps, loads
from .concurrency import cancel_and_drain
from .resource_model import CLEANUP_SECONDS, scan
from .resources import ResourceContext, ResourceStore, bind_context


async def _bounded(task: asyncio.Task[None], *, expires: float | None = None) -> bool:
    """Repeated cancellation cannot abandon the same fixed cleanup budget."""
    remaining = (
        CLEANUP_SECONDS if expires is None else max(0, expires - asyncio.get_running_loop().time())
    )
    waiter = asyncio.create_task(asyncio.wait([task], timeout=remaining))
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            continue
    waiter.result()
    if task.done():
        task.result()
    return task.done()


class AbortSignal:
    """One attempt's cooperative cancellation token, owned by its Activity."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_aborted(self) -> None:
        if self.aborted:
            raise asyncio.CancelledError(self.reason)

    def _abort(self, reason: str) -> None:
        if not self.aborted:
            self.reason = reason
            self._event.set()


Executor = Callable[[JsonObject, AbortSignal], Awaitable[JsonObject]]


class RunActivities:
    """A worker-local, fixed registry; never imported by the interpreter Workflow."""

    def __init__(
        self,
        executors: Mapping[str, Executor] | None = None,
        *,
        module_mounts: Mapping[str, str] | None = None,
        store: ResourceStore | None = None,
        resource_update: Callable[[str, JsonObject], Awaitable[None]] | None = None,
    ) -> None:
        from .builtin import execute_builtin
        from .module import module_executor

        self.store = store or ResourceStore("local-worker-" + uuid4().hex, module_mounts)
        self._resource_update = resource_update
        self._executors = {
            "builtin": execute_builtin,
            "module": module_executor(module_mounts),
            **(executors or {}),
        }
        for name, function in self._executors.items():
            if not name or not inspect.iscoroutinefunction(function):
                raise TypeError("executors must map executor names to async Python functions")

    @activity.defn(name="execute_run")
    async def execute_run(self, request_json: str) -> str:
        """Accept/return JSON text; validate before Server completion or retry."""
        try:
            request = loads(request_json)
            timeout = request["attempt_timeout_ms"] / 1000
            interval = request["heartbeat_interval_ms"] / 1000
            if timeout <= 0 or interval <= 0:
                raise ValueError("attempt timeout and heartbeat interval must be positive")
            context = ResourceContext(
                self.store, request.get("resource_context", {}).get("resources", [])
            )
            if scan(request["input_snapshot"], request["input_schema"]) != {
                rid: descriptor["kind"] for rid, descriptor in context.allowed.items()
            }:
                raise ValueError("resource authorization must exactly match input handles")
            request["module_mounts"] = self.store.mounts
        except Exception as error:
            raise exceptions.ApplicationError(
                str(error), type="EngineError", non_retryable=True
            ) from error

        signal = AbortSignal()
        loop = asyncio.get_running_loop()
        expires_at = loop.time() + timeout

        async def invoke() -> JsonObject:
            executor = request["run_spec"]["executor"]
            function = self._executors.get(executor)
            if function is None:
                raise LookupError(f"no executor registered for {executor}")
            with bind_context(context):
                try:
                    return await function(deepcopy(request), signal)
                finally:
                    while context.tasks:
                        owned, context.tasks = list(context.tasks), set()
                        await cancel_and_drain(owned)

        async def heartbeats() -> None:
            while True:
                activity.heartbeat({"attempt": activity.info().attempt})
                await asyncio.sleep(interval)

        business = asyncio.create_task(invoke())
        deadline = asyncio.create_task(asyncio.sleep(timeout))
        cancelled = asyncio.create_task(activity.wait_for_cancelled())
        heartbeat = asyncio.create_task(heartbeats())
        tasks = [business, deadline, cancelled, heartbeat]
        exported: set[str] = set()
        failure: BaseException | None = None
        validated_response: JsonObject = {}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if cancelled.done() or activity.is_cancelled():
                signal._abort("cancelled")
                raise asyncio.CancelledError
            if deadline.done() or loop.time() >= expires_at:
                signal._abort("timeout")
                raise exceptions.ApplicationError("attempt deadline exceeded", type="timeout")
            if heartbeat.done():
                # Infrastructure/heartbeat errors cannot become business errors.
                signal._abort("cancelled")
                heartbeat_error = heartbeat.exception()
                raise exceptions.ApplicationError(
                    str(heartbeat_error), type="EngineError", non_retryable=True
                )
            try:
                response = business.result()
            except asyncio.CancelledError:
                raise
            except axcutor_core.ContractError as error:
                raise exceptions.ApplicationError(str(error), type="contract") from error
            except Exception as error:
                raise exceptions.ApplicationError(str(error), type="execution") from error
            signal.raise_if_aborted()
            try:
                raw_response = dumps(response)
            except (ValueError, TypeError, RecursionError, OverflowError) as error:
                raise exceptions.ApplicationError(str(error), type="contract") from error
            try:
                response_json = axcutor_core.validate_run_contract(
                    raw_response,
                    dumps(request["output_schema"]),
                    dumps(request["branches_contract"]),
                )
            except axcutor_core.ContractError as error:
                raise exceptions.ApplicationError(str(error), type="contract") from error
            except Exception as error:
                raise exceptions.ApplicationError(
                    str(error), type="EngineError", non_retryable=True
                ) from error
            try:
                validated_response = loads(response_json)
                exported = context.exported(validated_response["output"], request["output_schema"])
            except Exception as error:
                raise exceptions.ApplicationError(str(error), type="contract") from error
            # Freeze the local outcome after all business/contract checks.
            # Synchronous validation may overrun without resuming the timer.
            # Subsequent cleanup has its own budget; Server's hard timeout A
            # remains unchanged and can still reject a late completion.
            if loop.time() >= expires_at:
                signal._abort("timeout")
                raise exceptions.ApplicationError("attempt deadline exceeded", type="timeout")
        except BaseException as error:
            failure = error
            exported = set()
            if isinstance(error, asyncio.CancelledError):
                signal._abort("cancelled")
        finally:
            cleanup_expires = loop.time() + CLEANUP_SECONDS
            context.closed = True
            for rid in context.allocated_rids - exported:
                self.store.entries[rid].pending = True

            async def settle() -> None:
                await cancel_and_drain(tasks)
                # Descendants inherit the context, including user-created tasks.
                # Keep draining until no new descendants can outlive their owner.
                while context.tasks:
                    owned, context.tasks = list(context.tasks), set()
                    await cancel_and_drain(owned)
                context.drained = True
                if activity.is_cancelled() or signal.aborted:
                    exported.clear()
                await context.close(exported)
                if activity.is_cancelled() and self._resource_update is not None:
                    try:
                        await self._resource_update(
                            activity.info().workflow_run_id or "",
                            {
                                "call_id": request.get("resource_context", {}).get("call_id"),
                                "resources": context.metadata(set()),
                            },
                        )
                    except Exception:
                        activity.logger.warning("resource final update could not be delivered")

            cleanup = asyncio.create_task(settle())
            self.store.retain(cleanup)
            await _bounded(cleanup, expires=cleanup_expires)
        if activity.is_cancelled():
            signal._abort("cancelled")
        if signal.aborted and exported:
            exported.clear()
            for rid in context.allocated_rids:
                entry = self.store.entries.get(rid)
                if entry is not None:
                    entry.pending = True
            if context.drained and loop.time() < cleanup_expires:
                cleanup = asyncio.create_task(self.store.release(context, context.allocated_rids))
                self.store.retain(cleanup)
                await _bounded(cleanup, expires=cleanup_expires)
        metadata = context.metadata(exported)
        metadata["default_queue"] = request.get("workflow_queue")
        if failure is not None:
            if isinstance(failure, exceptions.ApplicationError):
                raise exceptions.ApplicationError(
                    failure.message,
                    dumps({"resources": context.metadata(set()), "drained": context.drained}),
                    type=failure.type,
                    non_retryable=failure.non_retryable,
                ) from failure
            raise failure
        signal.raise_if_aborted()
        return dumps({"result": validated_response, "resources": metadata})

    @activity.defn(name="dispose_resources")
    async def dispose_resources(self, request_json: str) -> str:
        """All items share one budget; pending marks survive Activity cancellation."""
        request = loads(request_json)
        results: list[JsonObject] = []

        async def clean() -> None:
            for descriptor in request["resources"]:
                results.append(
                    await self.store.dispose(
                        descriptor,
                        restore=request.get("restore", False),
                        deleted=request.get("deleted", False),
                    )
                )

        cleanup = asyncio.create_task(clean())
        self.store.retain(cleanup)
        await _bounded(cleanup)
        completed = {item["rid"] for item in results}
        results.extend(
            {"rid": descriptor["rid"], "status": "unconfirmed"}
            for descriptor in request["resources"]
            if descriptor["rid"] not in completed
        )
        return dumps({"results": results})
