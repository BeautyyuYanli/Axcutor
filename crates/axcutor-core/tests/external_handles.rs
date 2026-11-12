//! Public locators are nominal, schema-directed values at every host boundary.
mod support;
use axcutor_core::{kernel::*, *};
use serde_json::{Value, json};
use support::{error, ok, source};

fn handle() -> Value {
    json!({"type":"handle", "kind":"infra:File"})
}
fn resources() -> Value {
    json!({"infra:File":{"locator":{"type":"object","properties":{"id":{"type":"number"}}}}})
}
fn compile(entry: &str, document: Value, declarations: Value) -> CompileOutput {
    let mut mounts = support::mounts([(entry.into(), source(document))]);
    mounts
        .entry("infra".into())
        .or_default()
        .resources
        .insert("infra".into(), source(declarations));
    compile_workflow(CompileRequest {
        entry: entry.into(),
        mounts,
    })
}

#[test]
fn public_interfaces_compile_and_bundle_trims_unused_resources() {
    let document = json!({"input":{"files":{"type":"array","items":handle()}},"output":{"file":handle()},"run":{"executor":"signal"}});
    let out = compile(
        "__main__",
        document,
        json!({"resources":{"File":{"description":"public", "locator":{"type":"string"}},"Unused":null}}),
    );
    ok(&out);
    let bundle = out.bundle.unwrap();
    assert_eq!(
        bundle
            .resources
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["infra:File"]
    );
    assert_eq!(
        bundle.resources["infra:File"].description.as_deref(),
        Some("public")
    );
    assert!(
        validate_root_interface_json(
            &json!({"files":{"type":"array","items":handle()}}).to_string(),
            "{}",
            &serde_json::to_string(&bundle.resources).unwrap()
        )
        .is_ok()
    );
}

#[test]
fn diagnostics_name_each_private_boundary_kind() {
    let out = compile(
        "__main__",
        json!({"input":{"file":handle()},"output":{},"run":{"executor":"builtin","content":"pass"}}),
        json!({"resources":{"File":null}}),
    );
    error(&out, "type.root_handle");
    assert_eq!(out.diagnostics[0].param_path, "/input/file/kind");
    assert_eq!(out.diagnostics[0].target_kind, "variable");
    let out = compile(
        "main",
        json!({"input":{},"output":{"files":{"type":"array","items":handle()}},"run":{"executor":"signal"}}),
        json!({"resources":{"File":null}}),
    );
    error(&out, "type.signal_handle");
    assert_eq!(out.diagnostics[0].param_path, "/output/files/items/kind");
}

#[test]
fn locator_schema_invariants_apply_to_compiler_and_all_ffi_inputs() {
    for locator in [
        json!({"type":["string","null"]}),
        handle(),
        json!({"type":"array","items":handle()}),
    ] {
        let out = compile(
            "main",
            json!({"input":{},"output":{},"run":{"executor":"signal"}}),
            json!({"resources":{"File":{"locator":locator}}}),
        );
        error(&out, "type.schema");
        assert_eq!(out.diagnostics[0].param_path, "/resources/File/locator");
        let resources = json!({"infra:File":{"locator":locator}}).to_string();
        assert!(validate_root_interface_json("{}", "{}", &resources).is_err());
        assert!(validate_external_input_json("{}", "{}", &resources).is_err());
        assert!(validate_external_output_json(r#"{"output":{}}"#, "{}", "{}", &resources).is_err());
    }
    let out = compile(
        "main",
        json!({"input":{},"output":{},"run":{"executor":"signal"}}),
        json!({"resources":{"File":{"persistent":true}}}),
    );
    error(&out, "schema.unknown_field");
}

#[test]
fn external_values_are_closed_nominal_nested_nullable_and_lossless() {
    let resources = resources().to_string();
    let schema = json!({"files":{"type":"array","items":{"type":["handle","null"],"kind":"infra:File"}},"data":{"type":"object","additionalProperties":{"type":"string"}}}).to_string();
    let input = r#"{"files":[{"$handle":"infra:File","locator":{"id":123456789012345678901234567890.000000001}},null],"data":{"$handle":"business"}}"#;
    assert_eq!(
        validate_external_input_json(input, &schema, &resources).unwrap(),
        input
    );
    assert!(validate_input_json(input, &schema).is_err());
    let response = format!(r#"{{"branch":"done","output":{input}}}"#);
    let branches = r#"{"done":{"description":"","output":["files"]}}"#;
    let result: Value = serde_json::from_str(
        &validate_external_output_json(&response, &schema, branches, &resources).unwrap(),
    )
    .unwrap();
    assert!(result["output"].get("data").is_none());
    for file in [
        json!({"$handle":"rid", "kind":"infra:File"}),
        json!({"$handle":"infra:Other", "locator":{"id":1}}),
        json!({"$handle":"infra:File", "locator":null}),
        json!({"$handle":"infra:File", "locator":{"id":true}}),
        json!({"$handle":"infra:File", "locator":{"id":1},"kind":"infra:File"}),
    ] {
        assert!(
            validate_external_input_json(
                &json!({"file":file}).to_string(),
                &json!({"file":handle()}).to_string(),
                &resources
            )
            .is_err()
        );
    }
}
