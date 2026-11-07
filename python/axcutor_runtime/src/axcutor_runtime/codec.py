"""JSON text is the Temporal payload; Decimal and dict preserve values and order.

Arithmetic belongs to Rust. Decimal is only a lossless transport representation;
its process/thread precision context must never affect Workflow computations.
RawJSON preserves finite numbers outside Decimal's exponent/Python's digit range
without changing interpreter globals or encoding numbers as JSON strings.
"""

from decimal import Context, Decimal, InvalidOperation
from typing import Any

import simplejson

JsonObject = dict[str, Any]


def _decimal(text: str) -> Decimal | simplejson.RawJSON:
    try:
        # Constructor errors must not depend on a host task's Decimal traps or
        # set flags in its context. Precision never rounds this constructor.
        return Decimal(text, context=Context(traps=[InvalidOperation]))
    except InvalidOperation:
        return simplejson.RawJSON(text)


def _integer(text: str) -> int | simplejson.RawJSON:
    return int(text) if len(text) <= 4000 else simplejson.RawJSON(text)


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def loads(text: str) -> Any:
    """Decode finite JSON, rejecting duplicates rather than silently overwriting."""
    return simplejson.loads(
        text, parse_float=_decimal, parse_int=_integer, object_pairs_hook=_unique_object
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not a JSON number")
        # Decimal('1e0') otherwise encodes as 1 and changes number to integer.
        if value.as_tuple().exponent == 0:
            parts = value.as_tuple()
            return Decimal((parts.sign, (*parts.digits, 0), -1))
    if type(value) is int and value.bit_length() > 13000:
        return simplejson.RawJSON(str(Decimal(value)))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def dumps(value: Any) -> str:
    """Keep native number categories, rejecting non-finite floats and Decimals."""
    return simplejson.dumps(
        _json_ready(value), use_decimal=True, allow_nan=False, separators=(",", ":")
    )
