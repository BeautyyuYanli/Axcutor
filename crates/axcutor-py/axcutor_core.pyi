"""Deterministic compiler and validation API; structured values use JSON text.

Internal handles use {"$handle": rid, "kind": kind}; external handles use
{"$handle": kind, "locator": value} checked against resources[kind].locator.
These functions validate structure without granting resource access authority.

ValueError reports malformed arguments/schemas and invalid root interfaces or
ingress values. ContractError (a ValueError subclass) reports invalid run or
external output responses and incompatible literal values. RuntimeError reports
engine invariant failures, including invalid checked builtin operations. Static
compilation diagnostics are returned in the compiler's JSON result.
"""

class ContractError(ValueError): ...

def compile_workflow(request_json: str) -> str: ...
def get_available_vars_at_node(
    analysis_json: str, node_id: str, expected_json: str | None = None
) -> str: ...
def derive_node_ports(analysis_json: str, node_id: str) -> str: ...
def eval_arg(arg_json: str, env_json: str) -> str: ...
def eval_builtin(op: str, bindings_json: str, env_json: str, symbols_json: str) -> str: ...
def validate_run_contract(response_json: str, output_schema_json: str, branches_json: str) -> str:
    """Filter active branch outputs and check internal capability envelopes."""

def validate_input(input_json: str, schema_json: str) -> str:
    """Check exact internal call/Child parameters; return their JSON text."""

def validate_root_interface(
    input_schema_json: str, output_schema_json: str, resources_json: str
) -> str:
    """Require public locators for all root handles; return the JSON text null."""

def validate_external_input(input_json: str, schema_json: str, resources_json: str) -> str:
    """Check exact host ingress parameters with closed external envelopes."""

def validate_external_output(
    response_json: str, output_schema_json: str, branches_json: str, resources_json: str
) -> str:
    """Filter root/Signal branch outputs and check closed external envelopes."""

def validate_literal(value_json: str, schema_json: str) -> str:
    """Check literal compatibility (no non-null handles); return JSON text."""

def literal_equal(left_json: str, right_json: str) -> bool: ...
