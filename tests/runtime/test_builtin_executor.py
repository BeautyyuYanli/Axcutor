"""Real compiler, generated Pydantic models, Activity ABI and Temporal boundaries."""

import asyncio
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import axcutor_core
import pytest
from axcutor_runtime import AxcutorInterpreterWorkflow, compile_bundle
from axcutor_runtime.activities import AbortSignal, RunActivities
from axcutor_runtime.builtin import execute_builtin, synthesize_module
from axcutor_runtime.codec import dumps, loads
from helpers import INTEGER, N, S, action, array, graph, inline, mapping
from temporalio import exceptions
from temporalio.testing import ActivityEnvironment

MANUAL = Path(__file__).resolve().parents[2] / "manual/docs"


def request(content, inputs=None, outputs=None, branches=None, values=None):
    document = {
        "input": inputs or {},
        "output": outputs or {},
        "branches": branches or {},
        "run": {"executor": "builtin", "content": content},
    }
    bundle = loads(
        compile_bundle(
            "main", {"main": {"packages": {"main": {"format": "json", "text": dumps(document)}}}}
        )
    )
    root = bundle["workflows"]["main"]
    return {
        "run_spec": root["body"]["run"],
        "input_schema": root["body"]["input_schema"],
        "input_snapshot": values or {},
        "output_schema": root["output"],
        "branches_contract": root["branches"],
        "attempt_timeout_ms": 1000,
        "heartbeat_interval_ms": 10,
    }


async def invoke(payload):
    return loads(await ActivityEnvironment().run(RunActivities().execute_run, dumps(payload)))[
        "result"
    ]


def check_module(filename, generated):
    filename.write_text(generated.source)
    compile(generated.source, str(filename), "exec")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pyrefly",
            "check",
            "--config",
            str(Path(__file__).resolve().parents[2] / "pyproject.toml"),
            "--output-format",
            "min-text",
            "--color",
            "never",
            str(filename),
        ],
        capture_output=True,
        text=True,
    )


async def test_default_builtin_recursively_models_inputs_and_preserves_snapshot():
    record = {
        "type": "object",
        "properties": {"amount": N, "nickname": S, "scores": array(N), "rates": mapping(N)},
        "required": ["amount", "scores", "rates"],
    }
    values = {"user": {"amount": Decimal("1.25"), "scores": [2], "rates": {"2": 3, "0": 4}}}
    original = deepcopy(values)
    payload = request(
        """
        assert isinstance(input.user.amount, float)
        assert input.user.nickname is None
        assert all(type(score) is float for score in input.user.scores)
        assert all(type(rate) is float for rate in input.user.rates.values())
        assert list(input.user.rates) == ["2", "0"]
        input.user.scores.append(99.0)
        return Output(total=input.user.amount + input.user.scores[0])
        """,
        {"user": record},
        {"total": N},
        values=values,
    )
    assert (await invoke(payload))["output"] == {"total": Decimal("3.25")}
    assert values == original
    assert payload["input_snapshot"] == original


@pytest.mark.parametrize(
    ("schema", "expression"),
    [
        (INTEGER, "True"),
        (INTEGER, "1.0"),
        (S, "123"),
        ({"type": "boolean"}, "1"),
        (N, "True"),
        (N, 'float("nan")'),
        (N, 'float("inf")'),
        ({"type": "integer", "enum": [1]}, "True"),
        ({"type": "boolean", "enum": [True]}, "1"),
        ({"type": "number", "enum": [1]}, "True"),
        (array(INTEGER), "(1, 2)"),
        (mapping(INTEGER), "{1: 2}"),
        ({"type": ["string", "null"]}, "_UNSET"),
    ],
)
async def test_strict_output_types_fail_at_activity_contract_boundary(schema, expression):
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(request(f"return Output(value={expression})", outputs={"value": schema}))
    assert failure.value.type == "contract"


@pytest.mark.parametrize("expression", ["7", "7.5", 'Decimal("7.5")'])
async def test_output_number_accepts_int_float_and_decimal(expression):
    assert (await invoke(request(f"return Output(value={expression})", outputs={"value": N})))[
        "output"
    ]["value"] in (7, Decimal("7.5"))


async def test_optional_output_keys_are_orthogonal_to_null_and_nested_dicts_are_rejected():
    record = {
        "type": "object",
        "properties": {"id": INTEGER, "nickname": S, "nullable": {"type": ["string", "null"]}},
        "required": ["id"],
    }
    payload = request(
        "return Output(user=Output_user(id=1, nullable=None))", outputs={"user": record}
    )
    assert (await invoke(payload))["output"] == {"user": {"id": 1, "nullable": None}}
    for content in (
        "return Output(user=Output_user(id=1, nickname=None))",
        'return Output(user={"id": 1})',
        "return Output(user=Output_user(id=1, extra=2))",
    ):
        payload["run_spec"]["content"] = content
        with pytest.raises(exceptions.ApplicationError) as failure:
            await invoke(payload)
        assert failure.value.type == "contract"


@pytest.mark.parametrize(
    "content", ["return None", 'return {"output": {}}', "pass", "return Output()"]
)
async def test_only_current_return_variants_and_required_nullable_fields_are_accepted(content):
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(request(content, outputs={"maybe": {"type": ["string", "null"]}}))
    assert failure.value.type == "contract"


async def test_branch_variants_trim_fields_and_support_empty_subsets():
    payload = request(
        "return Branch.empty()",
        outputs={"value": INTEGER, "reason": S},
        branches={
            "ok": {"description": "kept", "output": ["value"]},
            "empty": {"description": "skip", "output": []},
        },
    )
    assert await invoke(payload) == {"branch": "empty", "output": {}}
    payload["run_spec"]["content"] = "return Branch.ok(value=1)"
    assert await invoke(payload) == {"branch": "ok", "output": {"value": 1}}
    payload["run_spec"]["content"] = 'return Branch.ok(value=1, reason="no")'
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "contract"


@pytest.mark.parametrize(
    ("content", "relative_line"),
    [("\n\n    x = 1\n    raise ValueError('fault')\n\n", 2), ("x = 1\nif :\n    pass", 2)],
)
async def test_business_and_syntax_errors_report_relative_content_lines(content, relative_line):
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(request(content))
    assert failure.value.type == "execution"
    assert f"run.content line {relative_line}" in failure.value.message


def test_generated_module_is_deterministic_standalone_and_statically_typed(tmp_path):
    record = {"type": "object", "properties": {"value": N}, "required": ["value"]}
    payload = request(
        """
        if input.n > 0:
            return Branch.ok(user=Output_user(value=input.n))
        return Branch.skip()
        """,
        inputs={"n": N},
        outputs={"user": record},
        branches={"ok": "kept", "skip": {"description": "skip", "output": []}},
    )
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    generated = synthesize_module(*args, payload["run_spec"]["content"])
    assert generated == synthesize_module(*deepcopy(args), payload["run_spec"]["content"])
    assert generated.source.splitlines()[generated.line_offset] == "    if input.n > 0:"
    filename = tmp_path / "generated.py"
    # Test the public exported source, independent of the executor's loader.
    checked = check_module(filename, generated)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    namespace = {"__name__": "generated_test"}
    exec(compile(generated.source, str(filename), "exec"), namespace)
    result = asyncio.run(namespace["execute"](namespace["Input"](n=1)))
    assert result.user.value == 1


async def test_direct_abort_signal_cancels_builtin_and_discards_swallowed_cancellation():
    signal = AbortSignal()
    payload = request(
        """
        import asyncio
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return Output()
        """
    )
    running = asyncio.create_task(execute_builtin(payload, signal))
    await asyncio.sleep(0.02)
    signal._abort("cancelled")
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_default_worker_executes_builtin_root_and_for_each_in_real_history(execution):
    document = graph(
        {
            "double": action(
                "return Output(results=input.item * 2)",
                ["item"],
                {"results": INTEGER},
                for_each={"source": "items", "item": "item", "concurrency": 2},
            )
        },
        ["start -> double", "double -> end"],
        inputs={"items": array()},
        outputs={"results": array()},
    )
    async with execution() as run:
        assert (await run.execute(document, {"items": [3, 1, 2]}))["output"] == {
            "results": [6, 2, 4]
        }
        root = {
            "input": {"n": N},
            "output": {"total": N},
            "run": {"executor": "builtin", "content": "return Output(total=input.n * 2)"},
        }
        assert (await run.execute(root, {"n": 1.25}))["output"] == {"total": Decimal("2.5")}


async def test_temporal_parent_timeout_waits_for_builtin_cleanup_and_discards_late_output(
    execution, tmp_path
):
    cleanup = tmp_path / "cleaned"
    sub = graph(
        {
            "wait": action(
                "import asyncio\nfrom pathlib import Path\n"
                "try:\n    await asyncio.Event().wait()\n"
                "except asyncio.CancelledError:\n    await asyncio.sleep(0.01)\n"
                "    Path(input.cleanup).write_text('cleaned')\n    return Output(value=999)",
                ["cleanup"],
                {"value": INTEGER},
            )
        },
        ["start -> wait", "wait -> end"],
        outputs={"value": INTEGER},
    )
    document = graph(
        {"scope": inline(sub, ["cleanup"], timeout_ms=150, catch={"timed": ["timeout"]})},
        ["start -> scope", "scope -> end", "scope.timed -> end"],
        inputs={"cleanup": S},
        outputs={"value": INTEGER},
        variables={"value": {**INTEGER, "default": 5}},
    )
    async with execution() as run:
        assert (await run.execute(document, {"cleanup": str(cleanup)}))["output"] == {"value": 5}
        assert cleanup.read_text() == "cleaned"


async def test_activity_rust_check_rejects_model_construct_bypass():
    payload = request('return Output.model_construct(value="wrong")', outputs={"value": INTEGER})
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "contract"


@pytest.mark.parametrize(
    "name",
    [
        "_private",
        "model_config",
        "model_validate",
        "__dict__",
        "__class__",
        "_branch",
        "_ax_builtins",
    ],
)
async def test_legal_identifiers_remain_constructor_parameters_and_attributes(name):
    payload = request(
        f"return Output({name}=input.{name})",
        inputs={name: INTEGER},
        outputs={name: INTEGER},
        values={name: 7},
    )
    assert await invoke(payload) == {"branch": None, "output": {name: 7}}


async def test_input_models_are_frozen_and_unrelated_pydantic_errors_are_business_errors():
    for content in (
        "input.user.name = 'changed'\nreturn Output()",
        "from pydantic import BaseModel\nclass Business(BaseModel):\n"
        "    n: int\ncls = Output\nself = Output()\nBusiness(n='invalid')\nreturn Output()",
    ):
        payload = request(
            content,
            inputs={"user": {"type": "object", "properties": {"name": S}}},
            values={"user": {"name": "original"}},
        )
        with pytest.raises(exceptions.ApplicationError) as failure:
            await invoke(payload)
        assert failure.value.type == "execution"
        assert "run.content line" in failure.value.message


async def test_builtin_activity_deadline_heartbeats_and_cleans_up_user_coroutine(tmp_path):
    cleanup = tmp_path / "cleaned"
    payload = request(
        """
        import asyncio
        from pathlib import Path
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            Path(input.cleanup).write_text("cleaned")
            return Output(value=True)
        """,
        inputs={"cleanup": S},
        outputs={"value": INTEGER},
        values={"cleanup": str(cleanup)},
    )
    payload["attempt_timeout_ms"] = 40
    payload["heartbeat_interval_ms"] = 5
    beats = []
    environment = ActivityEnvironment()
    environment.on_heartbeat = lambda *details: beats.append(details)
    with pytest.raises(exceptions.ApplicationError) as failure:
        await environment.run(RunActivities().execute_run, dumps(payload))
    assert failure.value.type == "timeout"
    assert cleanup.read_text() == "cleaned"
    assert len(beats) >= 2


async def test_builtin_integer_transport_handles_large_integers_without_float_coercion():
    enormous = 10**5000
    payload = request(
        "return Output(n=input.n + 1)", {"n": INTEGER}, {"n": INTEGER}, values={"n": enormous}
    )
    result = await invoke(payload)
    assert dumps(result["output"]["n"]) == dumps(enormous + 1)


async def test_numeric_enums_preserve_exact_transport_values_and_do_not_eagerly_expand_exponents():
    huge = 10**5000
    schema = {"type": "integer", "enum": [huge]}
    payload = request("return Output(n=input.n)", {"n": schema}, {"n": schema}, values={"n": huge})
    assert dumps((await invoke(payload))["output"]["n"]) == dumps(huge)
    payload = request(
        "return Branch.skip()",
        outputs={"n": {"type": "number", "enum": [loads("1e999999999999999999999")]}},
        branches={"keep": "keep", "skip": {"description": "skip", "output": []}},
    )
    assert await invoke(payload) == {"branch": "skip", "output": {}}
    schema = {"type": "number", "enum": [Decimal("0.1"), 2]}
    payload = request(
        "assert type(input.n) is float\nreturn Output(n=input.n)",
        {"n": schema},
        {"n": schema},
        values={"n": Decimal("0.1")},
    )
    assert (await invoke(payload))["output"] == {"n": Decimal("0.1")}


async def test_non_yielding_trusted_code_cannot_commit_an_overdue_result():
    payload = request(
        "import time\ntime.sleep(0.04)\nreturn Output(value=1)", outputs={"value": INTEGER}
    )
    payload["attempt_timeout_ms"] = 10
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(payload)
    assert failure.value.type == "timeout"


@pytest.mark.parametrize("branch", ["__dict__", "__class__", "__slots__", "_StrictModel"])
def test_branch_namespace_supports_all_legal_identifiers(tmp_path, branch):
    payload = request(
        f"return Branch.{branch}()", branches={branch: {"description": "empty", "output": []}}
    )
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    generated = synthesize_module(*args, payload["run_spec"]["content"])
    checked = check_module(tmp_path / "legal_branch.py", generated)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert asyncio.run(invoke(payload)) == {"branch": branch, "output": {}}


def test_generated_branch_rejects_undeclared_variants_statically(tmp_path):
    content = "\n    value = Branch.ok(n=1)\n    return Branch.ok(n=value.n)\n"
    payload = request(content, outputs={"n": INTEGER}, branches={"ok": "kept"})
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    filename = tmp_path / "branch_variants.py"
    valid = synthesize_module(*args, content)
    checked = check_module(filename, valid)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert asyncio.run(invoke(payload)) == {"branch": "ok", "output": {"n": 1}}

    content = content.replace("value = Branch.ok", "value = Branch.typo")
    payload = request(content, outputs={"n": INTEGER}, branches={"ok": "kept"})
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    invalid = synthesize_module(*args, content)
    checked = check_module(filename, invalid)
    assert checked.returncode != 0
    assert f":{invalid.line_offset + 1}:" in checked.stdout
    assert "has no class attribute `typo`" in checked.stdout
    assert "[missing-attribute]" in checked.stdout
    assert checked.stdout.count("ERROR ") == 1, checked.stdout
    with pytest.raises(exceptions.ApplicationError) as failure:
        asyncio.run(invoke(payload))
    assert failure.value.type == "execution"
    assert "run.content line 1: AttributeError" in str(failure.value)


@pytest.mark.parametrize("branch", [None, "ok", "__dict__"])
@pytest.mark.parametrize(
    "names",
    [("_private", "model_config", "__dict__", "_branch"), ("__class__", "__slots__", "_abc_impl")],
)
def test_generated_alias_fields_preserve_static_constructor_checks(tmp_path, branch, names):
    fields = {name: INTEGER for name in names}
    constructor = f"Branch.{branch}" if branch is not None else "Output"
    content = f"return {constructor}(" + ", ".join(f"{name}=input.{name}" for name in fields) + ")"
    payload = request(content, fields, fields, {branch: "kept"} if branch else {})
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    valid = synthesize_module(*args, content)
    filename = tmp_path / "aliases.py"
    checked = check_module(filename, valid)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    corrected_content = f"return {constructor}(" + ", ".join(f"{name}=7" for name in names) + ")"
    invalid = synthesize_module(
        *args, corrected_content.replace(f"{names[0]}=7", f"{names[0]}='wrong'", 1)
    )
    checked = check_module(filename, invalid)
    assert checked.returncode != 0
    assert f":{invalid.line_offset + 1}:" in checked.stdout
    assert f"`{names[0]}`" in checked.stdout
    assert "[bad-argument-type]" in checked.stdout
    assert checked.stdout.count("ERROR ") == 1, checked.stdout
    corrected = synthesize_module(*args, corrected_content)
    checked = check_module(filename, corrected)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize(
    ("names", "branch"),
    [
        (("dict",), None),
        (("str",), None),
        (("type",), None),
        (("str", "_private"), None),
        (("__class__", "__slots__", "_abc_impl"), None),
        (("dict", "str", "type", "_private"), "dict"),
        (("str", "_private"), "str"),
        (("type",), "type"),
    ],
)
def test_legal_builtin_names_compile_type_check_and_execute(tmp_path, names, branch):
    fields = {name: INTEGER for name in names}
    values = {name: 7 for name in names}
    constructor = f"Branch.{branch}" if branch is not None else "Output"
    content = f"return {constructor}(" + ", ".join(f"{name}=input.{name}" for name in names) + ")"
    payload = request(content, fields, fields, {branch: "kept"} if branch else {}, values)
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    generated = synthesize_module(*args, content)
    checked = check_module(tmp_path / "legal_names.py", generated)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert asyncio.run(invoke(payload)) == {"branch": branch, "output": values}


@pytest.mark.parametrize("nested", [False, True])
def test_generated_input_rejects_undeclared_attributes_statically(tmp_path, nested):
    fields = {"n": INTEGER}
    if nested:
        inputs = {"user": {"type": "object", "properties": fields, "required": ["n"]}}
        prefix, values = "input.user", {"user": {"n": 7}}
    else:
        inputs, prefix, values = fields, "input", {"n": 7}
    content = f"return Output(value={prefix}.n)"
    payload = request(content, inputs, {"value": INTEGER}, values=values)
    args = [payload[key] for key in ("input_schema", "output_schema", "branches_contract")]
    filename = tmp_path / "input_fields.py"
    valid = synthesize_module(*args, content)
    checked = check_module(filename, valid)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert asyncio.run(invoke(payload))["output"] == {"value": 7}
    payload["run_spec"]["content"] = f"\n    return Output(value={prefix}.typo)\n"
    invalid = synthesize_module(*args, payload["run_spec"]["content"])
    checked = check_module(filename, invalid)
    assert checked.returncode != 0
    assert f":{invalid.line_offset + 1}:" in checked.stdout
    assert "has no attribute `typo`" in checked.stdout
    assert "[missing-attribute]" in checked.stdout
    assert checked.stdout.count("ERROR ") == 1, checked.stdout
    with pytest.raises(exceptions.ApplicationError) as failure:
        asyncio.run(invoke(payload))
    assert failure.value.type == "execution"
    assert "run.content line 1: AttributeError" in str(failure.value)


@pytest.mark.parametrize(
    "separator", ["\u2028", "\u2029", "\x85", "\v", "\f", "\x1c", "\x1d", "\x1e"]
)
@pytest.mark.parametrize("quote", ['"', '"""'])
async def test_source_preserves_nonphysical_line_separators_inside_strings(separator, quote):
    text = f"a{separator}b"
    payload = request(f"\n    return Output(text={quote}{text}{quote})\n", outputs={"text": S})
    generated = synthesize_module(
        payload["input_schema"],
        payload["output_schema"],
        payload["branches_contract"],
        payload["run_spec"]["content"],
    )
    assert generated.source.split("\n")[generated.line_offset] == (
        f"    return Output(text={quote}{text}{quote})"
    )
    compile(generated.source, "unicode_source.py", "exec")
    assert (await invoke(payload))["output"] == {"text": text}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_physical_newlines_preserve_relative_error_lines(newline):
    content = newline + f'    text = "a\u2028b"{newline}    raise ValueError(text)' + newline
    with pytest.raises(exceptions.ApplicationError) as failure:
        await invoke(request(content))
    assert failure.value.type == "execution"
    assert "run.content line 2: ValueError" in str(failure.value)
    assert "a\u2028b" in str(failure.value)


async def test_attempts_use_fresh_namespaces_even_when_source_is_identical():
    payload = request(
        "global counter\ncounter = globals().get('counter', 0) + 1\n"
        "import asyncio\nawait asyncio.sleep(0)\nreturn Output(value=counter)",
        outputs={"value": INTEGER},
    )
    results = await asyncio.gather(invoke(payload), invoke(payload))
    assert all(result["output"] == {"value": 1} for result in results)


@pytest.mark.parametrize(
    ("path", "block", "values", "expected"),
    [
        (
            "guide/01-foundations/01-basics.md",
            5,
            {"order_payload": '{"order_id":"A1","subtotal":20}', "tax_rate": Decimal("0.08")},
            {"total_amount": Decimal("21.6"), "receipt_text": "Order A1: Total $21.60"},
        ),
        (
            "cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md",
            1,
            {
                "batch": {
                    "feedbacks": [
                        {"user_id": "a", "content": "outage"},
                        {"user_id": "b", "content": "spam"},
                        {"user_id": "c", "content": "hello"},
                    ]
                }
            },
            {
                "urgent_feedbacks": [{"user_id": "a", "content": "outage"}],
                "analyzed_items": [
                    {"index": 0, "user_id": "a", "sentiment": "negative"},
                    {"index": 2, "user_id": "c", "sentiment": "neutral"},
                ],
                "urgent_tickets": ["INC-0:a"],
                "all_feature_tags": ["reliability", "incident", "general"],
                "executive_summary": "Analyzed=2, Urgent=1, Tags=3",
            },
        ),
    ],
)
async def test_documented_workflows_execute_with_default_builtin(
    execution, path, block, values, expected
):
    text = await asyncio.to_thread((MANUAL / path).read_text)
    source = text.split("```yaml\n")[block].split("```")[0]
    bundle_json = compile_bundle(
        "main", {"main": {"packages": {"main": {"format": "yaml", "text": source}}}}
    )
    async with execution() as run:
        handle = await run.server.client.start_workflow(
            AxcutorInterpreterWorkflow.run,
            {"bundle_json": bundle_json, "input_json": dumps(values)},
            id=str(uuid4()),
            task_queue=run.queue,
        )
        run.handles.append(handle)
        assert (await run.finish(handle))["output"] == expected


def test_keyword_list_matches_public_compiler_restrictions():
    import keyword

    for name in keyword.kwlist:
        source = {
            "input": {name: INTEGER},
            "output": {},
            "run": {"executor": "builtin", "content": "pass"},
        }
        compiled = loads(
            axcutor_core.compile_workflow(
                dumps(
                    {
                        "entry": "main",
                        "mounts": {
                            "main": {
                                "packages": {"main": {"format": "json", "text": dumps(source)}}
                            }
                        },
                    }
                )
            )
        )
        assert compiled["bundle"] is None
        assert any(d["code"] == "schema.identifier" for d in compiled["diagnostics"])
