"""Worker-only Python loading shared by executors and resource classes.

Mappings are closed when supplied, bijective at roots, and never import code
during construction. Every import rejects package/file ambiguity first.
"""

import importlib
import importlib.util
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from .packages import package_path, package_segment


def module_mounts(mounts: Mapping[str, str] | None) -> dict[str, str] | None:
    fixed = None if mounts is None else dict(mounts)
    if fixed is not None:
        for root, target in fixed.items():
            if not package_path(root, public=True) or "." in root:
                raise ValueError(f"invalid module mount root: {root!r}")
            if not isinstance(target, str) or not all(
                package_segment(part) for part in target.split(".")
            ):
                raise ValueError(f"invalid Python package mount: {target!r}")
        targets = list(fixed.values())
        if any(
            left == right or left.startswith(right + ".") or right.startswith(left + ".")
            for index, left in enumerate(targets)
            for right in targets[index + 1 :]
        ):
            raise ValueError("Python package mounts must not overlap or alias the same root")
    return fixed


def python_module(logical: str, mounts: Mapping[str, str] | None) -> str:
    root, separator, suffix = logical.partition(".")
    if mounts is not None and root not in mounts:
        raise LookupError(f"no Python package mounted for {root!r}")
    return (mounts[root] if mounts is not None else root) + separator + suffix


def logical_module(physical: str, mounts: Mapping[str, str] | None) -> str:
    if mounts is None:
        return physical
    for root, target in mounts.items():
        if physical == target or physical.startswith(target + "."):
            return root + physical[len(target) :]
    raise LookupError(f"no logical root mounted for {physical!r}")


def load_module(target: str, *, current_package: bool | None = None) -> ModuleType:
    """None permits either layout at the last component (resource modules)."""
    parts = target.split(".")
    for index in range(1, len(parts) + 1):
        name = ".".join(parts[:index])
        spec = importlib.util.find_spec(name)
        if spec is None:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        is_package = spec.submodule_search_locations is not None
        if spec.origin and spec.origin not in ("built-in", "frozen"):
            origin = Path(spec.origin)
            package = origin.parent if is_package else origin.with_suffix("")
            if package.with_suffix(".py").is_file() and (package / "__init__.py").is_file():
                raise ImportError(f"ambiguous Python package and module: {name!r}")
        expected = True if index < len(parts) else current_package
        if expected is not None and is_package != expected:
            raise ImportError(f"module target has incompatible package/file layout: {name!r}")
    return importlib.import_module(target)
