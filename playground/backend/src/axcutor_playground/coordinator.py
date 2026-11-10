"""One resident Worker, one workspace and one root execution at a time.

Temporal owns execution state. This process retains only the current/latest
run's source, view and cumulative trace; a new preflight-approved ID replaces it.
Imports are discarded between runs, never while business code is in flight.
A lost submission response keeps the workspace busy until that ID is observed
terminal; it must never trigger another submission.
"""

import asyncio
import importlib
import logging
import shutil
import sys
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import axcutor_core
from axcutor_host import (
    AxcutorHost,
    Execution,
    ExecutionNotFound,
    InputValidationError,
    InvalidRequest,
    RuntimeUnavailable,
)
from axcutor_runtime.activities import AbortSignal, Executor
from axcutor_runtime.builtin import execute_builtin
from axcutor_runtime.bundle import CompilationError, compile_bundle
from axcutor_runtime.codec import JsonObject, dumps, loads
from axcutor_runtime.contracts import generate_contracts
from axcutor_runtime.module import module_executor
from axcutor_runtime.packages import load_mounts
from axcutor_runtime.worker import ResourceWorker, create_worker
from axcutor_runtime.workflow import AxcutorInterpreterWorkflow
from temporalio.client import Client
from temporalio.common import QueryRejectCondition

from .workspace import Workspace, WorkspaceConflict, WorkspaceView, discover_mounts, source_path

logger = logging.getLogger(__name__)

# Pair application heartbeats with Worker throttling for interactive cancellation.
_HEARTBEAT_INTERVAL_MS = 200


class SubmittedSource(TypedDict):
    entry: str
    mounts: dict[str, JsonObject]


class WorkspaceState(WorkspaceView):
    busy: bool
    execution_id: str | None


class WorkspaceExecution(TypedDict):
    execution: Execution
    source: SubmittedSource
    workspace: WorkspaceState


class LatestTrace(TypedDict):
    execution_id: str
    execution: Execution | None
    source: SubmittedSource
    spans: list[JsonObject]
    observed_at: str | None
    complete: bool
    trace_error: str | None


class Playground:
    """Own one Worker for the ASGI lifespan; refuse writes while busy.

    Executor wrappers count concurrent calls without serializing a graph. The
    completion waiter clears the ID only after Temporal closes and those calls
    drain. Save and Run preparation do not yield while holding the storage lock.
    The latest-record observer retries independently of that cleanup lock and
    stops after final capture or replacement by the next preflight-approved ID.
    """

    def __init__(self, client: Client, workspace: Workspace) -> None:
        self.client = client
        self.workspace = workspace
        self.queue = "axcutor-playground-" + str(uuid4())
        self.executions = AxcutorHost(
            client,
            self.queue,
            options={"heartbeat_interval_ms": _HEARTBEAT_INTERVAL_MS, "record_trace": True},
        )
        self.execution_id: str | None = None
        self.latest: LatestTrace | None = None
        self._trace_observer: asyncio.Task[None] | None = None
        self._completion: asyncio.Task[None] | None = None
        self._worker: ResourceWorker | None = None
        self._module: Executor = module_executor({})
        self._inflight = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._paths = [str(workspace.root / "workflows"), str(workspace.root)]

    async def start(self) -> None:
        self.workspace.open()
        sys.path[:0] = self._paths

        async def builtin(request: JsonObject, signal: AbortSignal) -> JsonObject:
            return await self._invoke(execute_builtin, request, signal)

        async def module(request: JsonObject, signal: AbortSignal) -> JsonObject:
            return await self._invoke(self._module, request, signal)

        self._worker = create_worker(
            self.client,
            self.queue,
            executors={"builtin": builtin, "module": module},
            graceful_shutdown_timeout=timedelta(seconds=5),
            max_heartbeat_throttle_interval=timedelta(milliseconds=_HEARTBEAT_INTERVAL_MS),
        )
        await self._worker.__aenter__()

    async def _invoke(
        self, executor: Executor, request: JsonObject, signal: AbortSignal
    ) -> JsonObject:
        self._inflight += 1
        self._drained.clear()
        try:
            return await executor(request, signal)
        finally:
            self._inflight -= 1
            if not self._inflight:
                self._drained.set()

    async def close(self) -> None:
        try:
            if self.execution_id:
                with suppress(RuntimeUnavailable, ExecutionNotFound, TimeoutError):
                    async with asyncio.timeout(5):
                        await self.executions.cancel(self.execution_id)
            if self._completion:
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._completion), 10)
                self._completion.cancel()
                with suppress(asyncio.CancelledError):
                    await self._completion
            if self._worker:
                if self._trace_observer:
                    self._trace_observer.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._trace_observer
                await self._worker.__aexit__(None, None, None)
                self._worker = None
        finally:
            self._clear_imports()
            for path in self._paths:
                if path in sys.path:
                    sys.path.remove(path)
            self.workspace.close()

    def view(self) -> WorkspaceState:
        return {
            **self.workspace.view(),
            "busy": self.execution_id is not None,
            "execution_id": self.execution_id,
        }

    def _require_idle(self) -> None:
        if self.execution_id is not None:
            raise WorkspaceConflict(
                "Playground is busy; wait for execution and local cleanup to finish",
                {"execution_id": self.execution_id},
            )

    async def save(self, revision: Any, files: Any) -> WorkspaceState:
        self._require_idle()
        await self.workspace.save(revision, files)
        return self.view()

    def _clear_imports(self) -> None:
        """Use origins, not surviving files or root names, to find owned modules.

        Deleted helpers still have an origin. Foreign packages with the same
        name must remain cached; Run rejects that name rather than unloading it.
        """
        root = self.workspace.root
        owned = []
        for name, module in list(sys.modules.items()):
            if module is None:
                continue
            origin = vars(module).get("__file__")
            locations = [origin] if isinstance(origin, str) else []
            locations.extend(vars(module).get("__path__", ()))
            if any(Path(path).resolve().is_relative_to(root) for path in locations):
                owned.append(name)
        # Namespace paths can consult their parent in sys.modules during the
        # scan. Collect first so those parents remain available until removal.
        for name in owned:
            sys.modules.pop(name, None)
        for directory in root.rglob("__pycache__"):
            shutil.rmtree(directory)
        importlib.invalidate_caches()

    async def submit(self, entry: Any, input: Any, revision: Any) -> WorkspaceExecution:
        self._require_idle()
        entry = source_path(entry)
        if input is not None:
            input = source_path(input)
            if not input.endswith(".json"):
                raise InvalidRequest("input must be a JSON file")
        async with self.workspace.lock:
            current = self.workspace.check_revision(revision)
            paths = {file["path"] for file in current["files"]}
            if entry not in paths or (input is not None and input not in paths):
                raise InvalidRequest("entry and input must identify existing workspace files")
            mounts = discover_mounts(self.workspace.root)
            try:
                entry_id, snapshot = load_mounts(self.workspace.root / entry, mounts)
                generate_contracts(entry_id, snapshot, mounts)
                inputs = loads((self.workspace.root / input).read_text("utf-8")) if input else {}
                if not isinstance(inputs, dict):
                    raise InvalidRequest("workflow input must be a JSON object")
                bundle = loads(compile_bundle(entry_id, snapshot))
                try:
                    axcutor_core.validate_root_interface(
                        dumps(bundle["workflows"][entry_id]["input"]),
                        dumps(bundle["workflows"][entry_id]["output"]),
                    )
                    axcutor_core.validate_input(
                        dumps(inputs), dumps(bundle["workflows"][entry_id]["input"])
                    )
                except (ValueError, TypeError, OverflowError) as error:
                    raise InputValidationError(str(error)) from error
            except (OSError, ValueError, TypeError) as error:
                if isinstance(error, CompilationError):
                    raise
                raise InvalidRequest(str(error)) from error
            self._clear_imports()
            for mount in mounts:
                if mount.name in sys.modules:
                    raise InvalidRequest(f"Python package name already in use: {mount.name!r}")
            self._module = module_executor({mount.name: mount.name for mount in mounts})
            execution_id = str(uuid4())
            self.execution_id = execution_id
            if self._trace_observer:
                self._trace_observer.cancel()
            record: LatestTrace = {
                "execution_id": execution_id,
                "execution": None,
                "source": {"entry": entry_id, "mounts": deepcopy(snapshot)},
                "spans": [],
                "observed_at": None,
                "complete": False,
                "trace_error": None,
            }
            self.latest = record

        try:
            execution = await self.executions.submit(entry_id, snapshot, inputs, execution_id)
            record["execution"] = execution
        except RuntimeUnavailable as error:
            raise RuntimeUnavailable(
                "submission outcome unknown; observe or cancel this ID, do not resubmit",
                {"execution_id": execution_id},
            ) from error
        finally:
            # This waiter also survives an HTTP disconnect or an ambiguous ACK.
            # It only observes this ID; NOT_FOUND never authorizes a new start.
            self._completion = asyncio.create_task(self._wait_until_done(execution_id))
            self._trace_observer = asyncio.create_task(self._observe_trace(record))
        return {
            "execution": execution,
            "source": {"entry": entry_id, "mounts": snapshot},
            "workspace": self.view(),
        }

    async def _wait_until_done(self, execution_id: str) -> None:
        while True:
            try:
                execution = await self.executions.wait(execution_id)
                if self.latest and self.latest["execution_id"] == execution_id:
                    self.latest["execution"] = execution
                break
            except (RuntimeUnavailable, ExecutionNotFound):
                logger.warning("Awaiting uncertain execution %s", execution_id)
                await asyncio.sleep(1)
        await self._drained.wait()
        self.execution_id = None

    async def _observe_trace(self, record: LatestTrace) -> None:
        """Capture final trace independently of the workspace cleanup lock.

        Queries replay closed Workflows on the resident Worker, including failed
        and cancelled runs. A failed observation never changes execution status
        or prevents cleanup. Retry until captured or replaced by a new ID.
        """
        while self.latest is record:
            terminal = (
                record["execution"] is not None and record["execution"]["status"] != "running"
            )
            try:
                spans, observed_at, problem = await self._read_trace(record["execution_id"])
                if problem:
                    # A child outage must not discard either newly observed root
                    # calls or already captured child calls. IDs are per invocation.
                    merged = {span["id"]: span for span in record["spans"]}
                    merged.update({span["id"]: span for span in spans})
                    spans = list(merged.values())
                record["spans"] = spans
                record["observed_at"] = observed_at
                record["trace_error"] = problem
                if terminal and not problem:
                    record["complete"] = True
                    return
            except RuntimeUnavailable as error:
                record["trace_error"] = str(error)
            await asyncio.sleep(0.3)

    async def _read_trace(self, execution_id: str) -> tuple[list[JsonObject], str, str | None]:
        """Flatten child traces with run-scoped IDs and the active-frame address mapping.

        Links remain in cumulative runtime spans after child frames disappear.
        Child queries pin the exact run; only this Playground cache owns records.
        Payload JSON is decoded with the same lossless codec as Execution values.
        """
        spans: list[JsonObject] = []
        problems: list[str] = []

        async def read(
            workflow_id: str,
            run_id: str | None = None,
            parent: str | None = None,
            prefix: tuple[str, str] | None = None,
        ) -> str:
            handle = self.client.get_workflow_handle(workflow_id, run_id=run_id)
            try:
                async with asyncio.timeout(5):
                    snapshot = await handle.query(
                        AxcutorInterpreterWorkflow.get_execution_trace,
                        reject_condition=QueryRejectCondition.NONE,
                        rpc_timeout=timedelta(seconds=5),
                    )
            except Exception as error:
                raise RuntimeUnavailable(f"Trace unavailable: {error}") from error
            local = snapshot["spans"]
            id_prefix = f"{workflow_id}:{run_id or 'root'}:"
            for item in local:
                child = item.pop("child_execution", None)
                span_id = id_prefix + str(item["id"])
                item["id"] = span_id
                item["parent_id"] = (
                    id_prefix + str(item["parent_id"]) if item["parent_id"] is not None else parent
                )
                if prefix:
                    item["address"] = prefix[1] + item["address"].removeprefix(prefix[0])
                    for event in item["events"]:
                        if "address" in event:
                            event["address"] = prefix[1] + event["address"].removeprefix(prefix[0])
                item["input"] = loads(item.pop("input_json"))
                output = item.pop("output_json")
                item["output"] = loads(output) if output is not None else None
                spans.append(item)
                if child:
                    # A child starts at its callee entry. Keep the same address
                    # prefix replacement used for active-frame snapshots.
                    try:
                        await read(
                            child["workflow_id"],
                            child["run_id"],
                            span_id,
                            (child["entry"], item["address"]),
                        )
                    except RuntimeUnavailable as error:
                        problems.append(str(error))
            return snapshot["observed_at"]

        recorded_at = await read(execution_id)
        # Query-only activations retain the last Workflow Task's logical time.
        # Open bars extend to this real observation time; completed span endpoints
        # remain replayable Workflow events. Preserve the logical lower bound if
        # a test server has advanced its clock or the hosts' clocks differ.
        observed_at = max(datetime.fromisoformat(recorded_at), datetime.now(UTC))
        return spans, observed_at.isoformat(), "; ".join(problems) or None
