"""Real PyO3 text APIs, exact JSON transport and structured compile failures."""

from decimal import Decimal, InvalidOperation, localcontext

import axcutor_core
import pytest
from axcutor_runtime import CompilationError, compile_bundle
from axcutor_runtime.codec import dumps, loads


def test_native_kernel_text_and_decimal_round_trip():
    text = '{"2":0.12345678901234567890123456789,"0":999999999999999999999999999999}'
    assert dumps(loads(text)) == text
    assert isinstance(loads(text)["2"], Decimal)
    assert axcutor_core.eval_arg('{"kind":"const","value":' + text + "}", "{}") == text
    assert (
        axcutor_core.validate_input(text, '{"2":{"type":"number"},"0":{"type":"integer"}}') == text
    )
    assert loads(
        axcutor_core.validate_run_contract(
            '{"output":{"n":1,"ignored":false}}', '{"n":{"type":"integer"}}', "{}"
        )
    ) == {"branch": None, "output": {"n": 1}}
    with pytest.raises(axcutor_core.ContractError):
        axcutor_core.validate_run_contract(
            '{"output":{"n":true}}', '{"n":{"type":"integer"}}', "{}"
        )
    with pytest.raises(RuntimeError):
        axcutor_core.eval_arg('{"kind":"path","path":{"root":"unbound","fields":[]}}', "{}")


def test_compile_failure_retains_original_diagnostics():
    mounts = {
        "main": {
            "packages": {"main": {"format": "yaml", "text": "input: {}\noutput: {}\ngraph: {}"}}
        }
    }
    expected_json = axcutor_core.compile_workflow(dumps({"entry": "main", "mounts": mounts}))
    with pytest.raises(CompilationError) as failure:
        compile_bundle("main", mounts)
    assert failure.value.result_json == expected_json
    assert failure.value.diagnostics == loads(expected_json)["diagnostics"]
    assert failure.value.diagnostics


def test_codec_rejects_duplicate_keys_and_nonfinite_values():
    with pytest.raises(ValueError):
        loads('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        dumps(float("inf"))
    for value in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError):
            dumps({"n": value})
    assert dumps(loads("1e0")) == "1.0"
    assert dumps(loads("1e999999999999999999999")) == "1e999999999999999999999"
    enormous = "9" * 5000
    assert dumps(loads(enormous)) == enormous
    assert dumps(10**5000) == "1" + "0" * 5000
    with localcontext() as context:
        context.prec = 2
        context.traps[InvalidOperation] = False
        context.clear_flags()
        assert dumps(loads("1.1234567890123456789")) == "1.1234567890123456789"
        assert dumps(loads("1e999999999999999999999")) == "1e999999999999999999999"
        assert not context.flags[InvalidOperation]
