//! Builtin has a closed contract; extensions retain opaque JSON configuration.
mod support;
use axcutor_core::ast::*;
use serde_json::json;
use support::*;

#[test]
fn builtin_run_spec_is_closed_and_rejects_legacy_strings() {
    for (run, code, path) in [
        (json!("return Output()"), "schema.object", "/run"),
        (json!({}), "schema.string", "/run/executor"),
        (
            json!({"executor":"remote.invalid", "url":"http://localhost"}),
            "schema.executor",
            "/run/executor",
        ),
        (
            json!({"executor":"builtin"}),
            "schema.string",
            "/run/content",
        ),
        (
            json!({"executor":"builtin", "content":" \n\t"}),
            "schema.run",
            "/run/content",
        ),
        (
            json!({"executor":"builtin", "content":"pass", "extra":true}),
            "schema.unknown_field",
            "/run/extra",
        ),
    ] {
        let out = compile(json!({"input":{},"output":{},"run":run}));
        only_error(&out, code, path);
    }
    let out = compile(
        json!({"input":{},"output":{},"run":{"executor":"builtin","content":"# variables.missing.anything\nreturn Output()"}}),
    );
    ok(&out);
    let value = serde_json::to_value(out.bundle.unwrap()).unwrap();
    assert_eq!(
        value["workflows"]["main"]["body"]["input_schema"],
        json!({})
    );
    assert!(
        !value["workflows"]["main"]["body"]
            .as_object()
            .unwrap()
            .contains_key("references")
    );
}

#[test]
fn extension_run_spec_preserves_configuration_in_compiled_bundle_serialization() {
    let out = json_text(
        r#"{"input":{},"output":{},"run":{"executor":"class","url":"http://localhost","2":null,"0":[true,"value"],"settings":{"10":123456789012345678901234567890,"2":1.000000000000000000000000001,"0":1e10000,"$serde_json::private::Number":"ordinary"}}}"#,
    );
    ok(&out);
    let value = serde_json::to_value(out.bundle.unwrap()).unwrap();
    // Check the public bundle output, including exact numbers and mapping order.
    assert_eq!(
        value["workflows"]["main"]["body"]["run"].to_string(),
        r#"{"executor":"class","url":"http://localhost","2":null,"0":[true,"value"],"settings":{"10":123456789012345678901234567890,"2":1.000000000000000000000000001,"0":1e+10000,"$serde_json::private::Number":"ordinary"}}"#
    );
    for executor in ["remote", "start", "class", "_private"] {
        ok(&compile(graph(
            json!({"work":{"run":{"executor":executor,"payload":null}}}),
            json!(["start -> work", "work -> end"]),
        )));
    }
    for executor in ["", "remote-executor", "远程", "two words"] {
        only_error(
            &compile(json!({"input":{},"output":{},"run":{"executor":executor}})),
            "schema.executor",
            "/run/executor",
        );
    }
}

#[test]
fn input_schema_is_normalized_and_keeps_capture_order_and_iteration_types() {
    let out = compile(json!({
        "input":{"z":{"type":["null","number"]},"items":{"type":"array","items":{"type":"string"}}},
        "output":{"result":{"type":"array","items":{"type":"integer"}}},
        "graph":{"nodes":{"each":{
            "run":{"executor":"builtin","content":"return Output(result=input.idx)"},
            "input":["idx","z","item"], "output":{"result":{"type":"integer"}},
            "for_each":{"source":"items","item":"item","index":"idx"}
        }},"edges":["start -> each","each -> end"]}
    }));
    ok(&out);
    let bundle = out.bundle.unwrap();
    let RootBody::Graph { graph } = &bundle.workflows["main"].body else {
        panic!()
    };
    let NodeBody::Run { input_schema, .. } = &graph.nodes["each"].body else {
        panic!()
    };
    assert_eq!(
        input_schema.keys().map(String::as_str).collect::<Vec<_>>(),
        ["idx", "z", "item"]
    );
    assert_eq!(input_schema["idx"].to_value(), json!({"type":"integer"}));
    assert_eq!(
        input_schema["z"].to_value(),
        json!({"type":["number","null"]})
    );
    assert_eq!(input_schema["item"].to_value(), json!({"type":"string"}));

    let out = compile(
        json!({"input":{"z":{"type":"string"},"a":{"type":"integer"}},"output":{},"run":{"executor":"builtin","content":"pass"}}),
    );
    ok(&out);
    let root = &out.bundle.as_ref().unwrap().workflows["main"];
    let RootBody::Run { input_schema, .. } = &root.body else {
        panic!()
    };
    assert_eq!(
        input_schema.keys().collect::<Vec<_>>(),
        root.input.keys().collect::<Vec<_>>()
    );
}

#[test]
fn every_restricted_identifier_position_excludes_python_keywords_and_cfg_names() {
    let keywords = [
        "start", "end", "fail", "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "class", "continue", "def", "del", "elif", "else", "except", "finally", "for",
        "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass",
        "raise", "return", "try", "while", "with", "yield",
    ];
    for keyword in keywords {
        assert!(!control_id(keyword));
        assert!(FieldPath::parse(keyword).is_none());
        assert!(FieldPath::parse(&format!("record.{keyword}")).is_none());
        assert!(
            TypeSpec::parse(&json!({"type":"object","properties":{keyword:{"type":"integer"}}}))
                .is_err()
        );
        for field in ["input", "output"] {
            let mut document =
                json!({"input":{},"output":{},"run":{"executor":"builtin","content":"pass"}});
            document[field] = json!({keyword:{"type":"integer"}});
            error(&compile(document), "schema.identifier");
        }
        for field in ["input", "output", "branches", "catch", "for_each"] {
            let mut node = json!({"run":{"executor":"builtin","content":"pass"}});
            node[field] = match field {
                "input" => json!([keyword]),
                "output" => json!({keyword:{"type":"integer"}}),
                "branches" => json!({keyword:"description"}),
                "catch" => json!({keyword:["execution"]}),
                _ => json!({"source":"items","item":keyword}),
            };
            error(
                &compile(graph(
                    json!({"work":node}),
                    json!(["start -> work", "work -> end"]),
                )),
                "schema.identifier",
            );
        }
    }
    for allowed in ["match", "case", "type", "items", "_private", "TrueValue"] {
        assert!(control_id(allowed));
    }
}
