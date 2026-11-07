//! Python owns only strings; all language behavior lives in axcutor-core.
use axcutor_compiler_core as compiler;
use pyo3::{exceptions::PyValueError, prelude::*};

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
    m.add_function(wrap_pyfunction!(compile_workflow, m)?)?;
    m.add_function(wrap_pyfunction!(get_available_vars_at_node, m)?)?;
    m.add_function(wrap_pyfunction!(derive_node_ports, m)?)?;
    Ok(())
}
