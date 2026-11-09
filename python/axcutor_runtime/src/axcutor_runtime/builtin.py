"""Trusted Python executor and deterministic, standalone Pydantic v2 source synthesis.

Workflow code never imports this module. Each builtin attempt gets a fresh module
namespace and copied input; generated globals and mutable containers cannot leak
into another attempt. Source generation preserves schema order and never executes user content.
The shared Activity owns deadlines, heartbeat, cancellation and final Rust checks.
execute_python shares the loaded Python invocation lifecycle with module.py;
loading and executor-specific error diagnostics remain at their entry points.
"""

import asyncio
import hashlib
import textwrap
from collections.abc import Callable, Coroutine, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import axcutor_core
from pydantic import BaseModel, ValidationError

from .codec import JsonObject, dumps
from .concurrency import cancel_and_drain
from .python_loader import module_mounts as validate_mounts
from .python_loader import python_module
from .resources import Handle

if TYPE_CHECKING:
    from .activities import AbortSignal


# Pure-value contracts remain standalone. Handle contracts add the shared public
# type below; resource-class imports are only needed by static type checking.
_PRELUDE = """import math
import builtins as _ax_builtins
import typing as _ax_typing
import pydantic as _ax_pydantic
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, TypeAlias
from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictBool, StrictInt, StrictStr
from simplejson import RawJSON

_UNSET: Any = object()

def _coerce_input_float(value: Any) -> float:
    if isinstance(value, RawJSON):
        value = float(value.encoded_json)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("number requires int, float or Decimal")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("number is outside the finite float range") from error
    if not math.isfinite(result):
        raise ValueError("number must be finite")
    return result

def _validate_output_number(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("number requires int, float or Decimal")
    if isinstance(value, int):
        return value
    return _coerce_input_float(value)

def _coerce_transport_integer(value: Any) -> Any:
    if isinstance(value, RawJSON):
        text = value.encoded_json
        if any(c in text for c in ".eE"):
            raise ValueError("integer requires an integer token")
        return int(Decimal(text))
    return value

def _numeric_key(text: str) -> tuple[bool, str, int]:
    negative = text.startswith("-")
    mantissa, _, exponent = text.lstrip("-").lower().partition("e")
    fraction = len(mantissa.partition(".")[2])
    digits = mantissa.replace(".", "")
    trailing = len(digits) - len(digits.rstrip("0"))
    digits = digits.lstrip("0").rstrip("0")
    if not digits:
        return False, "", 0
    return negative, digits, int(Decimal(exponent or "0")) - fraction + trailing

def _enum_validator(base: str, values: tuple[Any, ...]) -> Any:
    def validate(value: Any) -> Any:
        if base in ("number", "integer"):
            if base == "integer":
                value = _coerce_transport_integer(value)
                if type(value) is not int:
                    raise ValueError("integer enum requires int")
            elif isinstance(value, RawJSON):
                _coerce_input_float(value)
            else:
                _validate_output_number(value)
            if isinstance(value, RawJSON):
                text = value.encoded_json
            else:
                text = str(Decimal(value)) if isinstance(value, int) else str(value)
            candidate = _numeric_key(text)
            valid = any(candidate == _numeric_key(item) for item in values)
        else:
            expected = {"string": str, "integer": int, "boolean": bool}[base]
            valid = type(value) is expected and value in values
        if not valid:
            raise ValueError("value does not match the scalar enum")
        return value
    return validate

def _model_instance(model: type[BaseModel]) -> Any:
    def validate(value: Any) -> Any:
        if not isinstance(value, model):
            raise ValueError("nested output requires an explicit " + model.__name__ + " instance")
        return value
    return validate

class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, protected_namespaces=())
    _ax_field_aliases: ClassVar[dict[str, str]] = {}

    if not _ax_typing.TYPE_CHECKING:
        def __getattribute__(self, name: str) -> Any:
            # Aliases are a runtime concern. Exposing this Any-returning hook to
            # a type checker would silently permit undeclared input attributes.
            cls = type(self)
            aliases = type.__getattribute__(cls, "_ax_field_aliases")
            if name in aliases:
                storage = object.__getattribute__(self, "__dict__")
                if aliases[name] in storage:
                    return storage[aliases[name]]
            return super().__getattribute__(name)

class _StrictModel(_FrozenModel):
    pass

_ax_FrozenModel = _FrozenModel
_ax_StrictModel = _StrictModel
_ax_input_number = _coerce_input_float
_ax_output_number = _validate_output_number
_ax_integer = _coerce_transport_integer
_ax_enum = _enum_validator
_ax_instance = _model_instance
_ax_unset = _UNSET

class _ax_BranchNamespace(type):
    if not _ax_typing.TYPE_CHECKING:
        def __getattribute__(cls, name: str) -> Any:
            # Resolve legal names such as '__dict__' at runtime while keeping
            # static attribute access limited to the declared branch aliases.
            try:
                variants = type.__getattribute__(cls, "_ax_variants")
            except AttributeError:
                variants = {}
            if name in variants:
                return variants[name]
            return type.__getattribute__(cls, name)

"""


@dataclass(frozen=True)
class BuiltinModule:
    """Exportable source; content line k is source line line_offset + k (1-based)."""

    source: str
    line_offset: int


def compute_contract_sha256(
    input_schema: JsonObject, output_schema: JsonObject, branches_contract: JsonObject
) -> str:
    """Hash normalized compiler schemas, preserving declaration and branch order."""
    payload = {"input": input_schema, "output": output_schema, "branches": branches_contract}
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


def synthesize_contract_module(
    input_schema: JsonObject,
    output_schema: JsonObject,
    branches_contract: JsonObject,
    *,
    module_mounts: Mapping[str, str] | None = None,
) -> str:
    """Generate standalone Python 3.11+ contracts without evaluating business code.

    Model names follow Input_<path>/Output_<path>; a deterministic suffix resolves
    flattened path collisions. Number enums use Annotated numeric validators because
    Python's Literal type cannot represent fractional numeric literals.
    """
    names = set(branches_contract)
    mounts = validate_mounts(module_mounts)

    def collect(schema: JsonObject) -> None:
        names.update(schema.get("properties", {}))
        for child in schema.get("properties", {}).values():
            collect(child)
        for key in ("items", "additionalProperties"):
            if key in schema:
                collect(schema[key])

    for symbols in (input_schema, output_schema):
        names.update(symbols)
        for schema in symbols.values():
            collect(schema)
    # Generated references use a prefix absent from all DSL names, including
    # nested fields and branches; Python class-body lookups cannot shadow them.
    private = "_ax_"
    while any(name.startswith(private) for name in names):
        private += "_"
    sections = [
        "# === AUTO-GENERATED AXCUTOR CONTRACT MODULE ===\n",
        _PRELUDE.replace("_ax_", private),
    ]
    used = {"Input", "Output", "Branch", "ReturnType"}
    exports = ["Input", "Branch" if branches_contract else "Output", "ReturnType"]
    model_count = 0
    resource_classes: dict[str, str] = {}

    def annotation(schema: JsonObject, path: str, output: bool) -> str:
        nonlocal model_count
        kind = schema["type"]
        nullable = isinstance(kind, list)
        base = next(t for t in kind if t != "null") if nullable else kind
        if base == "handle":
            logical, cls = schema["kind"].split(":")
            if schema["kind"] not in resource_classes:
                alias = f"{private}resource_{len(resource_classes)}"
                resource_classes[schema["kind"]] = alias
                if len(resource_classes) == 1:
                    sections.append(
                        f"from axcutor_runtime.resources import Handle as {private}Handle\n"
                        f"\ndef {private}handle(kind: str, output: bool) -> Any:\n"
                        "    def validate(value: Any) -> Any:\n"
                        f"        if not output and not isinstance(value, {private}Handle):\n"
                        "            if (not isinstance(value, dict)\n"
                        "                    or value.keys() != {'$handle', 'kind'}):\n"
                        "                raise ValueError('invalid handle envelope')\n"
                        f"            value = {private}Handle(\n"
                        "                rid=value['$handle'], kind=value['kind'])\n"
                        f"        if (not isinstance(value, {private}Handle)\n"
                        "                or value.kind != kind):\n"
                        "            raise ValueError('expected public Handle of this kind')\n"
                        "        return value\n"
                        "    return validate\n\n"
                    )
                sections.append(
                    f"if {private}typing.TYPE_CHECKING:\n"
                    f"    from {python_module(logical, mounts)} import {cls} as {alias}\n"
                    "else:\n"
                    f"    {alias} = {private}typing.Any\n\n"
                )
            alias = resource_classes[schema["kind"]]
            result = (
                f"{private}typing.Annotated[{private}Handle[{alias}], "
                f"{private}pydantic.PlainValidator({private}handle({schema['kind']!r}, {output}))]"
            )
        elif base == "object" and "properties" in schema:
            desired = ("Output_" if output else "Input_") + path
            name, index = desired, 2
            while name in used:
                name = f"{desired}_{index}"
                index += 1
            used.add(name)
            exports.append(name)
            model(name, schema["properties"], schema["required"], output, path)
            model_count += 1
            alias = f"{private}type_{model_count}"
            sections.append(f"{alias} = {name}\n\n")
            result = (
                f"{private}typing.Annotated[{alias}, "
                f"{private}pydantic.BeforeValidator({private}instance({alias}))]"
                if output
                else alias
            )
        elif base == "object":
            value_type = annotation(schema["additionalProperties"], path + "_value", output)
            result = f"{private}builtins.dict[{private}pydantic.StrictStr, {value_type}]"
        elif base == "array":
            result = (
                f"{private}builtins.list[{annotation(schema['items'], path + '_item', output)}]"
            )
        else:
            result = {
                "string": f"{private}pydantic.StrictStr",
                "integer": (
                    f"{private}pydantic.StrictInt"
                    if output
                    else f"{private}typing.Annotated[{private}pydantic.StrictInt, "
                    f"{private}pydantic.BeforeValidator({private}integer)]"
                ),
                "boolean": f"{private}pydantic.StrictBool",
                "number": (
                    f"{private}typing.Annotated[{private}builtins.float | {private}builtins.int, "
                    f"{private}pydantic.BeforeValidator({private}output_number)]"
                    if output
                    else f"{private}typing.Annotated[{private}builtins.float, "
                    f"{private}pydantic.BeforeValidator({private}input_number)]"
                ),
            }[base]
            if "enum" in schema:
                values = ", ".join(
                    repr(dumps(value)) if base in ("number", "integer") else repr(value)
                    for value in schema["enum"]
                )
                if base in ("string", "boolean") or (
                    base == "integer"
                    and all(
                        type(value) is int and value.bit_length() < 13000
                        for value in schema["enum"]
                    )
                ):
                    literals = ", ".join(repr(value) for value in schema["enum"])
                    result = f"{private}typing.Literal[{literals}]"
                result = (
                    f"{private}typing.Annotated[{result}, "
                    f"{private}pydantic.BeforeValidator({private}enum({base!r}, ({values},)))]"
                )
        return f"{result} | None" if nullable else result

    def model(
        name: str,
        fields: JsonObject,
        required: list[str],
        output: bool,
        path: str = "",
        branch: str | None = None,
        top: bool = False,
        mapped_fields: dict[str, str] | None = None,
    ) -> None:
        declarations = []
        aliases = {}
        parameters = []
        if top:
            branch_type = (
                f"{private}builtins.str" if branch is not None else f"{private}builtins.str | None"
            )
            metadata = f"_branch: {private}typing.ClassVar[{branch_type}] = {branch!r}"
            if "_branch" in fields:
                declarations.extend(
                    [
                        f"    if not {private}typing.TYPE_CHECKING:",
                        f"        {metadata}",
                    ]
                )
            else:
                declarations.append(f"    {metadata}")
        for index, (field, schema) in enumerate(fields.items()):
            field_path = f"{path}_{field}" if path else field
            mapped = (
                mapped_fields[field]
                if mapped_fields is not None
                else annotation(schema, field_path, output)
            )
            default = ""
            if field not in required:
                if output:
                    default = f" = {private}unset"
                else:
                    mapped = f"{mapped} | None"
                    default = " = None"
            parameters.append(f"{field}: {mapped}{default}")
            if field.startswith("_") or field in dir(BaseModel):
                storage = f"{private.lstrip('_')}field_{index}"
                while storage in fields:
                    storage += "_"
                aliases[field] = storage
                # Some runtime members have no static declaration. Selective type
                # ignores allow aliases either way and suppress only overrides.
                suppression = (
                    "  # type: ignore[pyrefly:bad-override]" if field in dir(BaseModel) else ""
                )
                declarations.extend(
                    [
                        f"    if {private}typing.TYPE_CHECKING:",
                        f"        {field}: {mapped}{suppression}",
                    ]
                )
                argument = f"default={default[3:]}, " if default else ""
                default = f" = {private}pydantic.Field({argument}alias={field!r})"
                field = storage
            declarations.append(f"    {field}: {mapped}{default}")
        if aliases:
            declarations.append(
                f"    {private}field_aliases: "
                f"{private}typing.ClassVar[{private}builtins.dict["
                f"{private}builtins.str, {private}builtins.str]] = {aliases!r}"
            )
            signature = ", ".join(parameters)
            declarations.extend(
                [
                    f"    if {private}typing.TYPE_CHECKING:",
                    f"        def __init__({private}self, /, *, {signature}) -> None: ...",
                ]
            )
        base = private + ("StrictModel" if output else "FrozenModel")
        source = f"class {name}({base}):\n" + "\n".join(declarations or ["    pass"]) + "\n\n"
        sections.append(source)

    model("Input", input_schema, list(input_schema), False)
    if branches_contract:
        # Nested output models are shared across all branch variants.
        field_types = {
            field: annotation(schema, field, True) for field, schema in output_schema.items()
        }
        lines = [
            f"class Branch(metaclass={private}BranchNamespace):\n",
            f"    if {private}typing.TYPE_CHECKING:\n",
        ]
        variants = {}
        for index, (branch, contract) in enumerate(branches_contract.items()):
            fields = {field: output_schema[field] for field in contract["output"]}
            variant = f"{private}branch_{index}"
            model(
                variant,
                fields,
                list(fields),
                True,
                branch=branch,
                top=True,
                mapped_fields=field_types,
            )
            variants[branch] = variant
            if branch == "__dict__":
                # Pyrefly treats an alias named __dict__ as object storage. A
                # nested class retains the branch constructor and field types.
                lines.append(
                    f"        class {branch}({variant}):  # pyrefly: ignore[bad-override]\n"
                    "            pass\n"
                )
            else:
                lines.append(f"        {branch}: {private}typing.TypeAlias = {variant}\n")
        registry = ", ".join(f"{branch!r}: {variant}" for branch, variant in variants.items())
        lines.append(
            f"    {private}variants: {private}typing.ClassVar["
            f"{private}builtins.dict[{private}builtins.str, "
            f"{private}builtins.type[{private}pydantic.BaseModel]]] = {{{registry}}}\n\n"
        )
        sections.append("".join(lines))
        returns = " | ".join(f"Branch.{branch}" for branch in branches_contract)
    else:
        model("Output", output_schema, list(output_schema), True, top=True)
        returns = "Output"
    sections.append(f"ReturnType: {private}typing.TypeAlias = {returns}\n\n")
    sha = compute_contract_sha256(input_schema, output_schema, branches_contract)
    sections.append(f"__axcutor_contract_sha256__: str = {sha!r}\n\n")
    sections.append(f"__all__ = {exports!r}\n")
    return "".join(sections)


def synthesize_module(
    input_schema: JsonObject,
    output_schema: JsonObject,
    branches_contract: JsonObject,
    content: str,
    *,
    module_mounts: Mapping[str, str] | None = None,
) -> BuiltinModule:
    """Add a trusted builtin function body to the shared contract model source."""
    prefix = synthesize_contract_module(
        input_schema, output_schema, branches_contract, module_mounts=module_mounts
    )
    prefix += "\nfrom axcutor_runtime.resources import Handle, resources\n"
    prefix += "\nasync def execute(input: Input) -> ReturnType:\n"
    # Python recognizes CR/LF physical lines. str.splitlines/textwrap.indent also
    # split Unicode separators that are ordinary characters inside Python strings.
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip(" \t\f"):
        lines.pop(0)
    while lines and not lines[-1].strip(" \t\f"):
        lines.pop()
    body = textwrap.dedent("\n".join(lines))
    indented = "\n".join("    " + line if line else "" for line in body.split("\n"))
    return BuiltinModule(prefix + indented + "\n", prefix.count("\n"))


def _message(error: Exception, filename: str, line_offset: int) -> str:
    """Only generated user frames contribute relative run.content line numbers."""
    lines = []
    if isinstance(error, SyntaxError) and error.filename == filename and error.lineno:
        lines.append(max(1, error.lineno - line_offset))
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_filename == filename and frame.tb_lineno > line_offset:
            lines.append(frame.tb_lineno - line_offset)
        frame = frame.tb_next
    location = " -> ".join(f"run.content line {line}" for line in lines)
    return f"{location}: {type(error).__name__}: {error}" if location else str(error)


def _export(value: Any, unset: Any) -> Any:
    """Copy models to JSON values, preserving aliases and omitted optional keys.

    Direct storage access also supports legal DSL names such as '__dict__' that
    shadow Python/Pydantic serialization attributes. Rust checks the resulting
    values, including models built with Pydantic's validation bypass APIs.
    """
    if isinstance(value, Handle):
        return {"$handle": value.rid, "kind": value.kind}
    if isinstance(value, BaseModel):
        storage = object.__getattribute__(value, "__dict__")
        return {
            field.alias or name: _export(storage[name], unset)
            for name, field in type(value).model_fields.items()
            if name in storage and storage[name] is not unset
        }
    if isinstance(value, list):
        return [_export(item, unset) for item in value]
    if isinstance(value, dict):
        return {key: _export(item, unset) for key, item in value.items()}
    return value


def _output_validation(error: ValidationError, model: type[BaseModel]) -> bool:
    """Pydantic failures from unrelated business models remain execution errors."""
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_globals.get("__name__") == BaseModel.__module__:
            instance = frame.tb_frame.f_locals.get("self")
            cls = frame.tb_frame.f_locals.get("cls")
            if isinstance(instance, model) or (isinstance(cls, type) and issubclass(cls, model)):
                return True
        frame = frame.tb_next
    return False


async def execute_python(
    request: JsonObject,
    signal: "AbortSignal",
    contract: Mapping[str, Any],
    function: Callable[[Any], Coroutine[Any, Any, Any]],
) -> JsonObject:
    """Invoke one loaded Python implementation with an isolated input and owned tasks.

    A direct caller's AbortSignal also cancels and drains the user coroutine. The
    Activity still owns the outer signal/deadline race and discards late outcomes.
    Business exceptions retain their traceback for the executor's own diagnostics.
    """
    tasks: list[asyncio.Task[Any]] = []
    try:
        signal.raise_if_aborted()
        try:
            input_model = contract["Input"].model_validate(deepcopy(request["input_snapshot"]))
        except ValidationError as error:
            raise axcutor_core.ContractError(str(error)) from error
        signal.raise_if_aborted()
        business = asyncio.create_task(function(input_model))
        aborted = asyncio.create_task(signal.wait())
        tasks = [business, aborted]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        signal.raise_if_aborted()
        result = business.result()
        branches = request["branches_contract"]
        variants = (
            tuple(getattr(contract["Branch"], branch) for branch in branches)
            if branches
            else (contract["Output"],)
        )
        if not isinstance(result, variants):
            raise axcutor_core.ContractError(
                "execute must return this node's Output or Branch instance"
            )
        try:
            return {"branch": type(result)._branch, "output": _export(result, contract["_UNSET"])}
        except (ValueError, TypeError, RecursionError, OverflowError) as error:
            raise axcutor_core.ContractError(str(error)) from error
    finally:
        await cancel_and_drain(tasks)


async def execute_builtin(request: JsonObject, signal: "AbortSignal") -> JsonObject:
    """Load a fresh builtin namespace and report user faults at run.content lines."""
    generated = synthesize_module(
        request["input_schema"],
        request["output_schema"],
        request["branches_contract"],
        request["run_spec"]["content"],
        module_mounts=request.get("module_mounts"),
    )
    filename = "<axcutor-builtin-" + hashlib.sha256(generated.source.encode()).hexdigest() + ">"
    namespace: JsonObject = {"__name__": "__axcutor_builtin__"}
    try:
        signal.raise_if_aborted()
        exec(compile(generated.source, filename, "exec"), namespace)
        return await execute_python(request, signal, namespace, namespace["execute"])
    except ValidationError as error:
        message = _message(error, filename, generated.line_offset)
        if _output_validation(error, namespace["_StrictModel"]):
            raise axcutor_core.ContractError(message) from error
        raise RuntimeError(message) from error
    except axcutor_core.ContractError:
        raise
    except Exception as error:
        raise RuntimeError(_message(error, filename, generated.line_offset)) from error
