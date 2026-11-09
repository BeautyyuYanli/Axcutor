"""Deterministic resource identity and schema-guided scans, safe during replay.

Only declared handle positions carry authority. Locators and ordinary objects
are never traversed as resources. Descriptions cross boundaries as JSON copies;
residencies may grow, but identity/protocol fields cannot be rewritten.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import axcutor_core

from .codec import JsonObject, dumps, loads
from .packages import package_path, package_segment

CLEANUP_SECONDS = 30
STICKY_SECONDS = 5
_QUEUE = re.compile(r"(.+)-worker-[0-9a-f]{32}\Z")


def valid_kind(kind: str) -> bool:
    module, separator, name = kind.partition(":")
    return (
        bool(separator)
        and package_path(module)
        and package_segment(name)
        and not name.startswith("_")
    )


def base_type(schema: JsonObject) -> str:
    value = schema["type"]
    return next(t for t in value if t != "null") if isinstance(value, list) else value


def contains_handle(schema: JsonObject) -> bool:
    return base_type(schema) == "handle" or any(
        contains_handle(child)
        for child in (
            *schema.get("properties", {}).values(),
            *[schema[key] for key in ("items", "additionalProperties") if key in schema],
        )
    )


def has_handles(symbols: JsonObject) -> bool:
    return any(contains_handle(schema) for schema in symbols.values())


def handles(value: Any, schema: JsonObject) -> Iterator[tuple[str, str]]:
    if value is None:
        return
    base = base_type(schema)
    if base == "handle":
        if (
            not isinstance(value, dict)
            or value.keys() != {"$handle", "kind"}
            or not isinstance(value["$handle"], str)
            or not value["$handle"]
            or value["kind"] != schema["kind"]
        ):
            raise ValueError("invalid handle envelope")
        yield value["$handle"], value["kind"]
    elif base == "array":
        for item in value:
            yield from handles(item, schema["items"])
    elif base == "object":
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties"))
            if child is not None:
                yield from handles(item, child)


def scan(values: JsonObject, symbols: JsonObject) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in values.items():
        if key not in symbols:
            continue
        for rid, kind in handles(value, symbols[key]):
            if rid in found and found[rid] != kind:
                raise ValueError("one resource ID cannot have multiple kinds")
            found[rid] = kind
    return found


def pool(queue: str) -> str:
    match = _QUEUE.fullmatch(queue)
    if match is None:
        raise ValueError(f"invalid resource Worker queue: {queue!r}")
    return match[1]


def description(value: JsonObject) -> JsonObject:
    if (
        not isinstance(value, dict)
        or value.keys()
        != {"rid", "kind", "locator", "persistent", "creator_queue", "resident_queues"}
        or not isinstance(value["rid"], str)
        or not value["rid"]
        or not isinstance(value["kind"], str)
        or not valid_kind(value["kind"])
        or type(value["persistent"]) is not bool
        or (value["persistent"] and value["locator"] is None)
        or not isinstance(value["creator_queue"], str)
        or not isinstance(value["resident_queues"], list)
        or any(not isinstance(q, str) for q in value["resident_queues"])
    ):
        raise ValueError("invalid resource description")
    pool(value["creator_queue"])
    for queue in value["resident_queues"]:
        pool(queue)
    return loads(dumps(value))


def same_identity(left: JsonObject, right: JsonObject) -> bool:
    return axcutor_core.literal_equal(
        dumps({k: v for k, v in left.items() if k != "resident_queues"}),
        dumps({k: v for k, v in right.items() if k != "resident_queues"}),
    )


@dataclass
class RegistryEntry:
    descriptor: JsonObject
    owned: bool
    pending: bool = False
    cleaning: bool = False
    deleted: bool = False
    cleaned: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)


class ResourceRegistry:
    """Run-local identity and cleanup responsibility, without slot reference counts."""

    def __init__(self) -> None:
        self.entries: dict[str, RegistryEntry] = {}
        self.sticky: dict[str, str] = {}

    def authorize(self, values: JsonObject, symbols: JsonObject) -> list[JsonObject]:
        result = []
        for rid, kind in scan(values, symbols).items():
            entry = self.entries.get(rid)
            if entry is None or entry.pending or entry.descriptor["kind"] != kind:
                raise ValueError(f"resource {rid} is unknown, revoked or has a conflicting kind")
            result.append(description(entry.descriptor))
        return result

    def borrow(
        self, descriptions: list[JsonObject], values: JsonObject, symbols: JsonObject
    ) -> None:
        allowed = {d["rid"]: description(d) for d in descriptions}
        if len(allowed) != len(descriptions) or scan(values, symbols) != {
            rid: d["kind"] for rid, d in allowed.items()
        }:
            raise ValueError("Child resource authorization must exactly match its input")
        self.entries.update({rid: RegistryEntry(d, False) for rid, d in allowed.items()})

    def accept(
        self, metadata: JsonObject, allowed: list[JsonObject], output: dict[str, str] | None
    ) -> None:
        """Validate the complete handoff before accepting any new ownership."""
        authorized = {d["rid"]: d for d in allowed}
        if (
            not isinstance(metadata, dict)
            or metadata.keys()
            - {"created", "worker_queue", "resident_rids", "residencies", "default_queue"}
            or not {"created", "worker_queue", "resident_rids"} <= metadata.keys()
        ):
            raise ValueError("invalid resource handoff")
        if (
            not isinstance(metadata["created"], list)
            or not isinstance(metadata["resident_rids"], list)
            or any(not isinstance(rid, str) for rid in metadata["resident_rids"])
            or not isinstance(metadata.get("residencies", []), list)
            or (
                metadata.get("default_queue") is not None
                and (
                    not isinstance(metadata["default_queue"], str) or not metadata["default_queue"]
                )
            )
        ):
            raise ValueError("invalid resource handoff fields")
        created_list = [description(d) for d in metadata["created"]]
        created = {d["rid"]: d for d in created_list}
        if len(created) != len(created_list) or created.keys() & authorized.keys():
            raise ValueError("duplicate or borrowed resource in created handoff")
        if output is None:
            if created:
                raise ValueError("failed calls cannot transfer new resources")
        elif set(created) != output.keys() - authorized.keys() or any(
            rid in self.entries or output[rid] != d["kind"] for rid, d in created.items()
        ):
            raise ValueError("new resource handoff does not match the complete output")
        known = {**authorized, **created}
        if output is not None and any(
            rid not in known or known[rid]["kind"] != kind for rid, kind in output.items()
        ):
            raise ValueError("output contains an unauthorized resource")
        residences = list(metadata.get("residencies", []))
        if metadata["worker_queue"] is not None:
            residences.append(
                {
                    "worker_queue": metadata["worker_queue"],
                    "resident_rids": metadata["resident_rids"],
                }
            )
        elif metadata["resident_rids"]:
            raise ValueError("resident IDs require a Worker queue")
        for residency in residences:
            if (
                not isinstance(residency, dict)
                or residency.keys() != {"worker_queue", "resident_rids"}
                or not isinstance(residency["worker_queue"], str)
                or not isinstance(residency["resident_rids"], list)
                or any(not isinstance(rid, str) for rid in residency["resident_rids"])
            ):
                raise ValueError("invalid resource residency")
            pool(residency["worker_queue"])
            if any(rid not in known for rid in residency["resident_rids"]):
                raise ValueError("residency contains an unauthorized resource")
        for rid, d in authorized.items():
            entry = self.entries.get(rid)
            if entry is not None and not same_identity(entry.descriptor, d):
                raise ValueError("resource identity changed across a call")
        self.entries.update({rid: RegistryEntry(d, True) for rid, d in created.items()})
        for residency in residences:
            queue = residency["worker_queue"]
            for rid in residency["resident_rids"]:
                entry = self.entries.get(rid)
                if entry is not None and queue not in entry.descriptor["resident_queues"]:
                    entry.descriptor["resident_queues"].append(queue)

    def route(self, base: str, inputs: list[JsonObject], produces_handles: bool) -> str:
        transient = [d for d in inputs if d["locator"] is None]
        if transient:
            queues = {d["creator_queue"] for d in transient}
            if len(queues) != 1 or pool(next(iter(queues))) != base:
                raise ValueError("transient inputs require the same Worker in the executor pool")
            return next(iter(queues))
        if not inputs and not produces_handles:
            return base
        if base in self.sticky:
            return self.sticky[base]
        for d in inputs:
            for queue in d["resident_queues"]:
                if pool(queue) == base:
                    return queue
        return base

    def metadata(self, exported: set[str]) -> JsonObject:
        residences: dict[str, list[str]] = {}
        for rid, entry in self.entries.items():
            if entry.owned and rid not in exported:
                continue
            for queue in entry.descriptor["resident_queues"]:
                residences.setdefault(queue, []).append(rid)
        return {
            "created": [
                description(entry.descriptor)
                for rid, entry in self.entries.items()
                if entry.owned and rid in exported
            ],
            "worker_queue": None,
            "resident_rids": [],
            "residencies": [
                {"worker_queue": queue, "resident_rids": rids} for queue, rids in residences.items()
            ],
        }
