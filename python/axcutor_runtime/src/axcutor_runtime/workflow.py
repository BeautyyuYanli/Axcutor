"""Deterministic interpreter for the compiler's owned, normalized AST bundle.

Frames own private envs; only validated deltas cross a node boundary. Concurrent
members read the same snapshot and commit at their barrier. No business function,
filesystem, clock or process configuration is consulted during execution/replay.
The native kernel and Decimal codec must be passed through the Temporal sandbox.
"""

import asyncio
from collections.abc import Awaitable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import axcutor_core
from temporalio import exceptions, workflow
from temporalio.common import RetryPolicy

from .codec import JsonObject, dumps, loads
from .concurrency import cancel_and_drain
from .errors import (
    EngineError,
    MaxStepsExceededError,
    Recoverable,
    StackOverflowError,
    boundary_error,
)
from .resource_model import (
    CLEANUP_SECONDS,
    STICKY_SECONDS,
    RegistryEntry,
    ResourceRegistry,
    description,
    has_handles,
    pool,
    scan,
)


def parse_runtime_options(options: JsonObject) -> JsonObject:
    """Validate and copy overrides, then fill deterministic execution defaults.

    Hosts use the same ingress rules but persist only their original overrides.
    Child requests carry the expanded result, so explicit values must survive
    repeated parsing. Backend duration limits are checked only when consumed.
    """
    result: JsonObject = {
        "activity_timeout_ms": 7 * 24 * 60 * 60 * 1000,
        "heartbeat_timeout_ms": 30000,
        "max_call_depth": 64,
        "task_queues": {},
        "child_workflow_history_events": None,
        "record_trace": False,
    }
    if not isinstance(options, dict) or options.keys() - (
        result.keys() | {"heartbeat_interval_ms"}
    ):
        raise ValueError("unknown runtime options")
    result.update(deepcopy(options))
    if type(result["record_trace"]) is not bool:
        raise ValueError("record_trace must be a boolean")
    for name in (
        "activity_timeout_ms",
        "heartbeat_interval_ms",
        "heartbeat_timeout_ms",
        "max_call_depth",
    ):
        if name in result and (type(result[name]) is not int or result[name] <= 0):
            raise ValueError(f"{name} must be a positive integer")
    # Only absence derives an interval; explicit None is rejected above.
    result.setdefault("heartbeat_interval_ms", max(1, result["heartbeat_timeout_ms"] // 3))
    if result["heartbeat_interval_ms"] >= result["heartbeat_timeout_ms"]:
        raise ValueError("heartbeat interval must be less than heartbeat timeout")
    queues = result["task_queues"]
    if not isinstance(queues, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v for k, v in queues.items()
    ):
        raise ValueError("task_queues must map executor names to nonempty task queues")
    threshold = result["child_workflow_history_events"]
    if threshold is not None and (type(threshold) is not int or threshold <= 0):
        raise ValueError("child_workflow_history_events must be a positive integer")
    return result


@dataclass
class Frame:
    """One activation. Only its execution coroutine mutates env/cursor.

    Fork/iteration activations share the owning graph's step counter; increments
    happen synchronously when a concurrency permit is acquired, before dispatch.
    Diagnostic IDs are execution-local and allocated in deterministic order.
    """

    id: int
    parent_id: int | None
    kind: str
    address: str
    call_depth: int
    env: JsonObject
    symbols: JsonObject
    max_steps: int | None = None
    steps: int = 0
    cursor: str = "start"
    child_execution: JsonObject | None = None


@dataclass
class _SignalWaiter:
    """One attempt owned by its waiting coroutine; Updates only claim it once.

    Validators read live frame steps without mutating either object. Delivery
    removes the address synchronously, before the execution coroutine resumes.
    """

    frame: Frame
    address: str
    output_schema_json: str
    branches_contract_json: str
    status: Literal["pending", "delivered", "expired"] = "pending"
    response: JsonObject | None = None


def _path_type(symbols: JsonObject, path: JsonObject) -> JsonObject:
    """Follow compiler-proved collection projections, without rechecking types."""
    result = symbols[path["root"]]
    for field in path["fields"]:
        result = result["properties"][field]
    return result


def _read_names(body: JsonObject) -> list[str]:
    if body["kind"] in ("run", "graph"):
        return list(body["input"])
    return list(
        dict.fromkeys(
            arg["path"]["root"]
            for arg in body["bindings"]["input"].values()
            if arg["kind"] == "path"
        )
    )


def _out_names(body: JsonObject) -> list[str]:
    if body["kind"] in ("run", "graph"):
        return list(body["output"])
    return list(body["bindings"]["output"].values())


def _snapshot(body: JsonObject, env: JsonObject) -> JsonObject:
    return deepcopy({name: env[name] for name in _read_names(body)})


def _contract(response: JsonObject | str, body: JsonObject) -> JsonObject:
    try:
        return loads(
            axcutor_core.validate_run_contract(
                response if isinstance(response, str) else dumps(response),
                dumps(body["output"]),
                dumps(body["branches"]),
            )
        )
    except axcutor_core.ContractError as error:
        raise Recoverable("contract", str(error)) from error


def _fatal_first(errors: Sequence[BaseException]) -> BaseException:
    """A simultaneous engine panic must not be hidden by a business failure."""
    for error in errors:
        if not isinstance(error, (Recoverable, asyncio.CancelledError, exceptions.CancelledError)):
            return error
    return errors[0]


async def _owned_group(coroutines: Sequence[Awaitable[Any]]) -> list[Any]:
    if not coroutines:
        return []
    tasks = [asyncio.ensure_future(coro) for coro in coroutines]
    try:
        await workflow.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        errors = [task.exception() for task in tasks if task.done() and not task.cancelled()]
        failures = [error for error in errors if error is not None]
        if failures:
            raise _fatal_first(failures)
        return [await task for task in tasks]
    except BaseException as original:
        results = await cancel_and_drain(tasks)
        if not isinstance(original, (asyncio.CancelledError, exceptions.CancelledError)):
            cleanup_errors = [r for r in results if isinstance(r, BaseException)]
            if cleanup_errors:
                raise _fatal_first([original, *cleanup_errors]) from None
        raise


async def _with_timeout(
    operation: Awaitable[JsonObject], timeout_ms: int, *, signal: bool = False
) -> JsonObject:
    """Own the deadline and child together, retaining the timeout selector.

    SDK cancellation can raise temporalio.CancelledError rather than asyncio's
    CancelledError, so asyncio.wait_for cannot reliably identify its own timer.
    Explicitly racing a Workflow timer also waits for descendant cleanup.
    A signal already claimed synchronously by an Update retains its result even
    if this timer wakes in the same turn. Other operations discard late results.
    """
    task = asyncio.ensure_future(operation)
    timer = asyncio.create_task(workflow.sleep(timedelta(milliseconds=timeout_ms)))
    try:
        await workflow.wait([task, timer], return_when=asyncio.FIRST_COMPLETED)
        if timer.done():
            raise Recoverable("timeout", "composite attempt timed out")
        result = await task
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        return result
    except BaseException as original:
        signal_deadline = (
            signal and isinstance(original, Recoverable) and original.selector == "timeout"
        )
        results = await cancel_and_drain(
            [task, timer], message="signal attempt timeout" if signal_deadline else None
        )
        if signal_deadline and not isinstance(results[0], BaseException):
            return results[0]
        if not isinstance(original, (asyncio.CancelledError, exceptions.CancelledError)):
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise _fatal_first([original, *errors]) from None
        raise


@workflow.defn
class AxcutorInterpreterWorkflow:
    """One read-only bundle plus a tree of active frames, reconstructed by replay.

    The run coroutine owns the optional, shared signal initialization timer.
    Attempts only own their waiters; delivering one never waits for that timer.
    """

    def __init__(self) -> None:
        self._frames: dict[int, Frame] = {}
        self._next_frame = 0
        self._bundle: JsonObject = {}
        self._bundle_json = ""
        self._options: JsonObject = {}
        self._waiters: dict[str, _SignalWaiter] = {}
        self._signal_initialized = False
        self._signal_init_timer: asyncio.Task[None] | None = None
        self._trace: list[JsonObject] = []
        self._trace_parent: ContextVar[int | None] = ContextVar(
            "axcutor_trace_parent", default=None
        )
        # Tasks inherit their enclosing deadline, independently of sibling tasks.
        self._deadline_scope: ContextVar[bool] = ContextVar("axcutor_deadline", default=False)
        self._registry = ResourceRegistry()
        self._resource_roots: dict[int, tuple[JsonObject, JsonObject]] = {}
        self._root_scope: ContextVar[set[int] | None] = ContextVar(
            "axcutor_root_scope", default=None
        )
        self._next_resource_root = 0
        self._next_call = 0
        self._resource_calls: dict[str, JsonObject] = {}
        self._default_queue: str | None = None

    def _hold(self, values: JsonObject, symbols: JsonObject) -> int:
        self._next_resource_root += 1
        token = self._next_resource_root
        self._resource_roots[token] = values, symbols
        scope = self._root_scope.get()
        if scope is not None:
            scope.add(token)
        return token

    def _retain_result(self, result: JsonObject, schema: JsonObject) -> JsonObject:
        if "_resource_root" not in result and scan(result["output"], schema):
            result["_resource_root"] = self._hold(result["output"], schema)
        return result

    def _release_result(self, result: JsonObject) -> None:
        self._resource_roots.pop(result.pop("_resource_root", None), None)

    def _output_types(self, body: JsonObject, symbols: JsonObject) -> JsonObject:
        if body["kind"] in ("run", "graph"):
            return body["output"]
        if body["kind"] == "workflow":
            target = self._bundle["workflows"][body["workflow_id"]]["output"]
            return {caller: target[name] for name, caller in body["bindings"]["output"].items()}
        return {name: symbols[name] for name in _out_names(body)}

    async def _collect_resources(
        self,
        *,
        final: bool = False,
        exported: set[str] | None = None,
        expires: float | None = None,
    ) -> None:
        """Synchronously scan real execution roots, then schedule bounded cleanup."""
        reachable: set[str] = set()
        if not final:
            for frame in self._frames.values():
                if frame.kind != "iteration":
                    reachable.update(scan(frame.env, frame.symbols))
            for values, symbols in self._resource_roots.values():
                reachable.update(scan(values, symbols))
        for rid, entry in self._registry.entries.items():
            if entry.owned and rid not in reachable and rid not in (exported or ()):
                entry.pending = True
        pending = [
            (rid, entry)
            for rid, entry in self._registry.entries.items()
            if entry.owned and entry.pending and not entry.cleaning
        ]
        if not pending:
            return
        for _, entry in pending:
            entry.cleaning = True
        expires = workflow.time() + CLEANUP_SECONDS if expires is None else expires

        async def clean() -> None:
            try:
                for rid, entry in pending:
                    d = entry.descriptor
                    for queue in list(d["resident_queues"]):
                        if queue in entry.cleaned or workflow.time() >= expires:
                            continue
                        status = await self._dispose_resource(
                            d, queue, expires, deleted=entry.deleted
                        )
                        if status == "disposed":
                            entry.cleaned.add(queue)
                            if d["persistent"]:
                                entry.deleted = True
                        elif status == "missing" and queue not in entry.failed:
                            entry.cleaned.add(queue)
                        else:
                            entry.failed.add(queue)
                    if d["persistent"] and not entry.deleted and workflow.time() < expires:
                        status = await self._dispose_resource(
                            d, pool(d["creator_queue"]), expires, restore=True
                        )
                        entry.deleted = status == "disposed"
                    if set(d["resident_queues"]) <= entry.cleaned and (
                        not d["persistent"] or entry.deleted
                    ):
                        if entry.deleted:
                            for call in self._resource_calls.values():
                                if rid in call["owned"]:
                                    call["deleted"].add(rid)
                        self._registry.entries.pop(rid, None)
                    elif final:
                        workflow.logger.warning(
                            "resource cleanup unconfirmed rid=%s kind=%s", rid, d["kind"]
                        )
            finally:
                for _, entry in pending:
                    entry.cleaning = False

        task = asyncio.create_task(clean())
        # Shield the same round through repeated cancellation. Each scheduled
        # Activity is bounded by the remaining round budget, never a fresh 30s.
        while not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, exceptions.CancelledError):
                continue
        task.result()

    async def _dispose_resource(
        self,
        descriptor: JsonObject,
        queue: str,
        expires: float,
        *,
        restore: bool = False,
        deleted: bool = False,
    ) -> str:
        remaining = expires - workflow.time()
        if remaining <= 0:
            return "unconfirmed"
        try:
            response = loads(
                await workflow.execute_activity(
                    "dispose_resources",
                    dumps({"resources": [descriptor], "restore": restore, "deleted": deleted}),
                    result_type=str,
                    task_queue=queue,
                    schedule_to_start_timeout=timedelta(seconds=min(STICKY_SECONDS, remaining)),
                    start_to_close_timeout=timedelta(seconds=remaining),
                    schedule_to_close_timeout=timedelta(seconds=remaining),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL,
                )
            )
            (item,) = response["results"]
            if item["rid"] != descriptor["rid"] or item["status"] not in {
                "disposed",
                "missing",
                "unconfirmed",
            }:
                raise EngineError("invalid resource cleanup response")
            return item["status"]
        except exceptions.FailureError:
            return "unconfirmed"

    def _resource_call(self, allowed: list[JsonObject], source_run_id: str) -> str:
        self._next_call += 1
        call_id = f"resource-call-{self._next_call}"
        self._resource_calls[call_id] = {
            "allowed": allowed,
            "owned": {d["rid"]: self._registry.entries[d["rid"]].owned for d in allowed},
            "source_run_id": source_run_id,
            "drained": False,
            "reported": {(d["rid"], q) for d in allowed for q in d["resident_queues"]},
            "deleted": set(),
        }
        return call_id

    def _remember_residencies(self, call_id: str) -> None:
        call = self._resource_calls[call_id]
        for d in call["allowed"]:
            entry = self._registry.entries.get(d["rid"])
            if entry is not None:
                call["reported"].update(
                    (d["rid"], queue) for queue in entry.descriptor["resident_queues"]
                )

    @workflow.signal
    async def resource_update(self, request_json: str) -> None:
        """Merge one exact-call final residency update; it never reactivates output."""
        try:
            request = loads(request_json)
            call = self._resource_calls.get(request["call_id"])
            if call is None or call["source_run_id"] != request["source_run_id"]:
                return
            # Validate before mutating live state. Repeated cumulative updates
            # add no work; a new late residency only restores cleanup authority.
            probe = ResourceRegistry()
            for d in call["allowed"]:
                probe.entries[d["rid"]] = RegistryEntry(description(d), False)
            probe.accept(request["resources"], call["allowed"], None)
            changed = False
            for rid, received in probe.entries.items():
                for queue in received.descriptor["resident_queues"]:
                    pair = rid, queue
                    if pair in call["reported"]:
                        continue
                    entry = self._registry.entries.get(rid)
                    if entry is None:
                        original = next(d for d in call["allowed"] if d["rid"] == rid)
                        entry = RegistryEntry(
                            description(original),
                            call["owned"][rid],
                            pending=True,
                            deleted=rid in call["deleted"],
                            cleaned=set(original["resident_queues"]),
                        )
                        self._registry.entries[rid] = entry
                    if queue not in entry.descriptor["resident_queues"]:
                        entry.descriptor["resident_queues"].append(queue)
                    call["reported"].add(pair)
                    changed = True
            call["drained"] = True
        except (KeyError, ValueError, TypeError):
            workflow.logger.warning("invalid resource final update")
            return
        if changed:
            await self._collect_resources()

    def _merge_failure(self, error: BaseException, call_id: str) -> bool:
        cause = error
        while isinstance(cause, (exceptions.ActivityError, exceptions.ChildWorkflowError)):
            if cause.cause is None:
                return False
            cause = cause.cause
        if isinstance(cause, exceptions.ApplicationError) and cause.details:
            try:
                details = loads(cause.details[0])
                call = self._resource_calls[call_id]
                self._registry.accept(details["resources"], call["allowed"], None)
                self._remember_residencies(call_id)
                call["drained"] = details["drained"] is True
                return call["drained"]
            except (TypeError, ValueError, KeyError):
                raise EngineError("invalid resource failure details") from error
        return False

    async def _settle_resource_call(
        self, handle: asyncio.Future[Any], call_id: str, body: JsonObject
    ) -> None:
        """End scheduling authority after a fixed wait; local uses stay with Store."""
        call = self._resource_calls[call_id]

        def receive_completed() -> None:
            if handle.cancelled():
                return
            try:
                response = handle.result()
            except exceptions.FailureError as error:
                self._merge_failure(error, call_id)
            except asyncio.CancelledError:
                pass
            else:
                # Cancellation can race a response already delivered by Temporal.
                # Accept known ownership for cleanup, never for business commit.
                result, _ = self._receive_response(response, body, call["allowed"])
                self._remember_residencies(call_id)
                self._release_result(result)
                call["drained"] = True

        if handle.done():
            receive_completed()
        if call["drained"]:
            return
        handle.cancel()

        async def observe() -> None:
            try:
                await asyncio.shield(handle)
            except (asyncio.CancelledError, exceptions.FailureError):
                pass
            if handle.done():
                receive_completed()

        observer = asyncio.create_task(observe())

        async def wait() -> None:
            try:
                await workflow.wait_condition(lambda: call["drained"], timeout=CLEANUP_SECONDS)
            except TimeoutError:
                workflow.logger.warning("resource call drainage unconfirmed call=%s", call_id)

        task = asyncio.create_task(wait())
        while not task.done():
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, exceptions.CancelledError):
                continue
        task.result()
        results = await cancel_and_drain([observer])
        if isinstance(results[0], EngineError):
            raise results[0]

    def _receive_response(
        self, response: str, body: JsonObject, allowed: list[JsonObject]
    ) -> tuple[JsonObject, str | None]:
        try:
            envelope = loads(response)
            if not isinstance(envelope, dict) or envelope.keys() != {"result", "resources"}:
                raise ValueError("Activity and Child responses require result/resources")
            result = _contract(envelope["result"], body)
            self._registry.accept(
                envelope["resources"], allowed, scan(result["output"], body["output"])
            )
            default = envelope["resources"].get("default_queue")
            if default is not None:
                if not isinstance(default, str) or not default:
                    raise ValueError("invalid default resource queue")
                # Offline SDK Replayers replace info().task_queue. Record the
                # original default in Activity results for later routing checks.
                self._default_queue = default
            return (
                self._retain_result(result, body["output"]),
                envelope["resources"]["worker_queue"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise Recoverable("contract", str(error)) from error

    @workflow.run
    async def run(self, request: JsonObject) -> str:
        """Run {bundle_json,input_json,options?}; return {branch,output} JSON text.

        _entry and _call_depth are reserved for interpreter Child Workflows.
        Root ingress failures are terminal and cannot enter a DSL node catch.
        """
        parent = workflow.info().parent
        context = request.get("resource_context", {})
        cancelled = False
        evicted = False
        result: JsonObject | None = None
        failure: BaseException | None = None
        exported: set[str] = set()
        try:
            self._bundle_json = request["bundle_json"]
            self._bundle = loads(self._bundle_json)
            self._options = parse_runtime_options(request.get("options", {}))
            depth = request.get("_call_depth", 1)
            if type(depth) is not int or depth < 1:
                raise ValueError("invalid call depth")
            entry = request.get("_entry", self._bundle["entry"])
            if not isinstance(entry, str):
                raise ValueError("entry must be a string")
            root = self._bundle["workflows"][entry]
            if parent is None:
                axcutor_core.validate_root_interface(dumps(root["input"]), dumps(root["output"]))
            else:
                if context.get("parent_run_id") != parent.run_id:
                    raise ValueError("Child resource context must match the real parent Run")
            values = loads(axcutor_core.validate_input(request["input_json"], dumps(root["input"])))
            if parent is not None:
                self._registry.borrow(context.get("resources", []), values, root["input"])
                self._default_queue = context.get("default_queue")
            with self._trace_call("workflow", entry, values) as span:
                if span is not None:
                    span["frame_kind"] = "function"
                    span["cursor"] = "run" if root["body"]["kind"] == "run" else "start"
                result = await self._exec_root(entry, values, depth, None, entry)
                self._trace_result(span, result)
                if parent is not None:
                    exported = set(scan(result["output"], root["output"]))
        except GeneratorExit:
            evicted = True
            raise
        except (asyncio.CancelledError, exceptions.CancelledError) as error:
            cancelled = True
            failure = error
        except Exception as error:
            failure = error
        finally:
            if not evicted:
                expires = workflow.time() + CLEANUP_SECONDS
                await self._collect_resources(final=True, exported=exported, expires=expires)
                if not workflow.all_handlers_finished():
                    handlers = asyncio.create_task(
                        workflow.wait_condition(
                            workflow.all_handlers_finished,
                            timeout=max(0, expires - workflow.time()),
                        )
                    )
                    while not handlers.done():
                        try:
                            await asyncio.shield(handlers)
                        except (asyncio.CancelledError, exceptions.CancelledError):
                            continue
                    try:
                        handlers.result()
                    except TimeoutError:
                        workflow.logger.warning(
                            "resource final handlers did not drain within budget"
                        )
                    await self._collect_resources(final=True, exported=exported, expires=expires)
                if workflow.cancellation_reason() is not None:
                    cancelled = True
                    failure = asyncio.CancelledError()
                    await self._collect_resources(final=True, expires=expires)
            if cancelled and parent is not None:
                try:
                    await workflow.get_external_workflow_handle(
                        parent.workflow_id, run_id=parent.run_id
                    ).signal(
                        "resource_update",
                        dumps(
                            {
                                "source_run_id": workflow.info().run_id,
                                "call_id": request.get("resource_context", {}).get("call_id"),
                                "resources": self._registry.metadata(set()),
                            }
                        ),
                    )
                except exceptions.FailureError:
                    workflow.logger.warning("Child resource final update could not be delivered")
            if not evicted and self._signal_init_timer is not None:
                # This task only owns a Workflow sleep. Propagate cancellation
                # of the run while gathering its cancellation/completion.
                self._signal_init_timer.cancel()
                await asyncio.gather(self._signal_init_timer, return_exceptions=True)
        if failure is not None:
            if isinstance(failure, (asyncio.CancelledError, exceptions.CancelledError)):
                raise failure
            selector = (
                failure.selector
                if isinstance(failure, Recoverable)
                else type(failure).__name__
                if isinstance(failure, EngineError)
                else "EngineError"
            )
            # Build the cumulative residency handoff after final cleanup and
            # its possible late updates, while preserving the original selector.
            raise exceptions.ApplicationError(
                str(failure),
                dumps({"resources": self._registry.metadata(set()), "drained": True}),
                type=selector,
                non_retryable=True,
            ) from failure
        assert result is not None
        self._release_result(result)
        if parent is not None:
            metadata = self._registry.metadata(exported)
            metadata["default_queue"] = self._default_queue
            for rid in exported:
                self._registry.entries[rid].owned = False
            return dumps({"result": result, "resources": metadata})
        return dumps(result)

    @contextmanager
    def _trace_call(
        self, kind: str, address: str, values: JsonObject
    ) -> Iterator[JsonObject | None]:
        """Optional cumulative observations, reconstructed using Workflow time.

        IDs identify invocations, never addresses. Context is inherited by child
        tasks, so parallel siblings keep their real parent without serialization.
        Payloads stay JSON text across the SDK's ordinary query converter.
        No commands, awaits or clocks are added when tracing is disabled.
        """
        if not self._options.get("record_trace"):
            yield None
            return
        span: JsonObject = {
            "id": len(self._trace) + 1,
            "parent_id": self._trace_parent.get(),
            "kind": kind,
            "address": address,
            "frame_kind": "iteration",
            "cursor": "",
            "started_at": workflow.now().isoformat(),
            "finished_at": None,
            "status": "running",
            "input_json": dumps(values),
            "output_json": None,
            "branch": None,
            "error": None,
            "events": [],
        }
        self._trace.append(span)
        token = self._trace_parent.set(span["id"])
        evicted = False
        try:
            yield span
        except GeneratorExit:
            # SDK cache eviction may collect suspended coroutines outside the
            # Workflow loop. It is not an execution event or a finish time.
            evicted = True
            raise
        except (asyncio.CancelledError, exceptions.CancelledError):
            span["status"] = "cancelled"
            raise
        except BaseException as error:
            span["status"] = "failed"
            span["error"] = {
                "type": error.selector if isinstance(error, Recoverable) else type(error).__name__,
                "message": str(error),
            }
            raise
        else:
            span["status"] = "completed"
        finally:
            if not evicted:
                span["finished_at"] = workflow.now().isoformat()
                self._trace_parent.reset(token)

    @staticmethod
    def _trace_result(span: JsonObject | None, result: JsonObject) -> None:
        if span is not None:
            span["output_json"] = dumps(result["output"])
            span["branch"] = result["branch"]

    def _trace_current(self) -> JsonObject | None:
        parent = self._trace_parent.get()
        return self._trace[parent - 1] if parent is not None else None

    def _trace_event(self, kind: str, **details: Any) -> None:
        span = self._trace_current()
        if span is not None:
            span["events"].append({"kind": kind, "at": workflow.now().isoformat(), **details})

    def _trace_input(self, values: JsonObject) -> None:
        span = self._trace_current()
        if span is not None:
            span["input_json"] = dumps(values)
            if span["kind"] == "attempt" and span["parent_id"] is not None:
                self._trace[span["parent_id"] - 1]["input_json"] = span["input_json"]

    @workflow.query
    def get_execution_trace(self) -> JsonObject:
        """Cumulative calls, including closed/failed/cancelled calls and child links.

        This optional query needs a Worker, including after closure. It never
        changes the normal execution result or active-frame snapshot contract.
        """
        return {"spans": deepcopy(self._trace), "observed_at": workflow.now().isoformat()}

    @workflow.query
    def get_runtime_snapshot(self) -> JsonObject:
        """Independent active-frame tree; envs remain exact JSON text.

        Offloaded calls include a private child_execution link for host-side
        live queries. Queries issue no Workflow commands or network operations.
        """
        nodes: dict[int, JsonObject] = {
            frame.id: {
                "id": frame.id,
                "parent_id": frame.parent_id,
                "kind": frame.kind,
                "address": frame.address,
                "call_depth": frame.call_depth,
                "cursor": frame.cursor,
                "waiting": [
                    w.address
                    for w in self._waiters.values()
                    if w.frame.id == frame.id and w.status == "pending"
                ],
                "steps": frame.steps,
                "max_steps": frame.max_steps,
                "env_json": dumps(frame.env),
                "children": [],
                **(
                    {"child_execution": dict(frame.child_execution)}
                    if frame.child_execution
                    else {}
                ),
            }
            for frame in self._frames.values()
        }
        roots = []
        for frame in self._frames.values():
            if frame.parent_id in nodes:
                nodes[frame.parent_id]["children"].append(nodes[frame.id])
            else:
                roots.append(nodes[frame.id])
        return {"frames": roots}

    def _validate_signal_request(self, request_json: str) -> tuple[_SignalWaiter, str]:
        """Reject invalid Updates without state changes or accepted history events."""
        try:
            request = loads(request_json)
            if (
                not isinstance(request, dict)
                or not {"address", "output"} <= request.keys()
                or request.keys() - {"address", "branch", "output", "step"}
                or not isinstance(request["address"], str)
                or not request["address"]
                or not isinstance(request["output"], dict)
                or (
                    request.get("branch") is not None
                    and (not isinstance(request["branch"], str) or not request["branch"])
                )
                or (
                    request.get("step") is not None
                    and (type(request["step"]) is not int or request["step"] < 1)
                )
            ):
                raise ValueError("invalid signal request")
        except (ValueError, TypeError, RecursionError, OverflowError) as error:
            raise exceptions.ApplicationError(
                str(error), type="SignalValidationError", non_retryable=True
            ) from error
        address = request["address"]
        waiter = self._waiters.get(address)
        if waiter is None or waiter.status != "pending":
            raise exceptions.ApplicationError(
                f"no signal node waiting at {address!r}",
                type="SignalNotWaitingError",
                non_retryable=True,
            )
        step = request.get("step")
        if step is not None and waiter.frame.steps != step:
            raise exceptions.ApplicationError(
                f"signal step mismatch at {address!r}: "
                f"expected {step}, current {waiter.frame.steps}",
                type="SignalNotWaitingError",
                non_retryable=True,
            )
        try:
            if has_handles(loads(waiter.output_schema_json)):
                raise ValueError("signal output must contain only pure values")
            validated = axcutor_core.validate_run_contract(
                dumps({"branch": request.get("branch"), "output": request["output"]}),
                waiter.output_schema_json,
                waiter.branches_contract_json,
            )
        except (
            axcutor_core.ContractError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
        ) as e:
            raise exceptions.ApplicationError(
                str(e), type="SignalValidationError", non_retryable=True
            ) from e
        except RuntimeError as error:
            raise exceptions.ApplicationError(
                str(error), type="EngineError", non_retryable=True
            ) from error
        return waiter, validated

    @workflow.update
    def deliver_signal(self, request_json: str) -> str:
        """Atomically claim a validated waiter, returning lossless response JSON."""
        # Revalidate on replay; validator memory must never carry execution state.
        waiter, validated = self._validate_signal_request(request_json)
        waiter.response = loads(validated)
        waiter.status = "delivered"
        del self._waiters[waiter.address]
        self._signal_initialized = True  # This accepted Update is itself durable.
        return validated

    @deliver_signal.validator
    def validate_deliver_signal(self, request_json: str) -> None:
        self._validate_signal_request(request_json)

    async def _wait_signal(self, frame: Frame, node: JsonObject, address: str) -> JsonObject:
        body = node["body"]
        waiter = _SignalWaiter(frame, address, dumps(body["output"]), dumps(body["branches"]))
        self._waiters[address] = waiter
        self._trace_event("signal_wait", address=address, step=frame.steps)
        try:
            try:
                if not self._signal_initialized and not self._deadline_scope.get():
                    # SDK issue #2080: a command-less first task can validate a
                    # cold Update before replay rebuilds the waiter. One shared
                    # command suffices; its firing time must not gate delivery.
                    self._signal_initialized = True
                    self._signal_init_timer = asyncio.create_task(
                        workflow.sleep(timedelta(milliseconds=1))
                    )
                await workflow.wait_condition(lambda: waiter.status == "delivered")
            except (asyncio.CancelledError, exceptions.CancelledError) as error:
                if waiter.status != "delivered" or error.args != ("signal attempt timeout",):
                    raise
                # Only our own deadline may retain a claim. External/fail-fast
                # cancellation must unwind even for an already delivered waiter.
            # The SDK may batch a claim and a server cancel before injecting
            # task cancellation. Enforce the same outcome during live execution
            # and replay.
            if workflow.cancellation_reason() is not None:
                raise asyncio.CancelledError
            assert waiter.response is not None
            self._trace_event("signal_delivered", address=address)
            return waiter.response
        finally:
            if waiter.status == "pending":
                waiter.status = "expired"
            if self._waiters.get(address) is waiter:
                del self._waiters[address]

    def _frame(
        self,
        kind: str,
        address: str,
        depth: int,
        parent: int | None,
        env: JsonObject,
        symbols: JsonObject,
        max_steps: int | None = None,
    ) -> Frame:
        """Own one deep copy of this activation's assembled input/default view."""
        if depth > self._options["max_call_depth"]:
            raise StackOverflowError("maximum call depth exceeded")
        self._next_frame += 1
        frame = Frame(
            self._next_frame, parent, kind, address, depth, deepcopy(env), symbols, max_steps
        )
        self._frames[frame.id] = frame
        return frame

    async def _exec_root(
        self, entry: str, values: JsonObject, depth: int, parent: int | None, address: str
    ) -> JsonObject:
        root = self._bundle["workflows"][entry]
        body = root["body"]
        if body["kind"] == "graph":
            return await self._exec_graph(
                body["graph"], root, values, root["input"], depth, parent, address, "function"
            )
        frame = self._frame("function", address, depth, parent, values, root["input"])
        frame.cursor = "run"
        node = {
            "body": {
                **body,
                "input": list(root["input"]),
                "output": root["output"],
                "branches": root["branches"],
            },
            "modifiers": {
                "timeout_ms": body["timeout_ms"],
                "retry": body["retry"],
                "catch": {},
                "for_each": None,
            },
        }
        evicted = False
        try:
            return await self._state_machine(frame, node, values, frame.symbols, {}, address)
        except GeneratorExit:
            evicted = True
            raise
        finally:
            del self._frames[frame.id]
            if not evicted:
                await self._collect_resources()

    async def _exec_graph(
        self,
        graph: JsonObject,
        host: JsonObject,
        values: JsonObject,
        input_types: JsonObject,
        depth: int,
        parent: int | None,
        address: str,
        kind: str,
    ) -> JsonObject:
        symbols = {**input_types, **host["output"]}
        env = dict(values)
        for name, declaration in graph["variables"].items():
            symbols[name] = declaration["typespec"]
            if declaration["initial"]["kind"] == "default":
                env[name] = declaration["initial"]["value"]
        for node in graph["nodes"].values():
            if node["modifiers"]["for_each"] is None and node["body"]["kind"] in ("run", "graph"):
                symbols.update(node["body"]["output"])
        frame = self._frame(kind, address, depth, parent, env, symbols, graph["max_steps"])
        evicted = False
        try:
            while True:
                route = graph["routes"][frame.cursor]
                for name in route["kill_vars"]:
                    frame.env.pop(name, None)
                await self._collect_resources()
                edge = route["edge"]
                if edge["kind"] == "fork_join":
                    locked = deepcopy(frame.env)
                    slots = await _owned_group(
                        [self._activate(frame, graph, worker, locked) for worker in edge["workers"]]
                    )
                    delta: JsonObject = {}
                    for slot in slots:
                        delta.update(slot["output"])
                    frame.env.update(delta)
                    for slot in slots:
                        self._release_result(slot)
                    await self._collect_resources()
                    target = edge["join_target"]
                else:
                    if edge["target"]["kind"] == "fail":
                        raise Recoverable("execution", "graph reached fail")
                    target = edge["target"]["target"]
                if target["kind"] == "end":
                    return self._retain_result(
                        _contract({"branch": target["branch_id"], "output": frame.env}, host),
                        host["output"],
                    )
                node_id = target["node_id"]
                frame.cursor = node_id
                result = await self._activate(frame, graph, node_id, frame.env)
                frame.env.update(result["output"])
                self._release_result(result)
                await self._collect_resources()
                frame.cursor = node_id + (
                    "." + result["branch"] if result["branch"] is not None else ""
                )
        except GeneratorExit:
            evicted = True
            raise
        finally:
            del self._frames[frame.id]
            if not evicted:
                await self._collect_resources()

    @staticmethod
    def _tick(frame: Frame) -> None:
        frame.steps += 1
        if frame.max_steps is not None and frame.steps > frame.max_steps:
            if frame.call_depth == 1:
                raise MaxStepsExceededError("root max_steps exceeded")
            raise Recoverable("max_steps", "subgraph max_steps exceeded")

    async def _activate(
        self, frame: Frame, graph: JsonObject, node_id: str, env: JsonObject
    ) -> JsonObject:
        node = graph["nodes"][node_id]
        address = f"{frame.address}/{node_id}"
        if node["modifiers"]["for_each"] is not None:
            values = {}
            if self._options["record_trace"]:
                # Collection plus captured parent inputs; per-item injections
                # belong to the individual calls after acquiring a permit.
                each = node["modifiers"]["for_each"]
                names = dict.fromkeys([each["source"]["root"], *_read_names(node["body"])])
                values = {name: env[name] for name in names if name in env}
            with self._trace_call("for_each", address, values) as span:
                result = await self._for_each(frame, node, env, address, node_id)
                self._trace_result(span, result)
                return result
        self._tick(frame)
        return await self._state_machine(
            frame, node, _snapshot(node["body"], env), frame.symbols, {}, address
        )

    async def _for_each(
        self, frame: Frame, node: JsonObject, env: JsonObject, address: str, node_id: str
    ) -> JsonObject:
        each = node["modifiers"]["for_each"]
        source = loads(
            axcutor_core.eval_arg(dumps({"kind": "path", "path": each["source"]}), dumps(env))
        )
        mapping = isinstance(source, dict)
        entries = list(source.items()) if mapping else list(enumerate(source))
        if not entries:
            return {
                "branch": None,
                "output": {name: {} if mapping else [] for name in _out_names(node["body"])},
            }
        source_type = _path_type(frame.symbols, each["source"])
        item_type = source_type["additionalProperties" if mapping else "items"]
        injected_types = {each["item"]: item_type}
        if each["key"] is not None:
            injected_types[each["key"]] = {"type": "string"}
        if each["index"] is not None:
            injected_types[each["index"]] = {"type": "integer"}
        symbols = {**frame.symbols, **injected_types}
        semaphore = asyncio.Semaphore(each["concurrency"])
        # Freeze once; retry and every iteration start from this activation's view.
        locked = deepcopy(env)
        stopped = False

        async def iteration(index: int, key: Any, value: Any) -> JsonObject:
            nonlocal stopped
            async with semaphore:
                if stopped:
                    raise asyncio.CancelledError
                try:
                    self._tick(frame)
                    injected = {each["item"]: value}
                    if each["key"] is not None:
                        injected[each["key"]] = key
                    if each["index"] is not None:
                        injected[each["index"]] = index
                    snapshot = _snapshot(node["body"], {**locked, **injected})
                    diagnostic = self._frame(
                        "iteration",
                        f"{address}[{index}]",
                        frame.call_depth,
                        frame.id,
                        # Observation retains the complete parent view; actual
                        # execution still receives only its declared snapshot.
                        {**locked, **injected},
                        symbols,
                    )
                    diagnostic.cursor = node_id
                    try:
                        return await self._state_machine(
                            diagnostic, node, snapshot, symbols, injected, diagnostic.address
                        )
                    finally:
                        del self._frames[diagnostic.id]
                except BaseException:
                    stopped = True
                    raise

        slots = await _owned_group(
            [iteration(i, key, value) for i, (key, value) in enumerate(entries)]
        )
        output: JsonObject = {}
        for name in _out_names(node["body"]):
            if mapping:
                output[name] = {
                    key: slot["output"][name]
                    for (key, _), slot in zip(entries, slots, strict=True)
                    if name in slot["output"]
                }
            else:
                output[name] = [slot["output"][name] for slot in slots if name in slot["output"]]
        result = self._retain_result({"branch": None, "output": output}, frame.symbols)
        for slot in slots:
            self._release_result(slot)
        return result

    async def _state_machine(
        self,
        frame: Frame,
        node: JsonObject,
        snapshot: JsonObject,
        symbols: JsonObject,
        injected: JsonObject,
        address: str,
    ) -> JsonObject:
        with self._trace_call(node["body"]["kind"], address, snapshot) as span:
            if span is not None:
                # A root run is inside its callee function at the same address;
                # a graph node/iteration still points to the caller's node.
                span["frame_kind"] = (
                    "function"
                    if frame.kind == "function" and address == frame.address
                    else "iteration"
                )
                span["cursor"] = frame.cursor
            parent_scope = self._root_scope.get()
            scope: set[int] = set()
            token = self._root_scope.set(scope)
            result: JsonObject | None = None
            evicted = False
            try:
                self._hold(snapshot, symbols)
                result = await self._node_attempts(
                    frame, node, snapshot, symbols, injected, address
                )
                self._retain_result(result, self._output_types(node["body"], symbols))
                self._trace_result(span, result)
                return result
            except GeneratorExit:
                # Cache eviction discards reconstructed state; it is not a
                # lifecycle transition and can run outside the original Task.
                evicted = True
                raise
            finally:
                carried = result.get("_resource_root") if result is not None else None
                for root in scope:
                    if root != carried:
                        self._resource_roots.pop(root, None)
                if carried is not None and parent_scope is not None:
                    parent_scope.add(carried)
                if not evicted:
                    self._root_scope.reset(token)
                    await self._collect_resources()

    async def _node_attempts(
        self,
        frame: Frame,
        node: JsonObject,
        snapshot: JsonObject,
        symbols: JsonObject,
        injected: JsonObject,
        address: str,
    ) -> JsonObject:
        body, modifiers = node["body"], node["modifiers"]
        if body["kind"] == "builtin":
            span = self._trace_current()
            if span is not None:
                span["input_json"] = dumps(
                    {
                        name: loads(axcutor_core.eval_arg(dumps(arg), dumps(snapshot)))
                        for name, arg in body["bindings"]["input"].items()
                    }
                )
            return loads(
                axcutor_core.eval_builtin(
                    body["op"], dumps(body["bindings"]), dumps(snapshot), dumps(symbols)
                )
            )
        retry = modifiers["retry"]
        is_signal = body["kind"] == "run" and body["run"]["executor"] == "signal"
        is_activity = body["kind"] == "run" and not is_signal
        # Contract validation also runs after Activity success in this Workflow.
        # If it can retry, this loop must own the entire node attempt budget.
        native = (
            is_activity
            and not has_handles(body["input_schema"])
            and not has_handles(body["output"])
            and retry is not None
            and "timeout" in retry["on"]
            and "contract" not in retry["on"]
            and 0 < retry["backoff_ms"] <= 2147483647000
            and retry["max_attempts"] <= 2147483647
        )
        attempts = 1 if native or retry is None else retry["max_attempts"]
        span = self._trace_current()
        if span is not None and native:
            # SDK-managed retries have no per-attempt Workflow activation.
            # Expose the policy, never invent individual attempts or durations.
            self._trace_event("activity_retry_policy", **retry)
        for attempt in range(1, attempts + 1):
            root_scope = self._root_scope.get()
            roots_before = set(root_scope or ())
            try:
                with (
                    self._trace_call("attempt", address, snapshot)
                    if attempts > 1
                    else nullcontext(None)
                ) as attempt_span:
                    if attempt_span is not None:
                        attempt_span["attempt"] = attempt
                        assert span is not None
                        attempt_span["frame_kind"] = span["frame_kind"]
                        attempt_span["cursor"] = span["cursor"]
                    operation = self._attempt(
                        frame, node, snapshot, symbols, injected, address, native
                    )
                    timeout = modifiers["timeout_ms"]
                    if timeout is not None and not is_activity:
                        token = self._deadline_scope.set(True)
                        try:
                            result = await _with_timeout(operation, timeout, signal=is_signal)
                        finally:
                            self._deadline_scope.reset(token)
                    else:
                        result = await operation
                    self._trace_result(attempt_span, result)
                    return result
            except Recoverable as error:
                if root_scope is not None:
                    for root in root_scope - roots_before:
                        self._resource_roots.pop(root, None)
                    root_scope.intersection_update(roots_before)
                await self._collect_resources()
                if error.selector == "timeout":
                    self._signal_initialized = True  # A durable deadline has fired.
                if attempt < attempts and error.selector in retry["on"]:
                    self._trace_event(
                        "retry",
                        attempt=attempt,
                        backoff_ms=retry["backoff_ms"],
                        error={"type": error.selector, "message": str(error)},
                    )
                    if retry["backoff_ms"] > 0:
                        await workflow.sleep(timedelta(milliseconds=retry["backoff_ms"]))
                    continue
                for branch, selectors in modifiers["catch"].items():
                    if error.selector in selectors:
                        self._trace_event(
                            "caught",
                            branch=branch,
                            error={"type": error.selector, "message": str(error)},
                        )
                        return {"branch": branch, "output": {}}
                raise
        raise EngineError("attempt loop did not terminate")

    async def _attempt(
        self,
        frame: Frame,
        node: JsonObject,
        snapshot: JsonObject,
        symbols: JsonObject,
        injected: JsonObject,
        address: str,
        native_retry: bool,
    ) -> JsonObject:
        body = node["body"]
        if body["kind"] == "run":
            if body["run"]["executor"] == "signal":
                return await self._wait_signal(frame, node, address)
            return await self._run_activity(node, snapshot, native_retry)
        depth = frame.call_depth + 1
        if depth > self._options["max_call_depth"]:
            raise StackOverflowError("maximum call depth exceeded")
        if body["kind"] == "graph":
            span = self._trace_current()
            if span is not None:
                self._trace_input({**snapshot, **injected})
            inputs = {
                **{name: symbols[name] for name in body["input"]},
                **{name: symbols[name] for name in injected},
            }
            return await self._exec_graph(
                body["graph"],
                body,
                {**snapshot, **injected},
                inputs,
                depth,
                frame.id,
                address,
                "block",
            )
        if body["kind"] != "workflow":
            raise EngineError("unknown node kind")
        bindings = body["bindings"]
        snapshot_json = dumps(snapshot)
        values = {
            name: loads(axcutor_core.eval_arg(dumps(arg), snapshot_json))
            for name, arg in bindings["input"].items()
        }
        target = body["workflow_id"]
        root = self._bundle["workflows"][target]
        values = loads(axcutor_core.validate_input(dumps(values), dumps(root["input"])))
        span = self._trace_current()
        if span is not None:
            self._trace_input(values)
        threshold = self._options["child_workflow_history_events"]
        use_child = workflow.info().is_continue_as_new_suggested() or (
            threshold is not None and workflow.info().get_current_history_length() >= threshold
        )
        if use_child:
            try:
                allowed = self._registry.authorize(values, root["input"])
            except ValueError as error:
                raise Recoverable("execution", str(error)) from error
            resource_call = bool(allowed) or has_handles(root["output"])
            call_id = self._resource_call(allowed, "")
            child: asyncio.Future[Any] | None = None
            # Keep the call's place in the frame tree while the SDK owns the
            # child execution. The host follows this link without exposing IDs.
            remote = self._frame(
                "function",
                address,
                depth,
                frame.id,
                values,
                root["input"],
                root["body"]["graph"]["max_steps"] if root["body"]["kind"] == "graph" else None,
            )
            try:
                child = await workflow.start_child_workflow(
                    AxcutorInterpreterWorkflow.run,
                    {
                        "bundle_json": self._bundle_json,
                        "input_json": dumps(values),
                        "options": self._options,
                        "_entry": target,
                        "_call_depth": depth,
                        "resource_context": {
                            "resources": allowed,
                            "call_id": call_id,
                            "parent_run_id": workflow.info().run_id,
                            "default_queue": self._default_queue,
                        },
                    },
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                    parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                )
                self._signal_initialized = True
                self._resource_calls[call_id]["source_run_id"] = child.first_execution_run_id
                remote.child_execution = {
                    "workflow_id": child.id,
                    "run_id": child.first_execution_run_id,
                }
                if span is not None:
                    span["child_execution"] = {**remote.child_execution, "entry": target}
                response = await asyncio.shield(child)
                result, _ = self._receive_response(response, root, allowed)
                self._remember_residencies(call_id)
                self._resource_calls[call_id]["drained"] = True
            except exceptions.FailureError as error:
                drained = self._merge_failure(error, call_id)
                if child is not None and not drained and resource_call:
                    await self._settle_resource_call(child, call_id, root)
                raise boundary_error(error) from error
            except asyncio.CancelledError:
                if child is not None:
                    if resource_call:
                        await self._settle_resource_call(child, call_id, root)
                    else:
                        await cancel_and_drain([child])
                raise
            finally:
                del self._frames[remote.id]
                if not resource_call:
                    del self._resource_calls[call_id]
        else:
            result = await self._exec_root(target, values, depth, frame.id, address)
        mapped = self._retain_result(
            {
                "branch": result["branch"],
                "output": {
                    caller: result["output"][name]
                    for name, caller in bindings["output"].items()
                    if name in result["output"]
                },
            },
            self._output_types(body, symbols),
        )
        self._release_result(result)
        await self._collect_resources()
        return mapped

    async def _run_activity(
        self, node: JsonObject, snapshot: JsonObject, native_retry: bool
    ) -> JsonObject:
        body, modifiers = node["body"], node["modifiers"]
        timeout = modifiers["timeout_ms"] or self._options["activity_timeout_ms"]
        retry = modifiers["retry"]
        policy = RetryPolicy(maximum_attempts=1)
        if native_retry:
            policy = RetryPolicy(
                initial_interval=timedelta(milliseconds=retry["backoff_ms"]),
                maximum_interval=timedelta(milliseconds=retry["backoff_ms"]),
                backoff_coefficient=1.0,
                maximum_attempts=retry["max_attempts"],
                non_retryable_error_types=[
                    name for name in ("execution", "contract") if name not in retry["on"]
                ],
            )
        request = {
            "workflow_id": body["workflow_id"],
            "run_spec": body["run"],
            "input_schema": body["input_schema"],
            "input_snapshot": snapshot,
            "output_schema": body["output"],
            "branches_contract": body["branches"],
            "attempt_timeout_ms": timeout,
            "heartbeat_interval_ms": self._options["heartbeat_interval_ms"],
        }
        try:
            allowed = self._registry.authorize(snapshot, body["input_schema"])
            base = (
                self._options["task_queues"].get(body["run"]["executor"])
                or self._default_queue
                or workflow.info().task_queue
            )
            queue = self._registry.route(base, allowed, has_handles(body["output"]))
        except ValueError as error:
            raise Recoverable("execution", str(error)) from error
        resource_call = bool(allowed) or has_handles(body["output"])
        call_id = self._resource_call(allowed, workflow.info().run_id)
        request["resource_context"] = {"resources": allowed, "call_id": call_id}
        request["workflow_queue"] = self._default_queue or workflow.info().task_queue
        try:
            while True:
                handle = workflow.start_activity(
                    "execute_run",
                    dumps(request),
                    result_type=str,
                    task_queue=queue,
                    schedule_to_start_timeout=(
                        timedelta(seconds=STICKY_SECONDS) if queue != base else None
                    ),
                    start_to_close_timeout=timedelta(milliseconds=timeout),
                    heartbeat_timeout=timedelta(milliseconds=self._options["heartbeat_timeout_ms"]),
                    retry_policy=policy,
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                try:
                    response = await asyncio.shield(handle)
                    break
                except exceptions.FailureError as error:
                    cause = error.cause
                    if (
                        isinstance(cause, exceptions.TimeoutError)
                        and cause.type == exceptions.TimeoutType.SCHEDULE_TO_START
                    ):
                        if queue != base and all(d["locator"] is not None for d in allowed):
                            queue = base
                            continue
                        raise Recoverable(
                            "execution", "transient resource Worker is unavailable"
                        ) from error
                    drained = self._merge_failure(error, call_id)
                    if resource_call and not drained:
                        await self._settle_resource_call(handle, call_id, body)
                    raise boundary_error(error) from error
                except asyncio.CancelledError:
                    if resource_call:
                        await self._settle_resource_call(handle, call_id, body)
                    else:
                        await cancel_and_drain([handle])
                    raise
            self._signal_initialized = True
            result, worker_queue = self._receive_response(response, body, allowed)
            self._remember_residencies(call_id)
            base = (
                self._options["task_queues"].get(body["run"]["executor"])
                or self._default_queue
                or workflow.info().task_queue
            )
            if worker_queue is not None and pool(worker_queue) == base and resource_call:
                self._registry.sticky[base] = worker_queue
            self._resource_calls[call_id]["drained"] = True
            return result
        finally:
            # Pure-value calls cannot report later resource responsibility.
            if not resource_call:
                del self._resource_calls[call_id]
