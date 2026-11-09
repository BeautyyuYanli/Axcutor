"""One resident Worker, one workspace and one root execution at a time.

Temporal owns history/results. Only the active ID, its completion waiter and
local executor calls live here. Imports are discarded between runs, never while
business code is in flight. A lost submission response keeps the workspace busy
until that ID is observed terminal; it must never trigger another submission.
"""

import asyncio
import importlib
import logging
import shutil
import sys
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import axcutor_core
from axcutor_host.service import (
    Execution,
    ExecutionNotFound,
    ExecutionService,
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
from axcutor_runtime.packages import load_packages
from axcutor_runtime.worker import create_worker
from temporalio.client import Client
from temporalio.worker import Worker

from .workspace import Workspace, WorkspaceConflict, WorkspaceView, discover_mounts, source_path

logger = logging.getLogger(__name__)


class SubmittedSource(TypedDict):
    entry: str
    packages: dict[str, dict[str, str]]


class WorkspaceState(WorkspaceView):
    busy: bool
    execution_id: str | None


class WorkspaceExecution(TypedDict):
    execution: Execution
    source: SubmittedSource
    workspace: WorkspaceState


class Playground:
    """Own one Worker for the ASGI lifespan; refuse writes while busy.

    Executor wrappers count concurrent calls without serializing a graph. The
    completion waiter clears the ID only after Temporal closes and those calls
    drain. Save and Run preparation do not yield while holding the storage lock.
    """

    def __init__(self, client: Client, workspace: Workspace) -> None:
        self.client = client
        self.workspace = workspace
        self.queue = "axcutor-playground-" + str(uuid4())
        self.executions = ExecutionService(client, self.queue)
        self.execution_id: str | None = None
        self._completion: asyncio.Task[None] | None = None
        self._worker: Worker | None = None
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
                entry_id, packages = load_packages(self.workspace.root / entry, mounts)
                generate_contracts(entry_id, packages, mounts)
                inputs = loads((self.workspace.root / input).read_text("utf-8")) if input else {}
                if not isinstance(inputs, dict):
                    raise InvalidRequest("workflow input must be a JSON object")
                bundle = loads(compile_bundle(entry_id, packages))
                try:
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

        try:
            execution = await self.executions.submit(entry_id, packages, inputs, execution_id)
        except RuntimeUnavailable as error:
            raise RuntimeUnavailable(
                "submission outcome unknown; observe or cancel this ID, do not resubmit",
                {"execution_id": execution_id},
            ) from error
        finally:
            # This waiter also survives an HTTP disconnect or an ambiguous ACK.
            # It only observes this ID; NOT_FOUND never authorizes a new start.
            self._completion = asyncio.create_task(self._wait_until_done(execution_id))
        return {
            "execution": execution,
            "source": {"entry": entry_id, "packages": packages},
            "workspace": self.view(),
        }

    async def _wait_until_done(self, execution_id: str) -> None:
        while True:
            try:
                await self.executions.wait(execution_id)
                break
            except (RuntimeUnavailable, ExecutionNotFound):
                logger.warning("Awaiting uncertain execution %s", execution_id)
                await asyncio.sleep(1)
        await self._drained.wait()
        self.execution_id = None
