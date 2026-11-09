//! JSON text is the boundary: JS numbers and object enumeration never touch packages.
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn validate_input(input_json: &str, schema_json: &str) -> Result<String, JsError> {
    axcutor_core::kernel::validate_input_json(input_json, schema_json)
        .map_err(|e| JsError::new(&e.to_string()))
}

#[wasm_bindgen]
pub fn validate_run_contract(
    response_json: &str,
    output_schema_json: &str,
    branches_json: &str,
) -> Result<String, JsError> {
    axcutor_core::kernel::validate_run_contract_json(
        response_json,
        output_schema_json,
        branches_json,
    )
    .map_err(|e| JsError::new(&e.to_string()))
}

#[wasm_bindgen]
pub fn validate_root_interface(
    input_schema_json: &str,
    output_schema_json: &str,
) -> Result<String, JsError> {
    axcutor_core::kernel::validate_root_interface_json(input_schema_json, output_schema_json)
        .map_err(|e| JsError::new(&e.to_string()))
}

#[wasm_bindgen]
pub fn compile_workflow(request_json: &str) -> Result<String, JsError> {
    axcutor_core::compile_workflow_json(request_json).map_err(|e| JsError::new(&e))
}

#[wasm_bindgen]
pub fn get_available_vars_at_node(
    analysis_json: &str,
    node_id: &str,
    expected_json: Option<String>,
) -> Result<String, JsError> {
    axcutor_core::available_vars_json(analysis_json, node_id, expected_json.as_deref())
        .map_err(|e| JsError::new(&e))
}

#[wasm_bindgen]
pub fn derive_node_ports(analysis_json: &str, node_id: &str) -> Result<String, JsError> {
    axcutor_core::node_ports_json(analysis_json, node_id).map_err(|e| JsError::new(&e))
}
