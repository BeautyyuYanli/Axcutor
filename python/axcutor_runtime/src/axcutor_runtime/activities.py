"""Async Python executor ABI. Only this module owns registered business functions.

An attempt owns its business task, local deadline, cancellation watcher and
heartbeat. AbortSignal is process-local and never crosses a Temporal payload.
Executors must cooperate with cancellation; cleanup is awaited even if they
swallow cancellation and return a late result. External side effects are theirs.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy

import axcutor_core
from temporalio import activity, exceptions

from .codec import JsonObject, dumps, loads
from .concurrency import cancel_and_drain


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

    def __init__(self, executors: Mapping[str, Executor]) -> None:
        self._executors = dict(executors)
        for name, function in self._executors.items():
            if not name or not inspect.iscoroutinefunction(function):
                raise TypeError("executors must map nonempty run specs to async Python functions")

    @activity.defn(name="execute_run")
    async def execute_run(self, request_json: str) -> str:
        """Accept/return JSON text; validate before Server completion or retry."""
        try:
            request = loads(request_json)
            timeout = request["attempt_timeout_ms"] / 1000
            interval = request["heartbeat_interval_ms"] / 1000
            if timeout <= 0 or interval <= 0:
                raise ValueError("attempt timeout and heartbeat interval must be positive")
        except Exception as error:
            raise exceptions.ApplicationError(
                str(error), type="EngineError", non_retryable=True
            ) from error

        signal = AbortSignal()

        async def invoke() -> JsonObject:
            function = self._executors.get(request["run_spec"])
            if function is None:
                raise LookupError(f"no executor registered for {request['run_spec']}")
            return await function(deepcopy(request), signal)

        async def heartbeats() -> None:
            while True:
                activity.heartbeat({"attempt": activity.info().attempt})
                await asyncio.sleep(interval)

        business = asyncio.create_task(invoke())
        deadline = asyncio.create_task(asyncio.sleep(timeout))
        cancelled = asyncio.create_task(activity.wait_for_cancelled())
        heartbeat = asyncio.create_task(heartbeats())
        tasks = [business, deadline, cancelled, heartbeat]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if cancelled.done() or activity.is_cancelled():
                signal._abort("cancelled")
                raise asyncio.CancelledError
            if deadline.done():
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
        except asyncio.CancelledError:
            signal._abort("cancelled")
            raise
        finally:
            # All paths drain all owned tasks. Late business results/errors from
            # aborted attempts are collected here and never validated/committed.
            await cancel_and_drain(tasks)
        if activity.is_cancelled():
            signal._abort("cancelled")
        signal.raise_if_aborted()
        return response_json
