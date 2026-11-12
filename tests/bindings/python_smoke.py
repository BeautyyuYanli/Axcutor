"""Run against a maturin-built, installed native Python extension."""

import json
import sys
from pathlib import Path

import axcutor_core

oracle = json.loads(Path(sys.argv[1]).read_text())


def expect_host_error(callback):
    try:
        callback()
    except ValueError:
        pass
    else:
        raise AssertionError("malformed host argument did not raise ValueError")


queries = 0
invalid_queries = 0
for case in oracle:
    assert axcutor_core.compile_workflow(case["request_json"]) == case["result_json"]
    for query in case["queries"]:
        assert (
            axcutor_core.get_available_vars_at_node(
                query["analysis_json"], query["node_id"], query["expected_json"]
            )
            == query["vars_json"]
        )
        assert (
            axcutor_core.derive_node_ports(query["analysis_json"], query["node_id"])
            == query["ports_json"]
        )
        queries += 1
    for query in case["invalid_queries"]:
        expect_host_error(
            lambda query=query: axcutor_core.get_available_vars_at_node(
                query["analysis_json"], query["node_id"], query["expected_json"]
            )
        )
        if query["check_ports"]:
            expect_host_error(
                lambda query=query: axcutor_core.derive_node_ports(
                    query["analysis_json"], query["node_id"]
                )
            )
        invalid_queries += 1

shallow = next(case["queries"][0]["analysis_json"] for case in oracle if case["queries"])
deep_type = '{"type":"array","items":' * 2048 + '{"type":"integer"}' + "}" * 2048
deep_analysis = shallow[:-1] + ',"extra":' + "[" * 2048 + "0" + "]" * 2048 + "}"
for callback in [
    lambda: axcutor_core.compile_workflow("{}"),
    lambda: axcutor_core.compile_workflow("not JSON"),
    lambda: axcutor_core.get_available_vars_at_node("{}", "n"),
    lambda: axcutor_core.derive_node_ports("not JSON", "n"),
    lambda: axcutor_core.get_available_vars_at_node(shallow, "n", deep_type),
    lambda: axcutor_core.get_available_vars_at_node(deep_analysis, "n"),
    lambda: axcutor_core.derive_node_ports(deep_analysis, "n"),
]:
    expect_host_error(callback)

print(f"Rejected query groups: {invalid_queries}")
handle_schema = '{"tx":{"type":"handle","kind":"infra.postgres:Tx"}}'
handle_value = '{"tx":{"$handle":"res_1","kind":"infra.postgres:Tx"}}'
assert json.loads(axcutor_core.validate_input(handle_value, handle_schema)) == json.loads(
    handle_value
)
assert json.loads(
    axcutor_core.validate_run_contract('{"output":' + handle_value + "}", handle_schema, "{}")
)["output"] == json.loads(handle_value)
expect_host_error(lambda: axcutor_core.validate_root_interface(handle_schema, "{}", "{}"))
resources = '{"infra.postgres:Tx":{"locator":{"type":"integer"}}}'
external = '{"tx":{"$handle":"infra.postgres:Tx","locator":123456789012345678901234567890}}'
assert axcutor_core.validate_root_interface(handle_schema, "{}", resources) == "null"
assert axcutor_core.validate_external_input(external, handle_schema, resources) == external
assert (
    axcutor_core.validate_external_output(
        '{"output":' + external + "}", handle_schema, "{}", resources
    )
    == '{"branch":null,"output":' + external + "}"
)
assert (
    axcutor_core.validate_literal("123456789012345678901234567890", '{"type":"integer"}')
    == "123456789012345678901234567890"
)
expect_host_error(
    lambda: axcutor_core.validate_external_input(handle_value, handle_schema, resources)
)
for locator in ['{"type":["integer","null"]}', '{"type":"handle","kind":"infra.postgres:Tx"}']:
    invalid_resources = '{"infra.postgres:Tx":{"locator":' + locator + "}}"
    expect_host_error(
        lambda invalid_resources=invalid_resources: axcutor_core.validate_root_interface(
            handle_schema, "{}", invalid_resources
        )
    )
    expect_host_error(
        lambda invalid_resources=invalid_resources: axcutor_core.validate_external_input(
            external, handle_schema, invalid_resources
        )
    )
    expect_host_error(
        lambda invalid_resources=invalid_resources: axcutor_core.validate_external_output(
            '{"output":' + external + "}", handle_schema, "{}", invalid_resources
        )
    )
expect_host_error(
    lambda: axcutor_core.validate_input(
        '{"tx":{"$handle":"res_1","kind":"infra.postgres:Other"}}', handle_schema
    )
)
print(f"Python native extension: {len(oracle)} compile cases, {queries} canvas queries passed")
