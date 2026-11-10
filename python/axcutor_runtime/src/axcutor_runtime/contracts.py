"""Generate colocated contracts from compiler-normalized module node schemas.

All destinations and collisions are checked before writing. Discovery and code
generation never import implementation modules or execute package initializers.
Only reachable workflow packages contribute contracts, matching compile_bundle.
"""

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .builtin import synthesize_contract_module
from .bundle import compile_bundle
from .codec import JsonObject, loads
from .packages import PackageMount, resolve_mounts


def _runs(root: JsonObject) -> Iterator[JsonObject]:
    body = root["body"]
    if body["kind"] == "run":
        yield {
            **body,
            "output": body["output"] if "output" in body else root["output"],
            "branches": body["branches"] if "branches" in body else root["branches"],
        }
    elif body["kind"] == "graph":
        for node in body["graph"]["nodes"].values():
            yield from _runs(node)


def generate_contracts(
    entry: str,
    snapshot: Mapping[str, Any],
    mounts: Sequence[PackageMount],
    *,
    check: bool = False,
    module_mounts: Mapping[str, str] | None = None,
) -> list[Path]:
    """Write deterministic module stubs or check them; conflicting contracts fail.

    snapshot is the root map from snapshot_mounts/load_mounts, including static
    resource headers and executor policies. mounts supplies physical destinations.
    References resolving to the same implementation file must share their complete
    normalized contract, including declaration order/branches. Template targets
    cannot shadow Python packages. No implementation files are created or modified.
    """
    roots = resolve_mounts(mounts)
    bundle = loads(compile_bundle(entry, snapshot))
    planned: dict[Path, str] = {}
    implementations: dict[Path, str] = {}
    for owner, root in bundle["workflows"].items():
        for body in _runs(root):
            if body["run"]["executor"] != "module":
                continue
            name, *tail = owner.split(".")
            if name not in roots:
                raise ValueError(f"no physical mount for workflow {owner!r}")
            directory = roots[name].joinpath(*tail)
            local = body["run"]["content"]
            parts = [] if local == "." else local[1:].split(".")
            implementation = (
                directory.joinpath(*parts[:-1], parts[-1] + ".py")
                if parts
                else directory / "__init__.py"
            )
            implementation = implementation.resolve()
            destination = (
                directory.joinpath(*parts[:-1], f"{parts[-1]}_contract.py")
                if parts
                else directory / "_contract.py"
            )
            if not destination.resolve().is_relative_to(roots[name]):
                raise ValueError(f"contract destination escapes its mount: {destination}")
            if any(path.is_symlink() for path in (destination, *destination.parents)):
                raise ValueError(f"contract destination must not contain symlinks: {destination}")
            if (destination.with_suffix("") / "__init__.py").is_file():
                raise ValueError(f"contract target conflicts with a Python package: {destination}")
            source = synthesize_contract_module(
                body["input_schema"], body["output"], body["branches"], module_mounts=module_mounts
            )
            if implementation in implementations and implementations[implementation] != source:
                raise ValueError(
                    f"conflicting module contracts for implementation: {implementation}"
                )
            implementations[implementation] = source
            if destination in planned and planned[destination] != source:
                raise ValueError(f"conflicting module contracts: {destination}")
            planned[destination] = source
    if implementations.keys() & planned.keys():
        raise ValueError("generated contract conflicts with a module implementation")
    if check:
        stale = [
            path
            for path, source in planned.items()
            if not path.is_file() or path.read_text("utf-8") != source
        ]
        if stale:
            raise ValueError("missing or stale contracts: " + ", ".join(map(str, stale)))
    else:
        for path, source in planned.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
    return list(planned)
