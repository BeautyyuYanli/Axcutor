//! Python owns only strings; all language behavior lives in axcutor-core.
use axcutor_compiler_core as compiler;
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
};

pyo3::create_exception!(axcutor_core, ContractError, PyValueError);

fn kernel_error(error: compiler::kernel::KernelError) -> PyErr {
    use compiler::kernel::KernelError;
    match error {
        KernelError::Contract(s) => ContractError::new_err(s),
        KernelError::Engine(s) => PyRuntimeError::new_err(s),
        KernelError::Argument(s) | KernelError::Input(s) => PyValueError::new_err(s),
    }
}

#[pyfunction]
fn eval_arg(arg_json: &str, env_json: &str) -> PyResult<String> {
    compiler::kernel::eval_arg_json(arg_json, env_json).map_err(kernel_error)
}

#[pyfunction]
fn eval_builtin(
    op: &str,
    bindings_json: &str,
    env_json: &str,
    symbols_json: &str,
) -> PyResult<String> {
    compiler::kernel::eval_builtin_json(op, bindings_json, env_json, symbols_json)
        .map_err(kernel_error)
}

#[pyfunction]
fn validate_run_contract(
    response_json: &str,
    output_schema_json: &str,
    branches_json: &str,
) -> PyResult<String> {
    compiler::kernel::validate_run_contract_json(response_json, output_schema_json, branches_json)
        .map_err(kernel_error)
}

#[pyfunction]
fn validate_input(input_json: &str, schema_json: &str) -> PyResult<String> {
    compiler::kernel::validate_input_json(input_json, schema_json).map_err(kernel_error)
}

#[pyfunction]
fn literal_equal(left_json: &str, right_json: &str) -> PyResult<bool> {
    compiler::kernel::literal_equal_json(left_json, right_json).map_err(kernel_error)
}

#[pyfunction]
fn validate_root_interface(
    input_schema_json: &str,
    output_schema_json: &str,
    resources_json: &str,
) -> PyResult<String> {
    compiler::kernel::validate_root_interface_json(
        input_schema_json,
        output_schema_json,
        resources_json,
    )
    .map_err(kernel_error)
}

#[pyfunction]
fn validate_external_input(
    input_json: &str,
    schema_json: &str,
    resources_json: &str,
) -> PyResult<String> {
    compiler::kernel::validate_external_input_json(input_json, schema_json, resources_json)
        .map_err(kernel_error)
}

#[pyfunction]
fn validate_external_output(
    response_json: &str,
    output_schema_json: &str,
    branches_json: &str,
    resources_json: &str,
) -> PyResult<String> {
    compiler::kernel::validate_external_output_json(
        response_json,
        output_schema_json,
        branches_json,
        resources_json,
    )
    .map_err(kernel_error)
}

#[pyfunction]
fn validate_literal(value_json: &str, schema_json: &str) -> PyResult<String> {
    compiler::kernel::validate_literal_json(value_json, schema_json).map_err(kernel_error)
}

#[pyfunction]
fn compile_workflow(request_json: &str) -> PyResult<String> {
    compiler::compile_workflow_json(request_json).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (analysis_json, node_id, expected_json=None))]
fn get_available_vars_at_node(
    analysis_json: &str,
    node_id: &str,
    expected_json: Option<&str>,
) -> PyResult<String> {
    compiler::available_vars_json(analysis_json, node_id, expected_json)
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn derive_node_ports(analysis_json: &str, node_id: &str) -> PyResult<String> {
    compiler::node_ports_json(analysis_json, node_id).map_err(PyValueError::new_err)
}

#[pymodule]
fn axcutor_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ContractError", m.py().get_type::<ContractError>())?;
    m.add_function(wrap_pyfunction!(eval_arg, m)?)?;
    m.add_function(wrap_pyfunction!(eval_builtin, m)?)?;
    m.add_function(wrap_pyfunction!(validate_run_contract, m)?)?;
    m.add_function(wrap_pyfunction!(validate_input, m)?)?;
    m.add_function(wrap_pyfunction!(validate_root_interface, m)?)?;
    m.add_function(wrap_pyfunction!(validate_external_input, m)?)?;
    m.add_function(wrap_pyfunction!(validate_external_output, m)?)?;
    m.add_function(wrap_pyfunction!(validate_literal, m)?)?;
    m.add_function(wrap_pyfunction!(literal_equal, m)?)?;
    m.add_function(wrap_pyfunction!(compile_workflow, m)?)?;
    m.add_function(wrap_pyfunction!(get_available_vars_at_node, m)?)?;
    m.add_function(wrap_pyfunction!(derive_node_ports, m)?)?;
    Ok(())
}
