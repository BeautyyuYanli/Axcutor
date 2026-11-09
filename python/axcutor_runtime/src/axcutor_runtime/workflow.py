"""Deterministic interpreter for the compiler's owned, normalized AST bundle.

Frames own private envs; only validated deltas cross a node boundary. Concurrent
members read the same snapshot and commit at their barrier. No business function,
filesystem, clock or process configuration is consulted during execution/replay.
The native kernel and Decimal codec must be passed through the Temporal sandbox.
"""

import asyncio
from collections.abc import Awaitable, Sequence
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
    }
    if not isinstance(options, dict) or options.keys() - (
        result.keys() | {"heartbeat_interval_ms"}
    ):
        raise ValueError("unknown runtime options")
    result.update(deepcopy(options))
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
        # Tasks inherit their enclosing deadline, independently of sibling tasks.
        self._deadline_scope: ContextVar[bool] = ContextVar("axcutor_deadline", default=False)

    @workflow.run
    async def run(self, request: JsonObject) -> str:
        """Run {bundle_json,input_json,options?}; return {branch,output} JSON text.

        _entry and _call_depth are reserved for interpreter Child Workflows.
        Root ingress failures are terminal and cannot enter a DSL node catch.
        """
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
            values = loads(axcutor_core.validate_input(request["input_json"], dumps(root["input"])))
            return dumps(await self._exec_root(entry, values, depth, None, entry))
        except (asyncio.CancelledError, exceptions.CancelledError):
            raise
        except Recoverable as error:
            raise exceptions.ApplicationError(
                str(error), type=error.selector, non_retryable=True
            ) from error
        except EngineError as error:
            raise exceptions.ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        except Exception as error:
            # Invalid bundles/requests and kernel invariants fail the Workflow,
            # rather than producing endless failing Workflow Tasks.
            raise exceptions.ApplicationError(
                str(error), type="EngineError", non_retryable=True
            ) from error
        finally:
            if self._signal_init_timer is not None:
                # This task only owns a Workflow sleep. Propagate cancellation
                # of the run while gathering its cancellation/completion.
                self._signal_init_timer.cancel()
                await asyncio.gather(self._signal_init_timer, return_exceptions=True)

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
        try:
            return await self._state_machine(frame, node, values, frame.symbols, {}, address)
        finally:
            del self._frames[frame.id]

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
        try:
            while True:
                route = graph["routes"][frame.cursor]
                for name in route["kill_vars"]:
                    frame.env.pop(name, None)
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
                    target = edge["join_target"]
                else:
                    if edge["target"]["kind"] == "fail":
                        raise Recoverable("execution", "graph reached fail")
                    target = edge["target"]["target"]
                if target["kind"] == "end":
                    return _contract({"branch": target["branch_id"], "output": frame.env}, host)
                node_id = target["node_id"]
                frame.cursor = node_id
                result = await self._activate(frame, graph, node_id, frame.env)
                frame.env.update(result["output"])
                frame.cursor = node_id + (
                    "." + result["branch"] if result["branch"] is not None else ""
                )
        finally:
            del self._frames[frame.id]

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
            return await self._for_each(frame, node, env, address, node_id)
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
        return {"branch": None, "output": output}

    async def _state_machine(
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
            and retry is not None
            and "timeout" in retry["on"]
            and "contract" not in retry["on"]
            and 0 < retry["backoff_ms"] <= 2147483647000
            and retry["max_attempts"] <= 2147483647
        )
        attempts = 1 if native or retry is None else retry["max_attempts"]
        for attempt in range(1, attempts + 1):
            try:
                operation = self._attempt(frame, node, snapshot, symbols, injected, address, native)
                timeout = modifiers["timeout_ms"]
                if timeout is not None and not is_activity:
                    token = self._deadline_scope.set(True)
                    try:
                        return await _with_timeout(operation, timeout, signal=is_signal)
                    finally:
                        self._deadline_scope.reset(token)
                return await operation
            except Recoverable as error:
                if error.selector == "timeout":
                    self._signal_initialized = True  # A durable deadline has fired.
                if attempt < attempts and error.selector in retry["on"]:
                    if retry["backoff_ms"] > 0:
                        await workflow.sleep(timedelta(milliseconds=retry["backoff_ms"]))
                    continue
                for branch, selectors in modifiers["catch"].items():
                    if error.selector in selectors:
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
        threshold = self._options["child_workflow_history_events"]
        use_child = workflow.info().is_continue_as_new_suggested() or (
            threshold is not None and workflow.info().get_current_history_length() >= threshold
        )
        if use_child:
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
                    },
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                    parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
                )
                self._signal_initialized = True
                remote.child_execution = {
                    "workflow_id": child.id,
                    "run_id": child.first_execution_run_id,
                }
                response = await child
            except exceptions.FailureError as error:
                raise boundary_error(error) from error
            finally:
                del self._frames[remote.id]
            result = _contract(response, root)
        else:
            result = await self._exec_root(target, values, depth, frame.id, address)
        return {
            "branch": result["branch"],
            "output": {
                caller: result["output"][name]
                for name, caller in bindings["output"].items()
                if name in result["output"]
            },
        }

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
            response = await workflow.execute_activity(
                "execute_run",
                dumps(request),
                result_type=str,
                task_queue=self._options["task_queues"].get(body["run"]["executor"]),
                start_to_close_timeout=timedelta(milliseconds=timeout),
                heartbeat_timeout=timedelta(milliseconds=self._options["heartbeat_timeout_ms"]),
                retry_policy=policy,
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except exceptions.FailureError as error:
            raise boundary_error(error) from error
        self._signal_initialized = True
        return _contract(response, body)
