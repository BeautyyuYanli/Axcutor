//! JSON text is the boundary: JS numbers and object enumeration never touch packages.
use wasm_bindgen::prelude::*;

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
