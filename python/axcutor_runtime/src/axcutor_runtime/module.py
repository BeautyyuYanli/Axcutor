"""Preinstalled Python implementations, loaded only in Activity attempts.

Module nodes carry logical owners and local specs, without implementation source
or filesystem paths. Builtin nodes retain their separate inline source contract.
Worker-local root mappings are copied once and cannot be changed by submissions.
Import caches belong to Python/the host; each invocation owns a fresh input and
its business task. Cancellation drains that task; an uncooperative task can outlive
remote waiting but retains its local resource leases until actual drainage.
"""

import inspect
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import TYPE_CHECKING

import axcutor_core
from pydantic import BaseModel, ValidationError

from .builtin import _output_validation, compute_contract_sha256, execute_python
from .codec import JsonObject
from .packages import local_module_spec, package_path
from .python_loader import load_module, module_mounts, python_module

if TYPE_CHECKING:
    from .activities import AbortSignal, Executor


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
    fixed = module_mounts(mounts)

    async def execute(request: JsonObject, signal: "AbortSignal") -> JsonObject:
        owner = request["workflow_id"]
        content = request["run_spec"]["content"]
        if not package_path(owner) or not local_module_spec(content):
            raise axcutor_core.ContractError("module requires a named owner and local module path")
        package = python_module(owner, fixed)
        target = package if content == "." else package + content
        signal.raise_if_aborted()
        module = load_module(target, current_package=content == ".")
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
