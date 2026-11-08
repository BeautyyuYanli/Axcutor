"""Preinstalled Python implementations, loaded only in Activity attempts.

Module nodes carry logical owners and local specs, without implementation source
or filesystem paths. Builtin nodes retain their separate inline source contract.
Worker-local root mappings are copied once and cannot be changed by submissions.
Import caches belong to Python/the host; each invocation owns a fresh input and
its business task. No executor task survives cancellation or an attempt deadline.
"""

import importlib
import importlib.util
import inspect
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import axcutor_core
from pydantic import BaseModel, ValidationError

from .builtin import _output_validation, compute_contract_sha256, execute_python
from .codec import JsonObject
from .packages import local_module_spec, package_path, package_segment

if TYPE_CHECKING:
    from .activities import AbortSignal, Executor


def _load_module(target: str, *, current_package: bool) -> ModuleType:
    """Reject package/file ambiguity before evaluating each target component.

    Intermediate components and '.' are packages; a local implementation's last
    component is a file module. Host prefix namespace packages remain supported.
    Python owns import caching; no workflow source is read to resolve a module.
    """
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
        if is_package != (index < len(parts) or current_package):
            raise ImportError(f"module target has incompatible package/file layout: {name!r}")
    return importlib.import_module(target)


def _contract(module: ModuleType, request: JsonObject) -> ModuleType:
    expected = compute_contract_sha256(
        request["input_schema"], request["output_schema"], request["branches_contract"]
    )
    input_cls = getattr(module, "Input", None)
    contract = sys.modules.get(getattr(input_cls, "__module__", ""))
    actual = getattr(contract, "__axcutor_contract_sha256__", None)
    output_name = "Branch" if request["branches_contract"] else "Output"
    output_cls = getattr(module, output_name, None)
    strict = getattr(contract, "_StrictModel", None)
    frozen = getattr(contract, "_FrozenModel", None)
    valid = (
        contract is not None
        and isinstance(input_cls, type)
        and isinstance(frozen, type)
        and issubclass(frozen, BaseModel)
        and issubclass(input_cls, frozen)
        and input_cls is getattr(contract, "Input", None)
        and isinstance(output_cls, type)
        and output_cls is getattr(contract, output_name, None)
        # Branch names may legally include __module__; bypass its namespace hook.
        and type.__getattribute__(output_cls, "__module__") == input_cls.__module__
        and isinstance(strict, type)
        and issubclass(strict, BaseModel)
        and hasattr(contract, "_UNSET")
    )
    if valid:
        assert contract is not None and isinstance(strict, type)
        variants = (
            [getattr(output_cls, branch, None) for branch in request["branches_contract"]]
            if request["branches_contract"]
            else [output_cls]
        )
        valid = all(
            isinstance(variant, type)
            and issubclass(variant, strict)
            and variant.__module__ == contract.__name__
            for variant in variants
        )
    if not valid:
        raise axcutor_core.ContractError(
            f"module {module.__name__!r} requires Input and {output_name} "
            "from the same generated contract module"
        )
    if actual != expected:
        raise axcutor_core.ContractError(
            f"module {module.__name__!r} contract SHA mismatch: expected {expected}, got {actual}"
        )
    assert contract is not None
    return contract


def module_executor(mounts: Mapping[str, str] | None = None) -> "Executor":
    """Bind logical roots to installed Python packages (identity by default).

    An explicit table is closed: every referenced root must be present. No module
    imports occur during registry construction, compilation or Workflow replay.
    """
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

    async def execute(request: JsonObject, signal: "AbortSignal") -> JsonObject:
        owner = request["workflow_id"]
        content = request["run_spec"]["content"]
        if not package_path(owner) or not local_module_spec(content):
            raise axcutor_core.ContractError("module requires a named owner and local module path")
        root, separator, suffix = owner.partition(".")
        if fixed is not None and root not in fixed:
            raise LookupError(f"no Python package mounted for {root!r}")
        package = (fixed[root] if fixed is not None else root) + separator + suffix
        target = package if content == "." else package + content
        signal.raise_if_aborted()
        module = _load_module(target, current_package=content == ".")
        function = getattr(module, "execute", None)
        if not inspect.iscoroutinefunction(function):
            raise axcutor_core.ContractError(f"module {target!r} requires async execute")
        contract = _contract(module, request)
        try:
            return await execute_python(request, signal, vars(contract), function)
        except ValidationError as error:
            if _output_validation(error, contract._StrictModel):
                raise axcutor_core.ContractError(str(error)) from error
            raise

    return execute


execute_module = module_executor()
