# === AUTO-GENERATED AXCUTOR CONTRACT MODULE ===
# mypy: warn_unused_ignores=False
import math
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

class Input(_ax_FrozenModel):
    doubled: _ax_builtins.list[_ax_typing.Annotated[_ax_pydantic.StrictInt, _ax_pydantic.BeforeValidator(_ax_integer)]]

class Output(_ax_StrictModel):
    _branch: _ax_typing.ClassVar[_ax_builtins.str | None] = None
    total: _ax_pydantic.StrictInt

ReturnType: _ax_typing.TypeAlias = Output

__axcutor_contract_sha256__: str = '599ff6f809a9c081e333e57193b2dab6d95581d3b4bafd9692a524fdc2664fbc'

__all__ = ['Input', 'Output', 'ReturnType']
