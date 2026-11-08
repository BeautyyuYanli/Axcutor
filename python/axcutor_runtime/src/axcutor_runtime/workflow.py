"""Deterministic interpreter for the compiler's owned, normalized AST bundle.

Frames own private envs; only validated deltas cross a node boundary. Concurrent
members read the same snapshot and commit at their barrier. No business function,
filesystem, clock or process configuration is consulted during execution/replay.
The native kernel and Decimal codec must be passed through the Temporal sandbox.
"""

import asyncio
from collections.abc import Awaitable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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


async def _with_timeout(operation: Awaitable[JsonObject], timeout_ms: int) -> JsonObject:
    """Own the deadline and child together, retaining the timeout selector.

    SDK cancellation can raise temporalio.CancelledError rather than asyncio's
    CancelledError, so asyncio.wait_for cannot reliably identify its own timer.
    Explicitly racing a Workflow timer also waits for descendant cleanup.
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
        results = await cancel_and_drain([task, timer])
        if not isinstance(original, (asyncio.CancelledError, exceptions.CancelledError)):
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise _fatal_first([original, *errors]) from None
        raise


@workflow.defn
class AxcutorInterpreterWorkflow:
    """One read-only bundle plus a tree of active frames, reconstructed by replay."""

    def __init__(self) -> None:
        self._frames: dict[int, Frame] = {}
        self._next_frame = 0
        self._bundle: JsonObject = {}
        self._bundle_json = ""
        self._options: JsonObject = {}

    @workflow.run
    async def run(self, request: JsonObject) -> str:
        """Run {bundle_json,input_json,options?}; return {branch,output} JSON text.

        _entry and _call_depth are reserved for interpreter Child Workflows.
        Root ingress failures are terminal and cannot enter a DSL node catch.
        """
        try:
            self._bundle_json = request["bundle_json"]
            self._bundle = loads(self._bundle_json)
            self._options = self._parse_options(request.get("options", {}))
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

    @staticmethod
    def _parse_options(options: JsonObject) -> JsonObject:
        result: JsonObject = {
            "activity_timeout_ms": 7 * 24 * 60 * 60 * 1000,
            "heartbeat_interval_ms": 1000,
            "heartbeat_timeout_ms": 10000,
            "max_call_depth": 64,
            "task_queues": {},
            "child_workflow_history_events": None,
        }
        if not isinstance(options, dict) or options.keys() - result.keys():
            raise ValueError("unknown runtime options")
        result.update(deepcopy(options))
        for name in (
            "activity_timeout_ms",
            "heartbeat_interval_ms",
            "heartbeat_timeout_ms",
            "max_call_depth",
        ):
            if type(result[name]) is not int or result[name] <= 0:
                raise ValueError(f"{name} must be a positive integer")
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

    @workflow.query
    def get_runtime_snapshot(self) -> JsonObject:
        """Independent active-frame tree; envs remain exact JSON text."""
        nodes: dict[int, JsonObject] = {
            frame.id: {
                "id": frame.id,
                "parent_id": frame.parent_id,
                "kind": frame.kind,
                "address": frame.address,
                "call_depth": frame.call_depth,
                "cursor": frame.cursor,
                "steps": frame.steps,
                "max_steps": frame.max_steps,
                "env_json": dumps(frame.env),
                "children": [],
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
            return await self._for_each(frame, node, env, address)
        self._tick(frame)
        return await self._state_machine(
            frame, node, _snapshot(node["body"], env), frame.symbols, {}, address
        )

    async def _for_each(
        self, frame: Frame, node: JsonObject, env: JsonObject, address: str
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
                        {**snapshot, **injected},
                        symbols,
                    )
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
        # Contract validation also runs after Activity success in this Workflow.
        # If it can retry, this loop must own the entire node attempt budget.
        native = (
            body["kind"] == "run"
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
                if timeout is not None and body["kind"] != "run":
                    return await _with_timeout(operation, timeout)
                return await operation
            except Recoverable as error:
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
            try:
                response = await workflow.execute_child_workflow(
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
            except exceptions.FailureError as error:
                raise boundary_error(error) from error
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
        return _contract(response, body)
