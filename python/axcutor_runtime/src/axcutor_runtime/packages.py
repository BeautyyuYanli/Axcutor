"""Explicit workflow mounts and pure dotted-name validation.

Snapshots contain only workflow documents. Python implementations are installed
by the host on every Worker and never loaded while discovering or compiling a
snapshot. Mounts are supplied as a sequence so duplicate roots cannot be hidden
by dictionary overwrites. Namespace parents may omit __axcutor__.yaml.
"""

import keyword
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*\Z")


def package_segment(value: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(value)) and not keyword.iskeyword(value)


def package_path(value: str, *, public: bool = False) -> bool:
    parts = value.split(".")
    return (
        not value.startswith("_")
        and all(package_segment(part) and part != "__axcutor__" for part in parts)
        and parts[-1] not in ("yaml", "yml", "json")
        and (not public or all(not part.startswith("_") for part in parts))
    )


def local_module_spec(value: str) -> bool:
    return value == "." or (
        value.startswith(".") and all(package_segment(part) for part in value[1:].split("."))
    )


@dataclass(frozen=True)
class PackageMount:
    """A public logical root and its physical Python package directory."""

    name: str
    directory: Path


def resolve_mounts(mounts: Sequence[PackageMount]) -> dict[str, Path]:
    """Validate one explicit, nonoverlapping root table without importing Python."""
    roots: dict[str, Path] = {}
    for mount in mounts:
        if not package_path(mount.name, public=True) or "." in mount.name:
            raise ValueError(f"mount root must be one public package segment: {mount.name!r}")
        if mount.directory.is_symlink():
            raise ValueError(f"mount requires an ordinary directory: {mount.directory}")
        root = mount.directory.resolve(strict=True)
        if mount.name in roots or any(
            root.is_relative_to(other) or other.is_relative_to(root) for other in roots.values()
        ):
            raise ValueError(f"conflicting package mount: {mount.name!r}")
        initializer = root / "__init__.py"
        if not root.is_dir() or initializer.is_symlink() or not initializer.is_file():
            raise ValueError(f"mount requires a Python package directory: {root}")
        if root.with_name(root.name + ".py").is_file():
            raise ValueError(f"package and module conflict: {root}")
        roots[mount.name] = root
    return roots


def snapshot_packages(mounts: Sequence[PackageMount]) -> dict[str, dict[str, str]]:
    """Read each __axcutor__.yaml once; reject duplicate roots and package aliases.

    Workflow ancestors and documents cannot be symlinks. Every ancestor is an
    ordinary Python package; discovery never imports its __init__.py.
    """
    packages: dict[str, dict[str, str]] = {}
    for root_name, root in resolve_mounts(mounts).items():
        for path in sorted(root.rglob("__axcutor__.yaml")):
            relative = path.parent.relative_to(root)
            name = ".".join((root_name, *relative.parts))
            if not package_path(name):
                raise ValueError(f"invalid workflow package: {name!r}")
            current = path.parent
            while True:
                initializer = current / "__init__.py"
                if current.is_symlink() or initializer.is_symlink() or not initializer.is_file():
                    raise ValueError(f"workflow requires ordinary Python packages: {current}")
                if current.with_name(current.name + ".py").is_file():
                    raise ValueError(f"package and module conflict: {current}")
                if current == root:
                    break
                current = current.parent
            if path.is_symlink() or path.resolve().parent != path.parent:
                raise ValueError(f"workflow document escapes its mount: {path}")
            if name in packages:
                raise ValueError(f"duplicate workflow package: {name}")
            packages[name] = {"format": "yaml", "text": path.read_text(encoding="utf-8")}
    return packages


def load_packages(
    entry: Path, mounts: Sequence[PackageMount] = ()
) -> tuple[str, dict[str, dict[str, str]]]:
    """Snapshot explicit mounts plus a named directory or anonymous entry document."""
    packages = snapshot_packages(mounts)
    entry = entry.resolve(strict=True)
    if entry.is_dir():
        entry = entry / "__axcutor__.yaml"
    if entry.name == "__axcutor__.yaml":
        matches = [
            ".".join((mount.name, *entry.parent.relative_to(mount.directory.resolve()).parts))
            for mount in mounts
            if entry.is_relative_to(mount.directory.resolve())
        ]
        if len(matches) != 1 or matches[0] not in packages:
            raise ValueError("named entry requires exactly one explicit package mount")
        entry_id = matches[0]
        if not package_path(entry_id, public=True):
            raise ValueError("entry must be a public workflow package")
    else:
        formats = {".yaml": "yaml", ".yml": "yaml", ".json": "json"}
        if entry.suffix not in formats:
            raise ValueError("anonymous entry must be YAML or JSON")
        entry_id = "__main__"
        packages[entry_id] = {"format": formats[entry.suffix], "text": entry.read_text("utf-8")}
    return entry_id, packages
