"""Compile before durable submission and project Temporal into Execution values.

Temporal owns all execution state. This service holds only a client and immutable
configuration, never execution records or a result cache. Each read pins one run;
closed executions use history and need no Worker. Live queries are bounded and
rechecked against Describe so completion racing a query keeps terminal invariants.
"""

import asyncio
from collections.abc import Awaitable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict, TypeVar
from uuid import uuid4

import axcutor_core
from axcutor_runtime.bundle import compile_bundle
from axcutor_runtime.codec import JsonObject, dumps, loads
from axcutor_runtime.errors import SELECTORS
from axcutor_runtime.packages import package_path
from axcutor_runtime.workflow import AxcutorInterpreterWorkflow
from temporalio import exceptions
from temporalio.client import (
    Client,
    WorkflowExecutionDescription,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import QueryRejectCondition, RetryPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

T = TypeVar("T")


class Frame(TypedDict):
    kind: Literal["function", "block", "iteration"]
    address: str
    cursor: str
    waiting: list[str]
    call_depth: int
    steps: int
    max_steps: int | None
    variables: JsonObject
    children: list["Frame"]


class ExecutionResult(TypedDict):
    branch: str | None
    output: JsonObject


class ExecutionError(TypedDict):
    category: Literal["recoverable", "panic"]
    type: str
    message: str


class Execution(TypedDict):
    execution_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    finished_at: str | None
    frames: list[Frame]
    result: ExecutionResult | None
    error: ExecutionError | None


class HostError(Exception):
    """Stable protocol-neutral error code and optional structured details."""

    code = "host_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class InvalidRequest(HostError):
    code = "invalid_request"


class InputValidationError(HostError):
    code = "input_validation_failed"


class ExecutionNotFound(HostError):
    code = "execution_not_found"


class ExecutionAlreadyExists(HostError):
    code = "execution_already_exists"


class RuntimeUnavailable(HostError):
    code = "runtime_unavailable"


class SignalNotWaiting(HostError):
    code = "signal_not_waiting"


class SignalValidationError(HostError):
    code = "signal_validation_failed"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _failure(cause: BaseException) -> ExecutionError:
    if isinstance(cause, exceptions.ApplicationError):
        if cause.type in SELECTORS:
            return {"category": "recoverable", "type": cause.type, "message": cause.message}
        if cause.type in ("StackOverflowError", "MaxStepsExceededError", "EngineError"):
            return {"category": "panic", "type": cause.type, "message": cause.message}
    # External termination, run timeout and unknown SDK failures are engine
    # failures, not DSL recoverable selectors or cooperative cancellation.
    return {"category": "panic", "type": "EngineError", "message": str(cause)}


def _validate_submission(
    entry: str, packages: dict[str, dict[str, Any]], inputs: JsonObject, execution_id: str | None
) -> None:
    if not isinstance(entry, str) or not entry:
        raise InvalidRequest("entry must be __main__ or a public dotted package path")
    if entry != "__main__" and not package_path(entry, public=True):
        raise InvalidRequest("entry must be __main__ or a public dotted package path")
    if not isinstance(packages, dict):
        raise InvalidRequest("packages must be an object")
    for path, source in packages.items():
        if not isinstance(path, str) or not isinstance(source, dict):
            raise InvalidRequest("packages must map dotted names to {format, text} objects")
        if path != "__main__" and not package_path(path):
            raise InvalidRequest("package names require dotted paths with public roots")
        if (
            source.keys() != {"format", "text"}
            or source["format"] not in ("yaml", "json")
            or not isinstance(source["text"], str)
        ):
            raise InvalidRequest("each source requires format (yaml or json) and text (string)")
    if not isinstance(inputs, dict):
        raise InvalidRequest("input must be an object")
    if execution_id is not None and (not isinstance(execution_id, str) or not execution_id):
        raise InvalidRequest("execution_id must be a nonempty string")


class ExecutionService:
    """One async client, a default queue and copied runtime options.

    A lazy Client lets controllers perform compile/input preflight even during a
    backend outage. rpc_timeout bounds each backend operation, including a live
    query with no Worker. A successful start can precede a query timeout; callers
    should supply an ID and GET it after an ambiguous 503. IDs cannot be reused
    while Temporal retains the execution (including closed executions).
    """

    def __init__(
        self,
        client: Client,
        task_queue: str = "axcutor",
        *,
        options: JsonObject | None = None,
        rpc_timeout: timedelta = timedelta(seconds=5),
    ) -> None:
        if rpc_timeout.total_seconds() <= 0:
            raise ValueError("rpc_timeout must be positive")
        self._client = client
        self._task_queue = task_queue
        self._options = deepcopy(options or {})
        self._timeout = rpc_timeout

    async def submit(
        self,
        entry: str,
        packages: dict[str, dict[str, Any]],
        input: JsonObject | None = None,
        execution_id: str | None = None,
    ) -> Execution:
        """Synchronously compile/validate, then start once and read its view.

        CompilationError retains compiler diagnostics. Shape and input errors
        never create a durable execution or contact the backend.
        """
        inputs = {} if input is None else input
        _validate_submission(entry, packages, inputs, execution_id)
        bundle_json = compile_bundle(entry, packages)
        bundle = loads(bundle_json)
        try:
            input_json = axcutor_core.validate_input(
                dumps(inputs), dumps(bundle["workflows"][entry]["input"])
            )
        except (ValueError, TypeError, OverflowError) as error:
            raise InputValidationError(str(error)) from error
        execution_id = execution_id or str(uuid4())
        try:
            handle = await self._call(
                self._client.start_workflow(
                    AxcutorInterpreterWorkflow.run,
                    {
                        "bundle_json": bundle_json,
                        "input_json": input_json,
                        "options": self._options,
                    },
                    id=execution_id,
                    task_queue=self._task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    rpc_timeout=self._timeout,
                ),
                execution_id,
            )
        except exceptions.WorkflowAlreadyStartedError as error:
            raise ExecutionAlreadyExists(f"execution {execution_id!r} already exists") from error
        # start_workflow handles have no query/describe run_id. Bind all later
        # operations to the run actually started, not the latest same-name run.
        return await self._get(execution_id, handle.result_run_id)

    async def get(self, execution_id: str) -> Execution:
        """Read current state; closed results remain readable without a Worker."""
        return await self._get(execution_id)

    async def cancel(self, execution_id: str) -> Execution:
        """Request cooperative cancellation; terminal states are idempotent.

        A running response means cleanup is still in flight. Cancellation never
        becomes a force termination and never guesses completion from an ACK.
        """
        description = await self._describe(self._handle(execution_id))
        handle = self._handle(execution_id, description.run_id)
        if description.status == WorkflowExecutionStatus.RUNNING:
            try:
                await self._call(handle.cancel(rpc_timeout=self._timeout), execution_id)
            except ExecutionNotFound:
                # A close racing cancellation may report NOT_FOUND. Describe the
                # same run again before deciding this resource is missing.
                description = await self._describe(handle)
                if description.status == WorkflowExecutionStatus.RUNNING:
                    raise
        return await self._get(execution_id, description.run_id)

    async def wait(self, execution_id: str) -> Execution:
        """Wait on one run's history without owning or cancelling its work.

        The business wait has no host RPC deadline and needs no Worker queries.
        Cancelling the calling task stops only this wait, not the execution.
        """
        description = await self._describe(self._handle(execution_id))
        handle = self._handle(execution_id, description.run_id)
        try:
            # The SDK long-polls close history. A per-RPC host timeout would
            # expire during normal long executions or a temporary Worker pause.
            await handle.result(follow_runs=False)
        except WorkflowFailureError:
            pass  # The terminal projection below preserves failure/cancel data.
        except RPCError as error:
            raise self._rpc_error(error, execution_id) from error
        except (RuntimeError, TimeoutError) as error:
            raise RuntimeUnavailable(f"runtime unavailable: {error}") from error
        return await self._get(execution_id, description.run_id)

    async def signal(
        self,
        execution_id: str,
        address: str,
        output: JsonObject | None = None,
        branch: str | None = None,
        step: int | None = None,
    ) -> Execution:
        """Deliver once to a waiting address, transparently crossing Child Workflows.

        Each handle pins its run. After root existence is established, a closed
        target is a conflict, including a close racing a query or Update. A 503
        means delivery is unknown; cancelling this caller never cancels the run.
        """
        if not isinstance(address, str) or not address:
            raise InvalidRequest("address must be a nonempty string")
        if output is not None and not isinstance(output, dict):
            raise InvalidRequest("output must be an object")
        if branch is not None and (not isinstance(branch, str) or not branch):
            raise InvalidRequest("branch must be a nonempty string or null")
        if step is not None and (type(step) is not int or step < 1):
            raise InvalidRequest("step must be a positive integer or null")
        try:
            # Freeze caller-owned data before any network suspension.
            request = loads(
                dumps({"address": address, "output": output or {}, "branch": branch, "step": step})
            )
        except (ValueError, TypeError, RecursionError, OverflowError) as error:
            raise InvalidRequest(str(error)) from error
        description = await self._describe(self._handle(execution_id))
        if description.status != WorkflowExecutionStatus.RUNNING:
            raise SignalNotWaiting(f"execution {execution_id!r} has ended")
        handle = self._handle(execution_id, description.run_id)
        current_prefix: str | None = None
        try:
            # The overall bound includes lazy connections and Update result polling.
            async with asyncio.timeout(self._timeout.total_seconds()):
                while True:
                    snapshot = await handle.query(
                        AxcutorInterpreterWorkflow.get_runtime_snapshot,
                        reject_condition=QueryRejectCondition.NOT_OPEN,
                        rpc_timeout=self._timeout,
                    )
                    nodes = list(snapshot["frames"])
                    if not nodes:
                        raise SignalNotWaiting(f"no signal node waiting at {address!r}")
                    local_root = nodes[0]["address"]
                    if current_prefix is None:
                        current_prefix = local_root
                        if address != local_root and not address.startswith(local_root + "/"):
                            raise SignalNotWaiting(f"no signal node waiting at {address!r}")
                    local_target = local_root + address.removeprefix(current_prefix)
                    remote = None
                    while nodes:
                        node = nodes.pop()
                        if node.get("child_execution") and (
                            local_target == node["address"]
                            or local_target.startswith(node["address"] + "/")
                        ):
                            remote = node
                            break
                        nodes.extend(node["children"])
                    if remote is None:
                        break
                    current_prefix += remote["address"].removeprefix(local_root)
                    child = remote["child_execution"]
                    handle = self._handle(child["workflow_id"], child["run_id"])
                request["address"] = local_target
                await handle.execute_update(
                    AxcutorInterpreterWorkflow.deliver_signal,
                    dumps(request),
                    rpc_timeout=self._timeout,
                )
        except WorkflowUpdateFailedError as error:
            cause = error.cause
            if isinstance(cause, exceptions.ApplicationError):
                if cause.type == "SignalNotWaitingError":
                    raise SignalNotWaiting(cause.message) from error
                if cause.type == "SignalValidationError":
                    raise SignalValidationError(cause.message) from error
            raise RuntimeUnavailable(f"signal Update failed: {cause}") from error
        except WorkflowUpdateRPCTimeoutOrCancelledError as error:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                # The SDK wraps caller cancellation as an Update RPC error.
                raise asyncio.CancelledError from error
            raise RuntimeUnavailable(f"signal delivery is unknown: {error}") from error
        except RPCError as error:
            if error.status in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION):
                raise SignalNotWaiting(f"no signal node waiting at {address!r}") from error
            raise RuntimeUnavailable(f"runtime unavailable: {error.message}") from error
        except WorkflowQueryRejectedError as error:
            raise SignalNotWaiting(f"no signal node waiting at {address!r}") from error
        except (WorkflowQueryFailedError, RuntimeError, TimeoutError) as error:
            raise RuntimeUnavailable(f"runtime unavailable: {error}") from error
        return await self._get(execution_id, description.run_id)

    def _handle(self, execution_id: str, run_id: str | None = None) -> WorkflowHandle[Any, Any]:
        return self._client.get_workflow_handle(
            execution_id, run_id=run_id, first_execution_run_id=run_id
        )

    def _rpc_error(self, error: RPCError, execution_id: str) -> HostError:
        if error.status == RPCStatusCode.NOT_FOUND:
            return ExecutionNotFound(f"execution {execution_id!r} does not exist")
        if error.status == RPCStatusCode.ALREADY_EXISTS:
            return ExecutionAlreadyExists(f"execution {execution_id!r} already exists")
        if error.status == RPCStatusCode.INVALID_ARGUMENT:
            return InvalidRequest(error.message)
        return RuntimeUnavailable(f"runtime unavailable: {error.message}")

    async def _call(self, operation: Awaitable[T], execution_id: str) -> T:
        """Bound lazy connection as well as RPC retries; preserve task cancellation."""
        try:
            async with asyncio.timeout(self._timeout.total_seconds()):
                return await operation
        except RPCError as error:
            raise self._rpc_error(error, execution_id) from error
        except (RuntimeError, TimeoutError) as error:
            raise RuntimeUnavailable(f"runtime unavailable: {error}") from error

    async def _describe(self, handle: WorkflowHandle[Any, Any]) -> WorkflowExecutionDescription:
        description = await self._call(handle.describe(rpc_timeout=self._timeout), handle.id)
        if description.workflow_type != "AxcutorInterpreterWorkflow" or description.parent_id:
            raise ExecutionNotFound(f"execution {handle.id!r} does not exist")
        return description

    async def _get(self, execution_id: str, run_id: str | None = None) -> Execution:
        handle = self._handle(execution_id, run_id)
        description = await self._describe(handle)
        handle = self._handle(execution_id, description.run_id)
        frames: list[Frame] = []
        query_error: Exception | None = None
        if description.status == WorkflowExecutionStatus.RUNNING:
            try:
                snapshot = await self._call(
                    handle.query(
                        AxcutorInterpreterWorkflow.get_runtime_snapshot,
                        reject_condition=QueryRejectCondition.NOT_OPEN,
                        rpc_timeout=self._timeout,
                    ),
                    execution_id,
                )
                frames = await self._frames(snapshot["frames"])
            except (RPCError, WorkflowQueryRejectedError, WorkflowQueryFailedError, HostError) as e:
                query_error = e
            # A result or cancellation may have committed during any live query.
            description = await self._describe(handle)
            if description.status == WorkflowExecutionStatus.RUNNING and query_error:
                raise RuntimeUnavailable(f"runtime Worker did not answer: {query_error}")
        view: Execution = {
            "execution_id": execution_id,
            "status": "running",
            "started_at": _timestamp(description.start_time),
            "finished_at": None,
            "frames": frames,
            "result": None,
            "error": None,
        }
        if description.status == WorkflowExecutionStatus.RUNNING:
            return view
        view["frames"] = []
        if description.close_time is None:
            raise RuntimeUnavailable("closed execution has no finish time")
        view["finished_at"] = _timestamp(description.close_time)
        try:
            # result() long-polls history. Describe proved this run closed; keep
            # an overall deadline too, and do not follow a different run chain.
            result = await self._call(
                handle.result(follow_runs=False, rpc_timeout=self._timeout), execution_id
            )
        except WorkflowFailureError as error:
            if isinstance(error.cause, exceptions.CancelledError):
                view["status"] = "cancelled"
            else:
                view["status"] = "failed"
                view["error"] = _failure(error.cause)
        else:
            view["status"] = "completed"
            view["result"] = loads(result)
        return view

    async def _frames(
        self, nodes: list[JsonObject], prefix: tuple[str, str] | None = None
    ) -> list[Frame]:
        result: list[Frame] = []
        for node in nodes:
            address = node["address"]
            if prefix:
                address = prefix[1] + address.removeprefix(prefix[0])
            remote = node.get("child_execution")
            if remote:
                child = self._handle(remote["workflow_id"], remote["run_id"])
                try:
                    snapshot = await self._call(
                        child.query(
                            AxcutorInterpreterWorkflow.get_runtime_snapshot,
                            reject_condition=QueryRejectCondition.NOT_OPEN,
                            rpc_timeout=self._timeout,
                        ),
                        child.id,
                    )
                except WorkflowQueryRejectedError:
                    continue  # The child closed before its parent committed.
                except (HostError, WorkflowQueryFailedError):
                    description = await self._call(
                        child.describe(rpc_timeout=self._timeout), child.id
                    )
                    if description.status != WorkflowExecutionStatus.RUNNING:
                        continue
                    raise
                if snapshot["frames"]:
                    child_entry = snapshot["frames"][0]["address"]
                    result.extend(await self._frames(snapshot["frames"], (child_entry, address)))
                continue
            result.append(
                {
                    "kind": node["kind"],
                    "address": address,
                    "cursor": node["cursor"],
                    "waiting": [
                        prefix[1] + waiting.removeprefix(prefix[0]) if prefix else waiting
                        for waiting in node["waiting"]
                    ],
                    "call_depth": node["call_depth"],
                    "steps": node["steps"],
                    "max_steps": node["max_steps"],
                    "variables": loads(node["env_json"]),
                    "children": await self._frames(node["children"], prefix),
                }
            )
        return result
