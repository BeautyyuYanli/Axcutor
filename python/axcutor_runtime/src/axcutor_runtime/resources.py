"""Worker-local resource protocol, physical Store, and Attempt-scoped authority.

Hooks run without a resource context. Store operations serialize per ID, while
business methods run outside locks. Pending entries reject new borrowers and
retain their instance on disposal failure. No process-global resource registry
or background retry service is installed.
"""

import asyncio
import inspect
import logging
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, overload
from uuid import uuid4

import axcutor_core
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from .codec import JsonObject, dumps, loads
from .packages import package_segment
from .python_loader import load_module, logical_module, module_mounts, python_module
from .resource_model import description, same_identity, scan, valid_kind

T = TypeVar("T")
_RESOURCE = "__axcutor_resource__"
_current: ContextVar["ResourceContext | None"] = ContextVar("axcutor_resources", default=None)
logger = logging.getLogger(__name__)


class Handle(BaseModel, Generic[T]):
    """Immutable resource identity; the parameter is only a static class contract."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    rid: StrictStr
    kind: StrictStr

    @field_validator("rid")
    @classmethod
    def _rid(cls, value: str) -> str:
        if not value:
            raise ValueError("resource ID must be nonempty")
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not valid_kind(value):
            raise ValueError("invalid resource kind")
        return value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Handle) and self.rid == other.rid

    def __hash__(self) -> int:
        return hash(self.rid)


def _protocol(cls: type[Any]) -> bool:
    if _RESOURCE not in vars(cls):
        raise TypeError("resource class must be directly decorated with @resource")
    persistent = vars(cls)[_RESOURCE]
    locator = getattr(cls, "locator", None)
    restore = inspect.getattr_static(cls, "restore", None)
    if (locator is None) != (restore is None):
        raise TypeError("locator and restore must be declared together")
    if locator is not None and (
        not callable(locator)
        or not isinstance(restore, (classmethod, staticmethod))
        or not callable(cls.restore)
    ):
        raise TypeError("restore must be a classmethod or staticmethod")
    dispose = getattr(cls, "dispose", None)
    if dispose is not None and not callable(dispose):
        raise TypeError("dispose must be callable")
    if persistent and (locator is None or dispose is None):
        raise TypeError("persistent resources require locator, restore and dispose")
    return persistent


@overload
def resource(cls: type[T], *, persistent: bool = False) -> type[T]: ...


@overload
def resource(*, persistent: bool = False) -> Callable[[type[T]], type[T]]: ...


def resource(cls: type[T] | None = None, *, persistent: bool = False) -> Any:
    """Declare a public module-level class; undecorated subclasses are rejected."""
    if type(persistent) is not bool:
        raise TypeError("persistent must be a boolean")

    def decorate(target: type[T]) -> type[T]:
        if (
            not isinstance(target, type)
            or target.__qualname__ != target.__name__
            or target.__name__.startswith("_")
            or not package_segment(target.__name__)
            or not all(package_segment(part) for part in target.__module__.split("."))
        ):
            raise TypeError("resource requires a public module-level class")
        setattr(target, _RESOURCE, persistent)
        _protocol(target)
        return target

    return decorate(cls) if cls is not None else decorate


async def _hook(function: Any, *args: Any) -> Any:
    token = _current.set(None)
    try:
        value = function(*args)
        return await value if inspect.isawaitable(value) else value
    finally:
        _current.reset(token)


@dataclass
class _Entry:
    value: Any
    descriptor: JsonObject
    users: set["ResourceContext"] = field(default_factory=set)
    pending: bool = False
    delete_external: bool = True


class ResourceStore:
    """One event-loop-owned instance table shared by a composite Worker.

    Kinds address a package's public exports. Definitions may live in its
    initializer or direct file submodules, never another package. Static resource
    headers belong to compilation and are not read by the Store. Local disposal
    follows the actual class protocol, including external descriptors whose
    persistent field remains null. External deletion revocation is irreversible.
    """

    def __init__(self, worker_queue: str, mounts: Mapping[str, str] | None = None) -> None:
        self.worker_queue = worker_queue
        self.mounts = module_mounts(mounts)
        self.entries: dict[str, _Entry] = {}
        # Revocation survives cache eviction, so delayed restore/cleanup requests
        # cannot regain permission to delete an entity already handed to a host.
        self._external: set[str] = set()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.closed = False
        self.settlements: set[asyncio.Task[None]] = set()

    def retain(self, task: asyncio.Task[None]) -> None:
        self.settlements.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            self.settlements.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error("resource cleanup task failed: %s", completed.exception())

        task.add_done_callback(finished)

    @asynccontextmanager
    async def _locked(self, rid: str) -> AsyncIterator[None]:
        lock, count = self._locks.get(rid, (asyncio.Lock(), 0))
        self._locks[rid] = lock, count + 1
        try:
            async with lock:
                yield
        finally:
            _, count = self._locks[rid]
            if count == 1:
                del self._locks[rid]
            else:
                self._locks[rid] = lock, count - 1

    def resolve_resource_class(
        self, kind: str, *, descriptor: JsonObject | None = None
    ) -> type[Any]:
        """Resolve and check the class protocol once, optionally against a descriptor."""
        if not valid_kind(kind):
            raise TypeError("invalid resource kind")
        module, name = kind.split(":")
        physical = python_module(module, self.mounts)
        cls = getattr(load_module(physical, current_package=True), name)
        if (
            not isinstance(cls, type)
            or cls.__qualname__ != name
            or getattr(sys.modules.get(cls.__module__), "__package__", None) != physical
        ):
            raise TypeError("resource kind must address a public class defined in the same package")
        persistent = _protocol(cls)
        if descriptor is not None:
            if descriptor["persistent"] is not None and persistent != descriptor["persistent"]:
                raise TypeError("resource protocol differs from its registered description")
            # _protocol already checks that locator/restore form a callable pair.
            if descriptor["persistent"] is None and getattr(cls, "locator", None) is None:
                raise TypeError("external resources require locator and restore")
        return cls

    def _class_kind(self, value: Any) -> str:
        cls = type(value)
        package = getattr(sys.modules.get(cls.__module__), "__package__", None)
        if not isinstance(package, str) or not package:
            raise TypeError("resource class must belong to a Python package")
        kind = f"{logical_module(package, self.mounts)}:{cls.__name__}"
        if self.resolve_resource_class(kind) is not cls:
            raise TypeError("resource class identity mismatch")
        return kind

    async def _validate_instance(self, value: Any, descriptor: JsonObject) -> None:
        """Check the instance locator after its class protocol has been validated."""
        locator = await _hook(value.locator) if hasattr(value, "locator") else None
        if not axcutor_core.literal_equal(dumps(locator), dumps(descriptor["locator"])):
            raise ValueError("resource locator differs from its registered description")

    async def get(self, context: "ResourceContext", descriptor: JsonObject) -> Any:
        rid = descriptor["rid"]
        async with self._locked(rid):
            context._check_open()
            if self.closed:
                raise RuntimeError("resource Store is closed")
            entry = self.entries.get(rid)
            cached = entry is not None
            cls = self.resolve_resource_class(descriptor["kind"], descriptor=descriptor)
            if descriptor["external"]:
                self._external.add(rid)
            if entry is None:
                if descriptor["locator"] is None:
                    raise LookupError(f"transient resource {rid} is missing on this Worker")
                value = await _hook(cls.restore, loads(dumps(descriptor["locator"])))
                if type(value) is not cls:
                    raise TypeError("restore must return the exact resource class")
                await self._validate_instance(value, descriptor)
                entry = _Entry(
                    value, description(descriptor), delete_external=rid not in self._external
                )
                self.entries[rid] = entry
                # Closure during an async restore cannot grant a new borrow.
                if context.closed or self.closed:
                    entry.pending = True
                    # This is an input restoration, never an allocation. Losing
                    # the borrow cannot grant deletion of its owner's object.
                    entry.delete_external = False
                    await self._dispose(rid, entry)
                    raise RuntimeError("resource context closed during restore")
            if (
                entry.pending
                or type(entry.value) is not cls
                or not same_identity(entry.descriptor, descriptor)
            ):
                raise RuntimeError(
                    f"resource {rid} is pending disposal or has conflicting identity"
                )
            if cached:
                await self._validate_instance(entry.value, descriptor)
            if rid in self._external:
                entry.descriptor["external"] = True
                entry.delete_external = False
            context._check_open()
            if self.closed:
                raise RuntimeError("resource Store is closed")
            entry.users.add(context)
            context.used.add(rid)
            return entry.value

    async def _dispose(self, rid: str, entry: _Entry) -> str:
        """Called under the ID lock; a failed destructor retains its exact instance."""
        if entry.users:
            return "unconfirmed"
        if not _protocol(type(entry.value)) or entry.delete_external:
            dispose = getattr(entry.value, "dispose", None)
            if dispose is not None:
                await _hook(dispose)
        del self.entries[rid]
        return "disposed"

    async def dispose(
        self, descriptor: JsonObject, *, restore: bool = False, deleted: bool = False
    ) -> JsonObject:
        """Mark before awaiting; local users continue until actual Attempt drainage."""
        descriptor = description(descriptor)
        rid = descriptor["rid"]
        try:
            async with self._locked(rid):
                if descriptor["external"] or deleted:
                    self._external.add(rid)
                entry = self.entries.get(rid)
                if entry is None:
                    if not (restore and descriptor["persistent"] and rid not in self._external):
                        return {"rid": rid, "status": "missing"}
                    cls = self.resolve_resource_class(descriptor["kind"], descriptor=descriptor)
                    value = await _hook(cls.restore, loads(dumps(descriptor["locator"])))
                    if type(value) is not cls:
                        raise TypeError("restore must return the exact resource class")
                    await self._validate_instance(value, descriptor)
                    entry = _Entry(value, descriptor, pending=True)
                    self.entries[rid] = entry
                if not same_identity(entry.descriptor, descriptor):
                    raise ValueError("cleanup description conflicts with registered identity")
                entry.pending = True
                if rid in self._external:
                    entry.descriptor["external"] = True
                    entry.delete_external = False
                return {"rid": rid, "status": await self._dispose(rid, entry)}
        except Exception as error:
            logger.warning(
                "resource dispose failed rid=%s kind=%s: %s", rid, descriptor["kind"], error
            )
            return {"rid": rid, "status": "unconfirmed", "error": str(error)}

    def _protect_orphan_aliases(self, context: "ResourceContext", orphans: set[str]) -> None:
        """Set deletion rights before any destructor can suspend or remove an alias.

        Unused input authorizations still protect their entities without a local
        get(). Pure orphan batches retain one deletion owner per (kind, locator);
        every nonpersistent wrapper still closes its own local connection.
        Local yielding does not mark a descriptor external: pending aliases that
        yielded cannot protect a later orphan that must take over deletion.
        """
        survivors = [d for rid, d in context.allowed.items() if rid not in orphans]
        survivors.extend(
            entry.descriptor
            for rid, entry in self.entries.items()
            if rid not in orphans
            and (
                not entry.pending
                or rid in self._external
                or (entry.delete_external and _protocol(type(entry.value)))
            )
        )
        deletion_owners: list[JsonObject] = []
        for rid, entry in self.entries.items():
            d = entry.descriptor
            if rid not in orphans or d["locator"] is None:
                continue
            if any(
                other["kind"] == d["kind"]
                and axcutor_core.literal_equal(dumps(other["locator"]), dumps(d["locator"]))
                for other in (*survivors, *deletion_owners)
            ):
                entry.delete_external = False
            elif _protocol(type(entry.value)):
                deletion_owners.append(d)

    async def release(self, context: "ResourceContext", orphans: set[str]) -> None:
        # Publish the complete orphan batch before alias adjudication or any ID
        # lock can suspend, so concurrent exits cannot protect each other forever.
        for rid in orphans:
            entry = self.entries.get(rid)
            if entry is not None:
                entry.pending = True
        self._protect_orphan_aliases(context, orphans)
        for rid in context.used:
            async with self._locked(rid):
                entry = self.entries.get(rid)
                if entry is not None:
                    entry.users.discard(context)
        # One destructor attempt per ID per round, after removing all local uses.
        pending = [
            entry.descriptor for entry in self.entries.values() if entry.pending and not entry.users
        ]
        for descriptor in pending:
            entry = self.entries.get(descriptor["rid"])
            if entry is not None and descriptor["rid"] not in context.cleanup_attempted:
                context.cleanup_attempted.add(descriptor["rid"])
                await self.dispose(descriptor)

    async def shutdown(self) -> None:
        self.closed = True
        for entry in list(self.entries.values()):
            if _protocol(type(entry.value)) and not entry.pending:
                entry.delete_external = False
            entry.pending = True
            await self.dispose(entry.descriptor, deleted=not entry.delete_external)


class ResourceContext:
    """One Attempt's authority and allocation ledger.

    Finalization revokes new access; local users remain until actual drainage.
    """

    def __init__(
        self,
        store: ResourceStore,
        allowed: list[JsonObject],
        locator_schemas: JsonObject | None = None,
    ) -> None:
        self.store = store
        self.allowed = {d["rid"]: description(d) for d in allowed}
        if len(self.allowed) != len(allowed):
            raise ValueError("duplicate resource authorization")
        self.allocated_rids: set[str] = set()
        self.used: set[str] = set()
        self.closed = False
        self.drained = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.cleanup_attempted: set[str] = set()
        self.locator_schemas = locator_schemas or {}

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("resource context is closed")

    async def put(self, value: T) -> Handle[T]:
        self._check_open()
        kind = self.store._class_kind(value)
        persistent = _protocol(type(value))
        locator = await _hook(value.locator) if hasattr(value, "locator") else None
        locator = loads(dumps(locator))
        if persistent and locator is None:
            raise ValueError("persistent resources require a nonempty locator")
        self._check_open()
        if self.store.closed:
            raise RuntimeError("resource Store is closed")
        if self.store._class_kind(value) != kind or _protocol(type(value)) != persistent:
            raise ValueError("resource protocol changed while registering")
        if any(entry.value is value for entry in self.store.entries.values()):
            raise ValueError("the same instance cannot be registered twice")
        rid = "res_" + uuid4().hex
        descriptor = {
            "rid": rid,
            "kind": kind,
            "locator": locator,
            "persistent": persistent,
            "external": False,
            "creator_queue": self.store.worker_queue,
            "resident_queues": [self.store.worker_queue],
        }
        # This commit never suspends: Store, use, and orphan responsibility agree
        # even when cancellation arrives before the caller receives the Handle.
        self.store.entries[rid] = _Entry(value, descriptor, {self})
        self.allowed[rid] = descriptor
        self.allocated_rids.add(rid)
        self.used.add(rid)
        return Handle(rid=rid, kind=kind)

    async def get(self, handle: Handle[T]) -> T:
        self._check_open()
        if not isinstance(handle, Handle):
            raise TypeError("get requires the public Handle class")
        descriptor = self.allowed.get(handle.rid)
        if descriptor is None or descriptor["kind"] != handle.kind:
            raise PermissionError("resource is not authorized for this Attempt")
        return await self.store.get(self, descriptor)

    def exported(self, output: JsonObject, schema: JsonObject) -> set[str]:
        found = scan(output, schema)
        if any(self.store.entries[rid].pending for rid in self.used):
            raise ValueError("resource authorization was revoked before result handoff")
        for rid, kind in found.items():
            descriptor = self.allowed.get(rid)
            if descriptor is None or descriptor["kind"] != kind:
                raise ValueError("output resource is not an input or allocation of this Attempt")
            if rid in self.allocated_rids:
                entry = self.store.entries.get(rid)
                if entry is None or entry.pending or self.store._class_kind(entry.value) != kind:
                    raise ValueError("allocated output resource is no longer valid")
                locator_schema = self.locator_schemas.get(kind)
                if locator_schema is not None:
                    if descriptor["locator"] is None:
                        raise ValueError("public resource requires a locator")
                    axcutor_core.validate_literal(
                        dumps(descriptor["locator"]), dumps(locator_schema)
                    )
        return set(found)

    def metadata(self, exported: set[str]) -> JsonObject:
        return {
            "created": [
                description(self.allowed[rid])
                for rid in self.allowed
                if rid in exported and rid in self.allocated_rids
            ],
            "worker_queue": self.store.worker_queue,
            "resident_rids": [
                rid
                for rid in self.allowed
                if (rid not in self.allocated_rids or rid in exported) and rid in self.store.entries
            ],
        }

    async def close(self, exported: set[str]) -> None:
        self.closed = True
        await self.store.release(self, self.allocated_rids - exported)


# A shared loop factory only observes tasks created with an active Attempt.
# Reference counting lets overlapping Activities restore the previous host
# factory after the last scoped invocation, including delayed drainage.
_trackers: dict[asyncio.AbstractEventLoop, tuple[Any, int]] = {}


@contextmanager
def bind_context(context: ResourceContext) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    previous, count = _trackers.get(loop, (loop.get_task_factory(), 0))
    if count == 0:

        def factory(
            owner_loop: asyncio.AbstractEventLoop,
            coro: Any,
            **kwargs: Any,
        ) -> asyncio.Task[Any]:
            task = (
                previous(owner_loop, coro, **kwargs)
                if previous is not None
                else asyncio.Task(coro, loop=owner_loop, **kwargs)
            )
            supplied: Context | None = kwargs.get("context")
            owner = supplied.get(_current) if supplied is not None else _current.get()
            if owner is not None:
                owner.tasks.add(task)
            return task

        loop.set_task_factory(factory)
    _trackers[loop] = previous, count + 1
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
        previous, count = _trackers[loop]
        if count == 1:
            loop.set_task_factory(previous)
            del _trackers[loop]
        else:
            _trackers[loop] = previous, count - 1


class _Resources:
    def _context(self) -> ResourceContext:
        context = _current.get()
        if context is None:
            raise RuntimeError("resources are only available inside an Activity Attempt")
        return context

    async def put(self, value: T) -> Handle[T]:
        return await self._context().put(value)

    async def get(self, handle: Handle[T]) -> T:
        return await self._context().get(handle)


resources = _Resources()


# Python also exposes imported submodules on their package. These delegates
# preserve the public context API if the submodule was imported first.
async def put(value: T) -> Handle[T]:
    return await resources.put(value)


async def get(handle: Handle[T]) -> T:
    return await resources.get(handle)
