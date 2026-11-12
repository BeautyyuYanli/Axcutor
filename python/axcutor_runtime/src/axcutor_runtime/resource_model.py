"""Deterministic resource identity and schema-guided scans, safe during replay.

Only declared handle positions carry authority. Locators and ordinary objects
are never traversed as resources. Descriptions cross boundaries as JSON copies;
residencies may grow, but identity/protocol fields cannot be rewritten.
"""

import re
from collections.abc import Callable, Iterator
from copy import deepcopy
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


def has_handles(symbols: JsonObject) -> bool:
    return bool(handle_kinds(symbols))


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


def map_handles(
    values: JsonObject, symbols: JsonObject, convert: Callable[[JsonObject, str], JsonObject]
) -> JsonObject:
    """Copy values, transforming only schema-declared non-null handle slots."""

    def visit(value: Any, schema: JsonObject) -> Any:
        if value is None:
            return None
        base = base_type(schema)
        if base == "handle":
            return convert(value, schema["kind"])
        if base == "array":
            return [visit(item, schema["items"]) for item in value]
        if base == "object":
            return {
                key: visit(
                    item,
                    schema["properties"][key]
                    if "properties" in schema
                    else schema["additionalProperties"],
                )
                for key, item in value.items()
            }
        return deepcopy(value)

    return {
        key: visit(value, symbols[key]) if key in symbols else deepcopy(value)
        for key, value in values.items()
    }


def handle_kinds(symbols: JsonObject) -> set[str]:
    """Kinds needed by an output contract, without inspecting runtime values."""
    result: set[str] = set()

    def visit(schema: JsonObject) -> None:
        if base_type(schema) == "handle":
            result.add(schema["kind"])
        for child in schema.get("properties", {}).values():
            visit(child)
        for key in ("items", "additionalProperties"):
            if key in schema:
                visit(schema[key])

    for schema in symbols.values():
        visit(schema)
    return result


def pool(queue: str) -> str:
    match = _QUEUE.fullmatch(queue)
    if match is None:
        raise ValueError(f"invalid resource Worker queue: {queue!r}")
    return match[1]


def description(value: JsonObject) -> JsonObject:
    if (
        not isinstance(value, dict)
        or value.keys()
        != {"rid", "kind", "locator", "persistent", "creator_queue", "external", "resident_queues"}
        or not isinstance(value["rid"], str)
        or not value["rid"]
        or not isinstance(value["kind"], str)
        or not valid_kind(value["kind"])
        or (value["persistent"] is not None and type(value["persistent"]) is not bool)
        or type(value["external"]) is not bool
        or ((value["persistent"] is None) != (value["creator_queue"] is None))
        or (value["persistent"] is None and not value["external"])
        or ((value["persistent"] or value["external"]) and value["locator"] is None)
        or (value["creator_queue"] is not None and not isinstance(value["creator_queue"], str))
        or not isinstance(value["resident_queues"], list)
        or any(not isinstance(q, str) for q in value["resident_queues"])
    ):
        raise ValueError("invalid resource description")
    if value["creator_queue"] is not None:
        pool(value["creator_queue"])
    for queue in value["resident_queues"]:
        pool(queue)
    return loads(dumps(value))


def same_identity(left: JsonObject, right: JsonObject) -> bool:
    return axcutor_core.literal_equal(
        dumps({k: v for k, v in left.items() if k not in {"resident_queues", "external"}}),
        dumps({k: v for k, v in right.items() if k not in {"resident_queues", "external"}}),
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
    """Run-local canonical identity and cleanup responsibility, without slot counts.

    Public (kind, locator) pairs have one active surrogate ID. Duplicate wrappers
    remain pending under their own IDs until eviction. Run ownership coordinates
    residencies independently of the descriptor's external deletion permission.
    """

    def __init__(self, resources: JsonObject | None = None, run_id: str = "") -> None:
        self.entries: dict[str, RegistryEntry] = {}
        self.sticky: dict[str, str] = {}
        self.resources = resources or {}
        self._run_id = run_id
        self._external_sequence = 0

    def _canonical(self, kind: str, locator: Any) -> str | None:
        if self.resources.get(kind, {}).get("locator") is None:
            return None
        for rid, entry in self.entries.items():
            if (
                not entry.pending
                and entry.descriptor["kind"] == kind
                and axcutor_core.literal_equal(dumps(entry.descriptor["locator"]), dumps(locator))
            ):
                return rid
        return None

    def validate_external_values(self, values: JsonObject, symbols: JsonObject) -> None:
        """Read-only rejection of locators whose internal entity is being deleted."""

        def check(value: JsonObject, kind: str) -> JsonObject:
            for entry in self.entries.values():
                d = entry.descriptor
                if (
                    entry.pending
                    and not d["external"]
                    and d["kind"] == kind
                    and axcutor_core.literal_equal(dumps(d["locator"]), dumps(value["locator"]))
                ):
                    raise ValueError("resource locator is pending external deletion")
            return value

        map_handles(values, symbols, check)

    def materialize_external_inputs(self, values: JsonObject, symbols: JsonObject) -> JsonObject:
        """Synchronous materialization; callers attach GC roots before yielding."""
        self.validate_external_values(values, symbols)

        def convert(value: JsonObject, kind: str) -> JsonObject:
            locator = value["locator"]
            rid = self._canonical(kind, locator)
            if rid is None:
                self._external_sequence += 1
                rid = f"res_ext_{self._run_id}_{self._external_sequence}"
                self.entries[rid] = RegistryEntry(
                    description(
                        {
                            "rid": rid,
                            "kind": kind,
                            "locator": locator,
                            "persistent": None,
                            "creator_queue": None,
                            "external": True,
                            "resident_queues": [],
                        }
                    ),
                    True,
                )
            return {"$handle": rid, "kind": kind}

        return map_handles(values, symbols, convert)

    def project_values(self, values: JsonObject, symbols: JsonObject) -> JsonObject:
        """Project now, while descriptors still exist; history stores no private IDs."""

        def convert(value: JsonObject, kind: str) -> JsonObject:
            if self.resources.get(kind, {}).get("locator") is None:
                return {"$handle": kind}
            d = self.entries[value["$handle"]].descriptor
            return {"$handle": kind, "locator": deepcopy(d["locator"])}

        return map_handles(values, symbols, convert)

    def dematerialize_external_outputs(self, result: JsonObject, symbols: JsonObject) -> JsonObject:
        return {
            "branch": result["branch"],
            "output": self.project_values(result["output"], symbols),
        }

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
    ) -> dict[str, str]:
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
        # The caller's public resource table is authoritative even for custom
        # executors that did not use ResourceContext on the Worker.
        for d in created.values():
            schema = self.resources.get(d["kind"], {}).get("locator")
            if schema is not None:
                if d["locator"] is None:
                    raise ValueError("public resource requires a locator")
                axcutor_core.validate_literal(dumps(d["locator"]), dumps(schema))
        aliases: dict[str, str] = {}
        for rid, d in created.items():
            canonical = self._canonical(d["kind"], d["locator"])
            if canonical is not None:
                aliases[rid] = canonical
                # Keep duplicate wrappers under their original IDs for eviction;
                # their shared external entity belongs to the canonical entry.
                d["external"] = True
            self.entries[rid] = RegistryEntry(d, True, pending=canonical is not None)
        for residency in residences:
            queue = residency["worker_queue"]
            for rid in residency["resident_rids"]:
                entry = self.entries.get(rid)
                if entry is not None and queue not in entry.descriptor["resident_queues"]:
                    entry.descriptor["resident_queues"].append(queue)
        return aliases

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
