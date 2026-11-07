//! Exercise every builtin through compilation, with valid and invalid contracts.
mod support;
use axcutor_core::*;
use serde_json::{Value, json};
use support::*;

fn fixture(op: &str) -> Value {
    let input = json!({
        "optional":{"type":["integer","null"]},"array":{"type":"array","items":{"type":"integer"}},
        "nested":{"type":"array","items":{"type":"array","items":{"type":"integer"}}},
        "map":{"type":"object","additionalProperties":{"type":"integer"}},
        "record":{"type":"object","properties":{"id":{"type":"integer"},"text":{"type":"string"}}},
        "flag":{"type":"boolean"},"number":{"type":"integer"},"text":{"type":"string"}
    });
    let vars = json!({"value":{"type":"integer"},"rest":{"type":"array","items":{"type":"number"}},"list":{"type":"array","items":{"type":"number"}},"mapping":{"type":"object","additionalProperties":{"type":"number"}},"record_out":{"type":"object","properties":{"id":{"type":"number"},"text":{"type":"string"}}},"boolean":{"type":"boolean"}});
    let (args, out, ports) = match op {
        "assign" => (
            json!({"source":"number"}),
            Some(json!({"value":"value"})),
            vec![],
        ),
        "unwrap" => (
            json!({"source":"optional"}),
            Some(json!({"value":"value"})),
            vec!["some", "none"],
        ),
        "switch" => (json!({"source":"flag"}), None, vec!["true", "false"]),
        "compare" => (
            json!({"op":{"value":"eq"},"left":"number","right":1.0}),
            None,
            vec!["true", "false"],
        ),
        "logic" => (
            json!({"op":{"value":"not"},"source":"flag"}),
            None,
            vec!["true", "false"],
        ),
        "math" => (
            json!({"op":{"value":"add"},"left":"number","right":1}),
            Some(json!({"result":"value"})),
            vec![],
        ),
        "length" => (
            json!({"source":"array"}),
            Some(json!({"result":"value"})),
            vec![],
        ),
        "array_get" => (
            json!({"source":"array","index":-1}),
            Some(json!({"value":"value"})),
            vec!["some", "none"],
        ),
        "array_shift" => (
            json!({"source":"array"}),
            Some(json!({"item":"value","rest":"rest"})),
            vec!["some", "empty"],
        ),
        "array_append" => (
            json!({"source":"array","item":2.0}),
            Some(json!({"result":"list"})),
            vec![],
        ),
        "array_concat" => (
            json!({"left":"array","right":{"value":[]}}),
            Some(json!({"result":"list"})),
            vec![],
        ),
        "array_flatten" => (
            json!({"source":"nested"}),
            Some(json!({"result":"list"})),
            vec![],
        ),
        "map_get" => (
            json!({"source":"map","key":{"value":"id"}}),
            Some(json!({"value":"value"})),
            vec!["some", "none"],
        ),
        "map_set" => (
            json!({"source":"map","key":{"value":"id"},"value":1.5}),
            Some(json!({"result":"mapping"})),
            vec![],
        ),
        "map_delete" => (
            json!({"source":"map","key":{"value":"id"}}),
            Some(json!({"result":"mapping"})),
            vec![],
        ),
        "object_merge" => (
            json!({"base":"record","text":{"value":"new"}}),
            Some(json!({"result":"record_out"})),
            vec![],
        ),
        _ => unreachable!(),
    };
    builtin(op, args, out, input, vars, &ports)
}

const OPS: [&str; 16] = [
    "assign",
    "unwrap",
    "switch",
    "compare",
    "logic",
    "math",
    "length",
    "array_get",
    "array_shift",
    "array_append",
    "array_concat",
    "array_flatten",
    "map_get",
    "map_set",
    "map_delete",
    "object_merge",
];

#[test]
fn all_sixteen_operators_check_valid_and_invalid_inputs_and_outputs() {
    for op in OPS {
        let doc = fixture(op);
        let out = compile(doc.clone());
        ok(&out);
        let mut invalid = doc.clone();
        invalid["graph"]["nodes"]["step"]["with"]["input"]["unexpected"] = json!(1);
        error(
            &compile(invalid),
            if op == "object_merge" {
                "builtin.type"
            } else {
                "builtin.signature"
            },
        );
        let mut invalid = doc.clone();
        invalid["graph"]["nodes"]["step"]["with"]["output"] = json!({});
        error(&compile(invalid), "builtin.signature");
        let mut invalid = doc;
        let args = invalid["graph"]["nodes"]["step"]["with"]["input"]
            .as_object_mut()
            .unwrap();
        let p = match op {
            "array_concat" => "left",
            "logic" => "source",
            "compare" | "math" => "left",
            "object_merge" => "text",
            _ => "source",
        };
        args.insert(p.into(), Value::Null);
        let out = compile(invalid);
        if op == "unwrap" {
            ok(&out);
        } else {
            assert!(out.bundle.is_none(), "null accepted by {op}");
            assert!(
                out.diagnostics
                    .iter()
                    .any(|d| d.code.starts_with("type.") || d.code.starts_with("builtin.")),
                "{op}: {:?}",
                out.diagnostics
            );
        }
    }
}

#[test]
fn not_source_and_shift_some_empty_follow_detailed_contract() {
    let out = compile(fixture("logic"));
    ok(&out);
    assert_eq!(
        derive_node_ports(&out.analysis[0], "step")
            .iter()
            .map(|p| p.branch_id.as_deref())
            .collect::<Vec<_>>(),
        vec![Some("true"), Some("false")]
    );
    let mut wrong = fixture("logic");
    wrong["graph"]["nodes"]["step"]["with"]["input"] = json!({"op":{"value":"not"},"left":"flag"});
    error(&compile(wrong), "builtin.signature");
    let out = compile(fixture("array_shift"));
    ok(&out);
    let ports = derive_node_ports(&out.analysis[0], "step");
    assert_eq!(ports[0].branch_id.as_deref(), Some("some"));
    assert_eq!(ports[0].output, vec!["value", "rest"]);
    assert_eq!(ports[1].branch_id.as_deref(), Some("empty"));
    assert!(ports[1].output.is_empty());
    assert_eq!(ports[1].kill_vars, vec!["value"]);
    let bundle = out.bundle.unwrap();
    let axcutor_core::ast::RootBody::Graph { graph } = &bundle.workflows["main"].body else {
        panic!()
    };
    assert_eq!(graph.routes["step.empty"].kill_vars, vec!["value"]);
    let mut wrong = fixture("array_shift");
    wrong["graph"]["edges"] = json!(["start -> step", "step.item -> end", "step.empty -> end"]);
    error(&compile(wrong), "topology.unknown_port");
}

#[test]
fn optional_defaults_presence_controls_ports_and_null_is_not_omission() {
    for op in ["unwrap", "array_get", "map_get"] {
        let mut doc = fixture(op);
        doc["graph"]["nodes"]["step"]["with"]["input"]["default"] = json!(0);
        doc["graph"]["edges"] = json!(["start -> step", "step -> end"]);
        let out = compile(doc.clone());
        ok(&out);
        assert_eq!(
            derive_node_ports(&out.analysis[0], "step")[0].branch_id,
            None
        );
        doc["graph"]["nodes"]["step"]["with"]["input"]["default"] = Value::Null;
        error(&compile(doc.clone()), "type.compatibility");
        doc["graph"]["variables"]["value"] = json!({"type":["integer","null"]});
        if op == "unwrap" {
            error(&compile(doc), "builtin.type");
        } else {
            ok(&compile(doc));
        }
    }
    for op in ["compare", "logic"] {
        let mut doc = fixture(op);
        doc["graph"]["nodes"]["step"]["with"]["output"] = json!({"result":"boolean"});
        doc["graph"]["edges"] = json!(["start -> step", "step -> end"]);
        ok(&compile(doc));
    }
}

#[test]
fn numerical_result_types_and_restricted_ops_are_static() {
    for op in ["add", "sub", "mul", "div", "mod"] {
        let mut doc = fixture("math");
        doc["graph"]["nodes"]["step"]["with"]["input"]["op"] = json!({"value":op});
        if op == "div" || op == "mod" {
            doc["graph"]["edges"] =
                json!(["start -> step", "step.some -> end", "step.none -> end"]);
        }
        if op == "div" {
            error(&compile(doc.clone()), "type.output");
            doc["graph"]["variables"]["value"] = json!({"type":"number"});
        }
        ok(&compile(doc.clone()));
        doc["graph"]["nodes"]["step"]["with"]["input"]["right"] = json!(1.0);
        if op == "mod" {
            error(&compile(doc), "builtin.type");
        } else if op != "div" {
            error(&compile(doc), "type.output");
        }
    }
    let mut doc = fixture("math");
    doc["graph"]["nodes"]["step"]["with"]["input"]["default"] = json!(0);
    error(&compile(doc), "builtin.signature");
    for bad in [json!({"value":"pow"}), json!("text"), json!(2)] {
        let mut doc = fixture("math");
        doc["graph"]["nodes"]["step"]["with"]["input"]["op"] = bad;
        error(&compile(doc), "builtin.type");
    }
}

#[test]
fn enum_routing_comparison_and_literal_contexts() {
    let mut doc = fixture("switch");
    doc["input"]["flag"] = json!({"type":"string","enum":["red","blue"]});
    doc["graph"]["edges"] = json!(["start -> step", "step.red -> end", "step.blue -> end"]);
    ok(&compile(doc.clone()));
    for invalid in ["end", "has-hyphen"] {
        doc["input"]["flag"]["enum"] = json!([invalid]);
        error(&compile(doc.clone()), "builtin.type");
    }
    let mut doc = fixture("compare");
    doc["graph"]["nodes"]["step"]["with"]["input"] =
        json!({"op":{"value":"eq"},"left":"optional","right":null});
    ok(&compile(doc.clone()));
    doc["graph"]["nodes"]["step"]["with"]["input"]["left"] = json!("number");
    error(&compile(doc), "builtin.type");
    let mut doc = fixture("compare");
    doc["graph"]["nodes"]["step"]["with"]["input"] =
        json!({"op":{"value":"lt"},"left":"optional","right":1});
    error(&compile(doc), "builtin.type");
    let mut doc = fixture("compare");
    doc["graph"]["nodes"]["step"]["with"]["input"] =
        json!({"op":{"value":"eq"},"left":"array","right":{"value":[1,2]}});
    ok(&compile(doc));
    for (op, args, out, vars, ports) in [
        (
            "array_get",
            json!({"source":{"value":[]},"index":0}),
            json!({"value":"value"}),
            json!({"value":{"type":"integer"}}),
            vec!["some", "none"],
        ),
        (
            "array_concat",
            json!({"left":{"value":[]},"right":{"value":[1,1.0]}}),
            json!({"result":"value"}),
            json!({"value":{"type":"array","items":{"type":"number"}}}),
            vec![],
        ),
        (
            "map_set",
            json!({"source":{"value":{}},"key":{"value":"0"},"value":true}),
            json!({"result":"value"}),
            json!({"value":{"type":"object","additionalProperties":{"type":"boolean"}}}),
            vec![],
        ),
    ] {
        ok(&compile(builtin(
            op,
            args,
            Some(out),
            json!({}),
            vars,
            &ports,
        )));
    }
}

#[test]
fn alias_nullable_shape_and_output_declaration_constraints() {
    let mut doc = fixture("unwrap");
    doc["graph"]["nodes"]["step"]["with"]["output"] = json!({"value":"optional"});
    error(&compile(doc), "builtin.alias");
    let mut doc = fixture("array_get");
    doc["graph"]["nodes"]["step"]["with"]["input"]["index"] = json!("number");
    doc["graph"]["nodes"]["step"]["with"]["output"] = json!({"value":"number"});
    error(&compile(doc), "builtin.alias");
    let mut doc = fixture("math");
    doc["graph"]["nodes"]["step"]["with"] = json!({"input":{"op":{"value":"mod"},"left":"number","right":2},"output":{"result":"number"}});
    doc["graph"]["edges"] = json!(["start -> step", "step.some -> end", "step.none -> end"]);
    error(&compile(doc.clone()), "builtin.alias");
    doc["graph"]["nodes"]["step"]["with"]["input"]["default"] = json!(0);
    doc["graph"]["edges"] = json!(["start -> step", "step -> end"]);
    ok(&compile(doc));
    for op in ["array_append", "array_concat", "map_set"] {
        let mut doc = fixture(op);
        let var = if op == "map_set" { "mapping" } else { "list" };
        doc["graph"]["variables"][var]["type"] =
            json!([if op == "map_set" { "object" } else { "array" }, "null"]);
        error(&compile(doc), "builtin.type");
    }
    let mut doc = fixture("length");
    doc["graph"]["nodes"]["step"]["with"]["input"]["source"] = json!("record");
    error(&compile(doc), "builtin.type");
    let mut doc = fixture("array_flatten");
    doc["input"]["nested"]["items"]["type"] = json!(["array", "null"]);
    error(&compile(doc), "builtin.type");
    let mut doc = fixture("assign");
    doc["graph"]["nodes"]["step"]["with"]["output"]["value"] = json!("undeclared");
    error(&compile(doc), "symbol.undeclared_output");
    let mut doc = fixture("array_shift");
    doc["graph"]["nodes"]["step"]["with"]["output"]["item"] = json!("rest");
    error(&compile(doc), "schema.output_injective");
}

#[test]
fn object_merge_covers_required_fields_and_checks_only_retained_base_types() {
    let mut doc = fixture("object_merge");
    doc["input"]["record"]["properties"]["id"] = json!({"type":"string"});
    doc["graph"]["nodes"]["step"]["with"]["input"]["id"] = json!(10);
    ok(&compile(doc.clone()));
    doc["graph"]["nodes"]["step"]["with"]["input"] = json!({"id":10,"text":{"value":"ready"}});
    ok(&compile(doc.clone()));
    doc["graph"]["nodes"]["step"]["with"]["input"] = json!({"id":10});
    error(&compile(doc.clone()), "builtin.type");
    doc["graph"]["nodes"]["step"]["with"]["input"] =
        json!({"base":{"value":{"id":"old","text":"kept"}},"id":10});
    ok(&compile(doc));
}

#[test]
fn shift_literals_are_validated_against_both_output_consumers() {
    for literal in [json!([]), json!([1, 2])] {
        let mut doc = fixture("array_shift");
        doc["graph"]["nodes"]["step"]["with"]["input"]["source"] = json!({"value":literal});
        ok(&compile(doc));
    }
    let mut doc = fixture("array_shift");
    doc["graph"]["nodes"]["step"]["with"]["input"]["source"] = json!({"value":[1.0]});
    error(&compile(doc), "type.compatibility");
}
