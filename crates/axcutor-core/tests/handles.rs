//! Opaque resource identity is nominal, unforgeable by literals, and host-private.
mod support;
use axcutor_core::{
    CompileOutput, CompileRequest, SourceDocument, ast::*, compile_workflow, kernel::*,
};
use serde_json::{Value, json};
use support::*;

fn handle() -> Value {
    json!({"type":"handle", "kind":"infra.postgres:Tx"})
}

fn compile(v: Value) -> CompileOutput {
    compile_packages("main", [("main".into(), source(v))])
}

fn compile_packages(
    entry: &str,
    packages: impl IntoIterator<Item = (String, SourceDocument)>,
) -> CompileOutput {
    let mut mounts = support::mounts(packages);
    mounts.entry("infra".into()).or_default().resources = [
        (
            "infra.postgres".into(),
            source(json!({"resources":{"Tx":null,"Other":{}}})),
        ),
        (
            "infra._private".into(),
            source(json!({"resources":{"Tx":null}})),
        ),
    ]
    .into_iter()
    .collect();
    compile_workflow(CompileRequest {
        entry: entry.into(),
        mounts,
    })
}

#[test]
fn handles_are_closed_nominal_and_nullable_without_structural_projection() {
    let t = TypeSpec::parse(&handle()).unwrap();
    assert_eq!(t.base(), "handle");
    assert!(t.contains_handle());
    assert!(t.equivalent(&TypeSpec::handle("infra.postgres:Tx").unwrap()));
    assert!(t.is_subtype_of(&t.make_nullable()));
    assert!(!t.make_nullable().is_subtype_of(&t));
    assert!(!t.is_subtype_of(&TypeSpec::handle("infra.postgres:Other").unwrap()));
    for kind in [
        "",
        "infra",
        "infra:Tx:Other",
        "_infra:Tx",
        "infra:_Tx",
        "infra:class",
        "infra/pg:Tx",
        "infra.json:Tx",
        "infra.__ax_resources__:Tx",
        "infra.__axcutor__:Tx",
    ] {
        assert!(TypeSpec::handle(kind).is_err(), "{kind}");
    }
    assert!(TypeSpec::handle("infra._internal:start").is_ok());
    for field in [
        "enum",
        "items",
        "properties",
        "additionalProperties",
        "required",
    ] {
        let mut invalid = handle();
        invalid[field] = json!([]);
        assert!(TypeSpec::parse(&invalid).is_err());
    }
    assert!(TypeSpec::parse(&json!({"type":"handle"})).is_err());
    assert_eq!(t.to_value(), handle());
}

#[test]
fn runtime_envelopes_are_validated_but_literals_cannot_create_authority() {
    let t = TypeSpec::parse(&handle()).unwrap();
    let envelope = json!({"$handle":"res_123", "kind":"infra.postgres:Tx"});
    assert!(t.accepts_runtime(&envelope));
    assert!(!t.accepts_literal(&envelope));
    assert!(t.make_nullable().accepts_literal(&Value::Null));
    for invalid in [
        json!({"$handle":"", "kind":"infra.postgres:Tx"}),
        json!({"$handle":"res_123", "kind":"infra.postgres:Other"}),
        json!({"rid":"res_123", "kind":"infra.postgres:Tx"}),
        json!({"$handle":"res_123", "kind":"infra.postgres:Tx", "locator":{}}),
    ] {
        assert!(!t.accepts_runtime(&invalid));
    }
    let array = TypeSpec::array(t.clone());
    let map = TypeSpec::map(t);
    assert!(array.accepts_literal(&json!([])));
    assert!(map.accepts_literal(&json!({})));
    assert!(array.accepts_runtime(&json!([envelope.clone()])));
    assert!(!array.accepts_literal(&json!([envelope.clone()])));
    assert!(!map.accepts_literal(&json!({"entry":envelope})));
    let business = TypeSpec::map(TypeSpec::scalar(Scalar::String));
    assert!(business.accepts_literal(&json!({"$handle":"customer-value"})));
}

#[test]
fn definitions_can_have_handles_but_host_entry_and_signal_outputs_cannot() {
    let out = compile(
        json!({"input":{"tx":handle()},"output":{"tx":handle()},"run":{"executor":"builtin","content":"return Output(tx=input.tx)"}}),
    );
    ok(&out);
    assert!(validate_root_interface_json(&json!({"tx":handle()}).to_string(), "{}", "{}").is_err());
    assert!(
        validate_root_interface_json(
            "{}",
            &json!({"tx":{"type":"array", "items":handle()}}).to_string(),
            "{}"
        )
        .is_err()
    );
    assert!(validate_root_interface_json("{}", "{}", "{}").is_ok());
    error(
        &compile(json!({"input":{},"output":{"tx":handle()},"run":{"executor":"signal"}})),
        "type.signal_handle",
    );
    error(
        &compile(graph(
            json!({"wait":{"output":{"tx":handle()},"run":{"executor":"signal"}}}),
            json!(["start -> wait", "wait -> end"]),
        )),
        "type.signal_handle",
    );
    let schema = json!({"tx":handle()}).to_string();
    let response = json!({"output":{"tx":{"$handle":"res_1","kind":"infra.postgres:Tx"}}});
    assert!(validate_run_contract_json(&response.to_string(), &schema, "{}").is_ok());
    assert!(validate_input_json(&response["output"].to_string(), &schema).is_ok());
}

#[test]
fn private_resource_modules_use_the_declaring_workflows_visibility() {
    let private = json!({"type":"array", "items":{"type":"handle", "kind":"infra._private:Tx"}});
    error(
        &compile(
            json!({"input":{"xs":private.clone()},"output":{},"run":{"executor":"builtin","content":"pass"}}),
        ),
        "visibility.private_resource",
    );
    let doc =
        json!({"input":{"xs":private}, "output":{},"run":{"executor":"builtin","content":"pass"}});
    ok(&compile_packages(
        "infra.flow",
        [("infra.flow".into(), source(doc))],
    ));
}

#[test]
fn handles_copy_and_compare_by_existing_value_semantics_but_not_by_order() {
    let mut doc = graph(
        json!({"cmp":{"use":"builtin:compare","with":{"input":{"op":{"value":"eq"},"left":"a","right":"b"},"output":{"result":"same"}}}}),
        json!(["start -> cmp", "cmp -> end"]),
    );
    doc["input"] = json!({"a":handle(), "b":handle()});
    doc["output"] = json!({"same":{"type":"boolean"}});
    ok(&compile(doc.clone()));
    doc["input"]["b"]["kind"] = json!("infra.postgres:Other");
    error(&compile(doc.clone()), "builtin.type");
    doc["input"]["b"] = handle();
    doc["graph"]["nodes"]["cmp"]["with"]["input"]["op"] = json!({"value":"lt"});
    error(&compile(doc), "builtin.type");
    assert!(literal_eq(
        &json!({"$handle":"res_1","kind":"infra.postgres:Tx"}),
        &json!({"kind":"infra.postgres:Tx","$handle":"res_1"})
    ));
}
