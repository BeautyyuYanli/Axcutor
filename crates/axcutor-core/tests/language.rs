//! Public-interface grammar, literal, type, symbol and module-boundary contracts.
mod support;
use axcutor_core::{ast::*, *};
use serde_json::{Value, json};
use support::*;

#[test]
fn yaml_json_and_normalization_preserve_owned_contracts() {
    let out = yaml(
        r#"
input:
  person:
    type: object
    properties:
      name: {type: string}
output: {}
graph:
  variables:
    absent: {type: [string, "null"]}
    present: {type: [string, "null"], default: null}
  nodes:
    work:
      input: [person]
      branches: {done: "description"}
      catch: {problem: "*"}
      retry: {max_attempts: 2}
      run: {executor: builtin, content: "Read variables.person.name."}
  edges: ["start->work", "work.done -> end", "work.problem->fail"]
"#,
    );
    ok(&out);
    let bundle = out.bundle.unwrap();
    let root = &bundle.workflows["main"];
    let TypeKind::TypedDict { required, .. } = &root.input["person"].kind else {
        panic!()
    };
    assert_eq!(required, &["name"]);
    let RootBody::Graph { graph } = &root.body else {
        panic!()
    };
    assert!(matches!(
        graph.variables["absent"].initial,
        InitialValue::Unbound
    ));
    assert!(matches!(
        graph.variables["present"].initial,
        InitialValue::Default(Value::Null)
    ));
    assert_eq!(graph.max_steps.as_str(), "1000");
    let node = &graph.nodes["work"];
    assert_eq!(
        node.modifiers.catch["problem"],
        vec!["execution", "timeout", "contract"]
    );
    assert_eq!(
        node.modifiers.retry.as_ref().unwrap().backoff_ms.as_str(),
        "0"
    );
    let NodeBody::Run {
        input_schema,
        branches,
        ..
    } = &node.body
    else {
        panic!()
    };
    assert_eq!(input_schema.keys().collect::<Vec<_>>(), vec!["person"]);
    assert!(branches["done"].output.is_empty());
    let text = serde_json::to_string(&bundle).unwrap();
    let decoded: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(serde_json::to_string(&decoded).unwrap(), text);
    assert_eq!(
        out.analysis[0].nodes["work"].definite_in.as_ref().unwrap(),
        &vec!["person".to_owned(), "present".to_owned()]
    );
}

#[test]
fn source_errors_reject_duplicates_extensions_and_non_json_values() {
    let cases = [
        "input: {}\ninput: {}\noutput: {}\nrun: work",
        "input: {}\noutput: {}\nrun: &a work",
        "input: {}\noutput: {}\nrun: *a",
        "input: {}\noutput: {}\nrun: !custom work",
        "input: {}\noutput: {}\nrun: {executor: builtin, content: work}\n---\ninput: {}",
        "input: {}\noutput: {}\nrun: {executor: builtin, content: work}\n1: invalid",
        "input: {}\noutput: {}\nrun: {executor: builtin, content: work}\nvalue: .inf",
        "input: {}\noutput: {}\nrun: {executor: builtin, content: work}\nvalue: .nan",
        "input: [",
    ];
    for text in cases {
        let out = yaml(text);
        error(&out, "parse.source");
        assert!(out.diagnostics[0].source_location.is_some());
    }
    for text in [
        r#"{"input":{},"input":{},"output":{},"run":{"executor":"builtin","content":"work"}}"#,
        r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work"},}"#,
    ] {
        let out = compile_sources(
            "main",
            [(
                "main".into(),
                SourceDocument {
                    format: SourceFormat::Json,
                    text: text.into(),
                },
            )],
        );
        error(&out, "parse.source");
    }
}

#[test]
fn json_decodes_unicode_with_exact_numbers_and_order() {
    let escaped = json_text(
        r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work \ud83d\ude00"}}"#,
    );
    let literal =
        json_text(r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work 😀"}}"#);
    ok(&escaped);
    ok(&literal);
    let RootBody::Run { run, .. } = &escaped.bundle.as_ref().unwrap().workflows["main"].body else {
        panic!()
    };
    assert_eq!(
        run,
        &RunSpec::Builtin {
            content: "work 😀".into()
        }
    );
    assert_eq!(
        serde_json::to_string(&escaped).unwrap(),
        serde_json::to_string(&literal).unwrap()
    );
    for text in [
        r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work \ud83d"}}"#,
        r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work \ude00"}}"#,
        r#"{"input":{},"output":{},"run":{"executor":"builtin","content":"work"},"\u0072un":"duplicate"}"#,
        r#"{"input":{},"output":{},"graph":{"variables":{"m":{"type":"object","additionalProperties":{"type":"integer"},"default":{"x":1,"\u0078":2}}},"nodes":{"n":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> n","n -> end"]}}"#,
    ] {
        only_error(&json_text(text), "parse.source", "");
    }
    let duplicate = json_text(
        "\n \n{\n  \"input\":{},\n  \"output\":{},\n  \"run\":{\"executor\":\"builtin\",\"content\":\"work\"},\n  \"extra\":{\n    \"x\":1,\n    \"\\u0078\":2\n  }\n}",
    );
    only_error(&duplicate, "parse.source", "");
    let mark = duplicate.diagnostics[0].source_location.as_ref().unwrap();
    assert_eq!((mark.line, mark.column), (9, 12));
    let out = json_text(
        r#"{"input":{},"output":{},"graph":{"variables":{"huge":{"type":"integer","default":1234567890123456789012345678901234567890},"tiny":{"type":"number","default":1.000000000000000000000000001},"exponent":{"type":"number","default":1e10000},"m":{"type":"object","additionalProperties":{"type":"string"},"default":{"10":"ten","2":"two","0":"zero","$serde_json::private::Number":"ordinary"}}},"nodes":{"n":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> n","n -> end"]}}"#,
    );
    ok(&out);
    let text = serde_json::to_string(&out.bundle).unwrap();
    assert!(text.contains("1234567890123456789012345678901234567890"));
    assert!(text.contains("1.000000000000000000000000001"));
    assert!(text.contains("1e+10000"));
    assert!(text.contains(
        r#"{"10":"ten","2":"two","0":"zero","$serde_json::private::Number":"ordinary"}"#
    ));
}

#[test]
fn standard_yaml_tags_normalize_only_matching_json_shapes() {
    ok(&yaml(
        "input: {}\noutput: {}\nrun: {executor: builtin, content: !!str work}\n",
    ));
    ok(&yaml(
        "input: {}\noutput: {}\nrun: {executor: builtin, content: !<tag:yaml.org,2002:str> work}\n",
    ));
    for (tagged, target, expected) in [
        ("!!str 01", "{type: string}", json!("01")),
        ("!!int '001'", "{type: integer}", json!(1)),
        ("!!int 0x10", "{type: integer}", json!(16)),
        ("!!float 1", "{type: number}", json!(1.0)),
        ("!!float '.5'", "{type: number}", json!(0.5)),
        ("!!bool 'TRUE'", "{type: boolean}", json!(true)),
        ("!!null null", "{type: [string, 'null']}", Value::Null),
        (
            "!!seq [a]",
            "{type: array, items: {type: string}}",
            json!(["a"]),
        ),
        (
            "!!map {x: 1}",
            "{type: object, additionalProperties: {type: integer}}",
            json!({"x":1}),
        ),
    ] {
        let declaration = target.strip_suffix('}').unwrap();
        let out = yaml(&format!(
            "input: !!map {{}}\noutput: {{}}\ngraph:\n  variables:\n    v: {declaration}, default: {tagged}}}\n  nodes: {{n: {{run: {{executor: builtin, content: work}}}}}}\n  edges: !!seq ['start -> n', 'n -> end']\n"
        ));
        ok(&out);
        let RootBody::Graph { graph } = &out.bundle.as_ref().unwrap().workflows["main"].body else {
            panic!()
        };
        let InitialValue::Default(value) = &graph.variables["v"].initial else {
            panic!()
        };
        assert_eq!(value, &expected, "{tagged}");
    }
    for tagged in [
        "!custom work",
        "!!timestamp 2026-11-07",
        "!!binary d29yaw==",
        "!!set {x: null}",
        "!!omap []",
        "!!pairs []",
        "!!int 1.0",
        "!!int 1e2",
        "!!float true",
        "!!float .inf",
        "!!bool yes",
        "!!null 0",
        "!!str []",
        "!!seq text",
        "!!map []",
        "!!int {}",
    ] {
        let out = yaml(&format!("input: {{}}\noutput: {{}}\nrun: {tagged}\n"));
        only_error(&out, "parse.source", "");
        assert!(out.diagnostics[0].source_location.is_some(), "{tagged}");
    }
    only_error(
        &yaml(
            "%TAG !! tag:custom.example,2026:\n---\ninput: {}\noutput: {}\nrun: {executor: builtin, content: !!str work}\n",
        ),
        "parse.source",
        "",
    );
}

#[test]
fn integer_bounds_use_exact_values_and_preserve_integer_zero() {
    for (literal, valid) in [
        ("-0", true),
        ("0", true),
        ("123456789012345678901234567890", true),
        ("-1", false),
        ("-123456789012345678901234567890", false),
        ("0.0", false),
        ("-0.0", false),
        ("0e0", false),
    ] {
        let out = json_text(&format!(
            r#"{{"input":{{}},"output":{{}},"run":{{"executor":"builtin","content":"work"}},"retry":{{"max_attempts":2,"backoff_ms":{literal}}}}}"#
        ));
        if valid {
            ok(&out);
            assert!(
                serde_json::to_string(&out.bundle)
                    .unwrap()
                    .contains(&format!("\"backoff_ms\":{literal}"))
            );
        } else {
            only_error(&out, "schema.integer", "/retry/backoff_ms");
        }
    }
    for literal in ["-0", "0", "-1", "0.0", "1.0"] {
        let out = json_text(&format!(
            r#"{{"input":{{}},"output":{{}},"run":{{"executor":"builtin","content":"work"}},"timeout_ms":{literal}}}"#
        ));
        only_error(&out, "schema.integer", "/timeout_ms");
    }
}

#[test]
fn source_depth_limit_is_shared_by_json_and_yaml() {
    for format in [SourceFormat::Json, SourceFormat::Yaml] {
        for arrays in [127, 128] {
            // The root is depth 0; the extra field's leaf is depth arrays + 1.
            let text = format!(
                r#"{{"input":{{}},"output":{{}},"run":{{"executor":"builtin","content":"work"}},"extra":{}0{}}}"#,
                "[".repeat(arrays),
                "]".repeat(arrays)
            );
            let out = compile_sources(
                "main",
                [(
                    "main".into(),
                    SourceDocument {
                        format: format.clone(),
                        text,
                    },
                )],
            );
            if arrays == 127 {
                only_error(&out, "schema.unknown_field", "/extra");
            } else {
                only_error(&out, "parse.source", "");
            }
        }
    }
}

#[test]
fn catch_branch_collisions_have_one_authoritative_diagnostic() {
    let target = source(
        json!({"input":{},"output":{},"branches":{"ok":"success"},"run":{"executor":"builtin","content":"work"}}),
    );
    for body in [
        json!({"run":{"executor":"builtin","content":"work"},"branches":{"ok":"success"}}),
        json!({"branches":{"ok":"success"},"graph":{"nodes":{"inner":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> inner","inner -> end.ok"]}}),
        json!({"use":"workflow:target"}),
    ] {
        for collision in [false, true] {
            let mut node = body.clone();
            node["catch"] = if collision {
                json!({"ok":["execution"]})
            } else {
                json!({"err":["execution"]})
            };
            let edges = if collision {
                json!(["start -> n", "n.ok -> end"])
            } else {
                json!(["start -> n", "n.ok -> end", "n.err -> fail"])
            };
            let out = compile_sources(
                "main",
                [
                    ("main".into(), source(graph(json!({"n":node}), edges))),
                    ("target".into(), target.clone()),
                ],
            );
            if collision {
                only_error(&out, "type.branch_collision", "/graph/nodes/n/catch/ok");
                no_facts(&out);
            } else {
                ok(&out);
            }
        }
    }
}

#[test]
fn strict_nodes_modifiers_identifiers_and_edges() {
    let valid = graph(
        json!({"one":{"run":{"executor":"builtin","content":"work"}},"two":{"run":{"executor":"builtin","content":"work"}}}),
        json!([" \tstart\t-> [ one ,\ttwo ] -> end\t"]),
    );
    ok(&compile(valid));
    for edge in [
        "start -> one -> end",
        "start -> [one] -> end",
        "start -> [one, one] -> end",
        "start -> [one,two] -> fail",
        "start -> one.x",
        "start .x -> one",
        "one. x -> end",
        "start -> end .x",
        "start\n->one",
        "start -> one\u{a0}",
        "fail -> one",
        "end -> one",
        "start -> start",
    ] {
        let out = compile(graph(
            json!({"one":{"run":{"executor":"builtin","content":"work"}},"two":{"run":{"executor":"builtin","content":"work"}}}),
            json!([edge]),
        ));
        error(&out, "schema.edge");
        no_facts(&out);
    }
    let cases = [
        (
            json!({"run":{"executor":"builtin","content":" "}}),
            json!({"run":{"executor":"builtin","content":"work"}}),
            json!(["one -> end"]),
            "schema.run",
            "/run/content",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"input":["a","a"]}),
            json!({"run":{"executor":"builtin","content":"work"},"input":["a"]}),
            json!(["one -> end"]),
            "schema.duplicate",
            "/input/1",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"graph":{"nodes":{"n":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> n","n -> end"]}}),
            json!({"graph":{"nodes":{"n":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> n","n -> end"]}}),
            json!(["one -> end"]),
            "schema.body",
            "",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"extra":true}),
            json!({"run":{"executor":"builtin","content":"work"}}),
            json!(["one -> end"]),
            "schema.unknown_field",
            "/extra",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"retry":{"max_attempts":1}}),
            json!({"run":{"executor":"builtin","content":"work"},"retry":{"max_attempts":2}}),
            json!(["one -> end"]),
            "schema.integer",
            "/retry/max_attempts",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"retry":{"max_attempts":2,"on":["max_steps"]}}),
            json!({"run":{"executor":"builtin","content":"work"},"retry":{"max_attempts":2,"on":["execution"]}}),
            json!(["one -> end"]),
            "schema.selectors",
            "/retry/on",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution"],"other":["execution"]}}),
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution"],"other":["timeout"]}}),
            json!(["one -> end", "one.err -> fail", "one.other -> fail"]),
            "schema.catch",
            "/catch/other",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["*"]}}),
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution"]}}),
            json!(["one -> end", "one.err -> fail"]),
            "schema.selectors",
            "/catch/err",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":"*","other":"*"}}),
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution"],"other":"*"}}),
            json!(["one -> end", "one.err -> fail", "one.other -> fail"]),
            "schema.catch",
            "/catch/other",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution","timeout","contract"],"other":"*"}}),
            json!({"run":{"executor":"builtin","content":"work"},"catch":{"err":["execution"],"other":"*"}}),
            json!(["one -> end", "one.err -> fail", "one.other -> fail"]),
            "schema.catch",
            "/catch/other",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"branches":{"end":"reserved"}}),
            json!({"run":{"executor":"builtin","content":"work"},"branches":{"ok":"success"}}),
            json!(["one.ok -> end"]),
            "schema.identifier",
            "/branches/end",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"output":{"x":{"type":"integer"}},"branches":{"a":{"description":"x","output":["unknown"]}}}),
            json!({"run":{"executor":"builtin","content":"work"},"output":{"x":{"type":"integer"}},"branches":{"a":{"description":"x","output":["x"]}}}),
            json!(["one.a -> end"]),
            "schema.branch_output",
            "/branches/a/output",
        ),
        (
            json!({"use":"builtin:assign","timeout_ms":1,"with":{"input":{"source":1},"output":{"value":"x"}}}),
            assign(json!(1), "x"),
            json!(["one -> end"]),
            "schema.unknown_field",
            "/timeout_ms",
        ),
        (
            json!({"run":{"executor":"builtin","content":"work"},"output":{"ys":{"type":"integer"}},"for_each":{"source":"items","item":"i","index":"i"}}),
            json!({"run":{"executor":"builtin","content":"work"},"output":{"ys":{"type":"integer"}},"for_each":{"source":"items","item":"i","index":"idx"}}),
            json!(["one -> end"]),
            "symbol.injected_collision",
            "/for_each/index",
        ),
    ];
    for (invalid, valid, routes, code, suffix) in cases {
        let mut edges = vec![json!("start -> one")];
        edges.extend(routes.as_array().unwrap().iter().cloned());
        let fixture = |node| json!({"input":{"a":{"type":"integer"},"items":{"type":"array","items":{"type":"integer"}}},"output":{},"graph":{"variables":{"x":{"type":"integer"},"ys":{"type":"array","items":{"type":"integer"}}},"nodes":{"one":node},"edges":edges}});
        ok(&compile(fixture(valid)));
        let out = compile(fixture(invalid));
        only_error(&out, code, &format!("/graph/nodes/one{suffix}"));
        no_facts(&out);
    }
    for key in ["start", "end", "fail", "has-hyphen", "has.dot", "é"] {
        let out = compile(graph(
            json!({key:{"run":{"executor":"builtin","content":"work"}}}),
            json!(["start -> end"]),
        ));
        error(&out, "schema.identifier");
    }
}

#[test]
fn constants_distinguish_integer_float_bool_null_and_key_order() {
    let out = yaml(
        r#"
input: {}
output: {}
graph:
  variables:
    huge: {type: integer, default: 1234567890123456789012345678901234567890}
    tiny: {type: number, default: 1.000000000000000000000000001}
    map:
      type: object
      additionalProperties: {type: integer}
      default: {"10": 10, "2": 2, "0": 0, "a": 3}
  nodes: {work: {run: {executor: builtin, content: work}}}
  edges: ["start -> work", "work -> end"]
"#,
    );
    ok(&out);
    let text = serde_json::to_string(&out.bundle).unwrap();
    assert!(text.contains("1234567890123456789012345678901234567890"));
    assert!(text.contains("1.000000000000000000000000001"));
    assert!(text.contains(r#"{"10":10,"2":2,"0":0,"a":3}"#));
    for literal in [json!(1.0), json!(true), Value::Null] {
        let v = builtin(
            "assign",
            json!({"source":{"value":literal}}),
            Some(json!({"value":"x"})),
            json!({}),
            json!({"x":{"type":"integer"}}),
            &[],
        );
        error(&compile(v), "type.compatibility");
    }
    for (target, literal) in [
        (
            json!({"type":"array","items":{"type":"integer"}}),
            json!([]),
        ),
        (
            json!({"type":"object","additionalProperties":{"type":"string"}}),
            json!({}),
        ),
        (
            json!({"type":["array","null"],"items":{"type":"boolean"}}),
            Value::Null,
        ),
    ] {
        ok(&compile(builtin(
            "assign",
            json!({"source":{"value":literal}}),
            Some(json!({"value":"x"})),
            json!({}),
            json!({"x":target}),
            &[],
        )));
    }
    let record = json!({"type":"object","properties":{"id":{"type":"integer"},"label":{"type":"string"}},"required":["id"]});
    for (literal, valid) in [
        (json!({"id":7,"label":"seven"}), true),
        (json!({"label":"missing id"}), false),
        (json!({"id":7,"extra":true}), false),
        (json!({"id":"seven"}), false),
    ] {
        let out = compile(builtin(
            "assign",
            json!({"source":{"value":literal}}),
            Some(json!({"value":"x"})),
            json!({}),
            json!({"x":record}),
            &[],
        ));
        if valid {
            ok(&out);
        } else {
            only_error(
                &out,
                "type.compatibility",
                "/graph/nodes/step/with/input/source",
            );
        }
    }
    for arg in [json!([]), json!({}), json!({"value":1,"extra":2})] {
        error(
            &compile(builtin(
                "assign",
                json!({"source":arg}),
                Some(json!({"value":"x"})),
                json!({}),
                json!({"x":{"type":"integer"}}),
                &[],
            )),
            "schema.argument",
        );
    }
}

#[test]
fn typespec_is_closed_recursive_and_enum_equality_is_exact() {
    for t in [
        json!({}),
        json!({"type":"any"}),
        json!({"type":"null"}),
        json!({"type":["integer","string"]}),
        json!({"type":"array","items":true}),
        json!({"type":"array","items":{"type":"string"},"enum":[1]}),
        json!({"type":"object"}),
        json!({"type":"object","properties":{}}),
        json!({"type":"object","properties":{"x":{"type":"integer"}},"additionalProperties":false}),
        json!({"type":"object","additionalProperties":true}),
        json!({"type":"object","properties":{"x":{"type":"integer"}},"required":["x","x"]}),
        json!({"type":"string","enum":[null]}),
        json!({"type":"integer","enum":[true]}),
        json!({"type":"number","enum":[1,1.0]}),
        json!({"type":"integer","minimum":0}),
    ] {
        let out = compile(
            json!({"input":{"x":t},"output":{},"run":{"executor":"builtin","content":"work"}}),
        );
        error(&out, "type.schema");
    }
    let numeric: TypeSpec = serde_json::from_str(
        r#"{"type":"number","enum":[123456789012345678901234567890,1e10000]}"#,
    )
    .unwrap();
    assert!(numeric.accepts(&serde_json::from_str("123456789012345678901234567890.0").unwrap()));
    assert!(!numeric.accepts(&serde_json::from_str("123456789012345678901234567891").unwrap()));
    assert!(numeric.accepts(&serde_json::from_str("10e9999").unwrap()));
    ok(&compile(
        json!({"input":{},"output":{},"run":{"executor":"builtin","content":"work"},"retry":{"max_attempts":2,"backoff_ms":0}}),
    ));
}

fn subtype(actual: Value, expected: Value, valid: bool) {
    let main = json!({"input":{"value":actual},"output":{},"graph":{"nodes":{"call":{"use":"workflow:target","with":{"input":{"p":"value"}}}},"edges":["start -> call","call -> end"]}});
    let target = json!({"input":{"p":expected},"output":{},"run":{"executor":"builtin","content":"read variables.p"}});
    let out = compile_sources(
        "main",
        [
            ("main".into(), source(main)),
            ("target".into(), source(target)),
        ],
    );
    if valid {
        ok(&out);
    } else {
        only_error(&out, "type.compatibility", "/graph/nodes/call/with/input/p");
    }
}

#[test]
fn subtype_variance_nullable_enum_and_closed_record_rules() {
    subtype(
        json!({"type":"integer"}),
        json!({"type":["number","null"]}),
        true,
    );
    subtype(json!({"type":"number"}), json!({"type":"integer"}), false);
    subtype(
        json!({"type":["integer","null"]}),
        json!({"type":"integer"}),
        false,
    );
    subtype(
        json!({"type":"string","enum":["b","a"]}),
        json!({"type":"string","enum":["a","b","c"]}),
        true,
    );
    subtype(
        json!({"type":"string"}),
        json!({"type":"string","enum":["a"]}),
        false,
    );
    subtype(
        json!({"type":"array","items":{"type":"integer"}}),
        json!({"type":"array","items":{"type":"number"}}),
        true,
    );
    subtype(
        json!({"type":"object","additionalProperties":{"type":"integer"}}),
        json!({"type":"object","additionalProperties":{"type":"number"}}),
        true,
    );
    let small = json!({"type":"object","properties":{"x":{"type":"integer"}}});
    let optional = json!({"type":"object","properties":{"x":{"type":"number"},"y":{"type":"string"}},"required":["x"]});
    subtype(small.clone(), optional.clone(), true);
    subtype(optional.clone(), small, false);
    subtype(
        json!({"type":"object","properties":{"x":{"type":"number"},"y":{"type":["string","null"]}},"required":["x"]}),
        optional,
        false,
    );
    let required =
        json!({"type":"object","properties":{"id":{"type":"integer"}},"required":["id"]});
    let optional = json!({"type":"object","properties":{"id":{"type":"integer"}},"required":[]});
    subtype(optional.clone(), required.clone(), false);
    subtype(required, optional, true);
    let out = compile(
        json!({"input":{},"output":{"x":{"type":"integer"}},"graph":{"nodes":{"n":{"run":{"executor":"builtin","content":"work"},"output":{"x":{"type":"number"}}}},"edges":["start -> n","n -> end"]}}),
    );
    error(&out, "symbol.conflicting_type");
    no_facts(&out);
    ok(&compile(
        json!({"input":{},"output":{"x":{"type":"string","enum":["a","b"],"description":"root"}},"graph":{"nodes":{"n":{"run":{"executor":"builtin","content":"work"},"output":{"x":{"type":"string","enum":["b","a"],"description":"node"}}}},"edges":["start -> n","n -> end"]}}),
    ));
}

#[test]
fn field_projection_checks_bindings_and_leaves_run_content_opaque() {
    let person = json!({"type":"object","properties":{"name":{"type":"string"},"profile":{"type":"object","properties":{"label":{"type":"string"}}}},"required":["name"]});
    let make = |path: &str, target: Value| {
        builtin(
            "assign",
            json!({"source":path}),
            Some(json!({"value":"result"})),
            json!({"person":person,"map":{"type":"object","additionalProperties":{"type":"string"}},"array":{"type":"array","items":{"type":"string"}}}),
            json!({"result":target}),
            &[],
        )
    };
    ok(&compile(make("person.name", json!({"type":"string"}))));
    ok(&compile(make(
        "person.profile",
        json!({"type":["object","null"],"properties":{"label":{"type":"string"}}}),
    )));
    for path in [
        "person.profile.label",
        "person.unknown",
        "map.key",
        "array.item",
        "person.name.first",
    ] {
        error(
            &compile(make(path, json!({"type":"string"}))),
            "type.projection",
        );
    }
    ok(&compile(
        json!({"input":{"x":{"type":"string"}},"output":{},"graph":{"nodes":{"n":{"run":{"executor":"builtin","content":"read variables.x."}}},"edges":["start -> n","n -> end"]}}),
    ));
    let out = compile(
        json!({"input":{"x":{"type":"string"}},"output":{},"graph":{"nodes":{"n":{"input":["x"],"run":{"executor":"builtin","content":"read avariables.missing and variables.x."}}},"edges":["start -> n","n -> end"]}}),
    );
    ok(&out);
}

#[test]
fn closure_recursive_signatures_and_explicit_call_mapping() {
    let a = json!({"input":{},"output":{"a":{"type":"integer"}},"graph":{"nodes":{"call":{"use":"workflow:opaque/../B","with":{"output":{"b":"a"}}}},"edges":["start -> call","call -> end"]}});
    let b = json!({"input":{},"output":{"b":{"type":"integer"}},"graph":{"nodes":{"call":{"use":"workflow:A","with":{"output":{"a":"b"}}}},"edges":["start -> call","call -> end"]}});
    let out = compile_sources(
        "A",
        [
            ("A".into(), source(a.clone())),
            ("opaque/../B".into(), source(b)),
            (
                "ignored".into(),
                SourceDocument {
                    format: SourceFormat::Yaml,
                    text: "[broken".into(),
                },
            ),
        ],
    );
    ok(&out);
    assert_eq!(
        out.bundle.unwrap().workflows.keys().collect::<Vec<_>>(),
        vec!["A", "opaque/../B"]
    );
    let out = compile_sources("A", [("A".into(), source(a.clone()))]);
    error(&out, "type.unresolved_workflow");
    assert_eq!(
        out.diagnostics
            .iter()
            .find(|d| d.code == "type.unresolved_workflow")
            .unwrap()
            .node_id
            .as_deref(),
        Some("call")
    );
    no_facts(&out);
    let bad = source(
        json!({"input":{},"output":{"b":{"type":"any"}},"run":{"executor":"builtin","content":"work"}}),
    );
    error(
        &compile_sources("A", [("A".into(), source(a)), ("opaque/../B".into(), bad)]),
        "type.unresolved_workflow",
    );
    let target = source(
        json!({"input":{"p":{"type":"integer"}},"output":{"x":{"type":"integer"},"ignored":{"type":"string"}},"branches":{"yes":{"description":"ok","output":["x"]},"no":{"description":"skip","output":[]}},"run":{"executor":"builtin","content":"work"}}),
    );
    let main = json!({"input":{},"output":{},"graph":{"variables":{"result":{"type":"number"}},"nodes":{"call":{"use":"workflow:target","with":{"input":{"p":1},"output":{"x":"result"}}}},"edges":["start -> call","call.yes -> end","call.no -> end"]}});
    let out = compile_sources(
        "main",
        [
            ("main".into(), source(main.clone())),
            ("target".into(), target.clone()),
        ],
    );
    ok(&out);
    let ports = derive_node_ports(&out.analysis[0], "call");
    assert_eq!(ports.len(), 2);
    assert_eq!(ports[0].branch_id.as_deref(), Some("yes"));
    assert_eq!(ports[0].output, vec!["result"]);
    assert_eq!(ports[1].branch_id.as_deref(), Some("no"));
    assert!(ports[1].output.is_empty());
    for (read_branch, old_value, valid) in [
        ("yes", false, true),
        ("no", false, false),
        ("no", true, true),
    ] {
        let mut consume = main.clone();
        consume["graph"]["nodes"]["read"] = json!({"input":["result"],"run":{"executor":"builtin","content":"read variables.result"}});
        consume["graph"]["edges"] = json!([
            "start -> call",
            if read_branch == "yes" {
                "call.yes -> read"
            } else {
                "call.yes -> end"
            },
            if read_branch == "no" {
                "call.no -> read"
            } else {
                "call.no -> end"
            },
            "read -> end"
        ]);
        if old_value {
            consume["graph"]["variables"]["result"]["default"] = json!(42);
        }
        let out = compile_sources(
            "main",
            [
                ("main".into(), source(consume)),
                ("target".into(), target.clone()),
            ],
        );
        if valid {
            ok(&out);
        } else {
            only_error(&out, "assignment.read", "/graph/nodes/read/input/0");
            no_facts(&out);
        }
    }
    let mut invalid = main.clone();
    invalid["graph"]["nodes"]["call"]["with"]["input"] = json!({});
    error(
        &compile_sources(
            "main",
            [
                ("main".into(), source(invalid)),
                ("target".into(), target.clone()),
            ],
        ),
        "type.call_input",
    );
    let mut invalid = main;
    invalid["graph"]["nodes"]["call"]["catch"] = json!({"yes":"*"});
    error(
        &compile_sources(
            "main",
            [("main".into(), source(invalid)), ("target".into(), target)],
        ),
        "type.branch_collision",
    );
}

#[test]
fn multiple_errors_have_deterministic_canvas_addresses() {
    let v = json!({"input":{},"output":{},"graph":{"nodes":{"one":{"run":{"executor":"builtin","content":""},"unknown":true},"two":{"input":["missing"],"run":{"executor":"builtin","content":"read variables.missing"}},"inside":{"graph":{"nodes":{"bad":{"input":["other"],"run":{"executor":"builtin","content":"work"}}},"edges":["start -> bad","bad -> end"]}}},"edges":["start -> two","two -> inside","inside -> end","start -> one -> end"]}});
    let out = compile(v.clone());
    for code in [
        "schema.run",
        "schema.unknown_field",
        "symbol.undeclared",
        "schema.edge",
    ] {
        error(&out, code);
    }
    let inner = out
        .diagnostics
        .iter()
        .find(|d| d.node_id.as_deref() == Some("bad") && d.code == "symbol.undeclared")
        .unwrap();
    assert_eq!(inner.scope_graph_path, vec!["inside"]);
    assert_eq!(
        inner.param_path,
        "/graph/nodes/inside/graph/nodes/bad/input/0"
    );
    let edge = out
        .diagnostics
        .iter()
        .find(|d| d.code == "schema.edge")
        .unwrap();
    assert_eq!(edge.edge_index, Some(3));
    no_facts(&out);
    for _ in 0..4 {
        assert_eq!(
            serde_json::to_string(&out).unwrap(),
            serde_json::to_string(&compile(v.clone())).unwrap()
        );
    }
}

#[test]
fn json_literal_keys_never_enter_serde_number_tag_deserialization() {
    let doc = json!({"input":{},"output":{},"graph":{"variables":{"m":{"type":"object","additionalProperties":{"type":"string"},"default":{"$serde_json::private::Number":"ordinary map value"}}},"nodes":{"n":{"run":{"executor":"builtin","content":"work"}}},"edges":["start -> n","n -> end"]}});
    let out = compile(doc);
    ok(&out);
    let text = serde_json::to_string(&out.bundle).unwrap();
    assert!(text.contains(r#""$serde_json::private::Number":"ordinary map value""#));
}

#[test]
fn directly_recursive_body_is_checked_once_using_its_signature() {
    let doc = json!({"input":{},"output":{"x":{"type":"integer"}},"graph":{"nodes":{"self_call":{"use":"workflow:main","with":{"output":{"x":"x"}}}},"edges":["start -> self_call","self_call -> end"]}});
    let out = compile(doc);
    ok(&out);
    assert_eq!(out.bundle.unwrap().workflows.len(), 1);
}

#[test]
fn workflow_output_diagnostic_points_to_the_explicit_binding() {
    let main = json!({"input":{},"output":{"local":{"type":"integer"}},"graph":{"nodes":{"call":{"use":"workflow:target","with":{"output":{"remote":"local"}}}},"edges":["start -> call","call -> end"]}});
    let target = json!({"input":{},"output":{"remote":{"type":"string"}},"run":{"executor":"builtin","content":"work"}});
    let out = compile_sources(
        "main",
        [
            ("main".into(), source(main)),
            ("target".into(), source(target)),
        ],
    );
    error(&out, "type.output");
    let diagnostic = out
        .diagnostics
        .iter()
        .find(|d| d.code == "type.output")
        .unwrap();
    assert_eq!(
        diagnostic.param_path,
        "/graph/nodes/call/with/output/remote"
    );
}
