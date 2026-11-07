//! Text boundaries exercise the same API used by the real Python extension.
use axcutor_core::kernel::*;
use serde_json::{Value, json};

fn builtin(op: &str, input: Value, output: Value, env: &str, symbols: &str) -> Value {
    serde_json::from_str(
        &eval_builtin_json(
            op,
            &json!({"input":input,"output":output,"output_present":true}).to_string(),
            env,
            symbols,
        )
        .unwrap(),
    )
    .unwrap()
}
fn literal(v: Value) -> Value {
    json!({"kind":"const","value":v})
}
fn path(root: &str) -> Value {
    json!({"kind":"path","path":{"root":root,"fields":[]}})
}

#[test]
fn text_arguments_preserve_numbers_objects_and_optional_leaves() {
    let value = r#"{"$serde_json::private::Number":"42"}"#;
    assert_eq!(
        eval_arg_json(&format!(r#"{{"kind":"const","value":{value}}}"#), "{}").unwrap(),
        value
    );
    assert_eq!(
        eval_arg_json(
            r#"{"kind":"path","path":{"root":"a","fields":["leaf"]}}"#,
            r#"{"a":{}}"#
        )
        .unwrap(),
        "null"
    );
    for (arg, env) in [
        (r#"{"kind":"path","path":{"root":"a","fields":[]}}"#, "{}"),
        (
            r#"{"kind":"path","path":{"root":"a","fields":["x","y"]}}"#,
            r#"{"a":{}}"#,
        ),
    ] {
        assert!(matches!(
            eval_arg_json(arg, env),
            Err(KernelError::Engine(_))
        ));
    }
    assert!(eval_arg_json(r#"{"kind":"const","value":{"x":1,"x":2}}"#, "{}").is_err());
    assert_eq!(
        eval_arg_json(
            r#"{"kind":"const","value":0.12345678901234567890123456789}"#,
            "{}"
        )
        .unwrap(),
        "0.12345678901234567890123456789"
    );
}

#[test]
fn schema_boundary_projects_only_branch_fields_and_rejects_deep_violations() {
    let schema = r#"{"n":{"type":"integer"},"record":{"type":"object","properties":{"x":{"type":"boolean"}}}}"#;
    let branches = r#"{"ok":{"description":"","output":["n"]},"other":{"description":"","output":["record"]}}"#;
    assert_eq!(validate_run_contract_json(r#"{"branch":"ok","output":{"n":123456789012345678901234567890,"record":false,"extra":null}}"#, schema, branches).unwrap(), r#"{"branch":"ok","output":{"n":123456789012345678901234567890}}"#);
    for response in [
        r#"{"output":{"n":1}}"#,
        r#"{"branch":"ok","output":{}}"#,
        r#"{"branch":"ok","output":{"n":true}}"#,
        r#"{"branch":"ok","output":{"n":1.0}}"#,
        r#"{"branch":"other","output":{"record":{"x":true,"extra":1}}}"#,
        "invalid JSON",
    ] {
        assert!(matches!(
            validate_run_contract_json(response, schema, branches),
            Err(KernelError::Contract(_))
        ));
    }
    assert!(matches!(
        validate_input_json(r#"{"n":1,"extra":2}"#, r#"{"n":{"type":"integer"}}"#),
        Err(KernelError::Input(_))
    ));
    assert!(matches!(
        validate_input_json("{}", r#"{"n":{"type":["integer","null"]}}"#),
        Err(KernelError::Input(_))
    ));
    assert!(validate_input_json(r#"{"n":null}"#, r#"{"n":{"type":["integer","null"]}}"#).is_ok());
}

#[test]
fn exact_arithmetic_and_comparison_use_static_operand_types() {
    let math = |op: &str, left: Value, right: Value, env: &str, symbols: &str| {
        builtin(
            "math",
            json!({"op":literal(json!(op)),"left":left,"right":right}),
            json!({"result":"z"}),
            env,
            symbols,
        )
    };
    assert_eq!(
        math(
            "mul",
            path("x"),
            literal(json!(3)),
            r#"{"x":999999999999999999999999999999}"#,
            r#"{"x":{"type":"integer"}}"#
        )["output"]["z"]
            .to_string(),
        "2999999999999999999999999999997"
    );
    assert_eq!(
        math(
            "add",
            path("x"),
            literal(json!(1)),
            r#"{"x":1}"#,
            r#"{"x":{"type":"number"}}"#
        )["output"]["z"]
            .to_string(),
        "2.0"
    );
    for (left, right, expected) in [(-5, 3, 1), (5, -3, -1), (-5, -3, -2)] {
        assert_eq!(
            math(
                "mod",
                literal(json!(left)),
                literal(json!(right)),
                "{}",
                "{}"
            )["output"]["z"],
            expected
        );
    }
    assert_eq!(
        math("div", literal(json!(1)), literal(json!(0)), "{}", "{}"),
        json!({"branch":"none","output":{}})
    );
    assert_eq!(
        math("div", literal(json!(5)), literal(json!(2)), "{}", "{}"),
        json!({"branch":"some","output":{"z":2.5}})
    );
    let compare = builtin(
        "compare",
        json!({"op":literal(json!("gt")),"left":path("x"),"right":path("y")}),
        json!({"result":"z"}),
        r#"{"x":9007199254740993,"y":9007199254740992.0}"#,
        "{}",
    );
    assert_eq!(compare["output"]["z"], true);
    for (left, right, op, expected) in [
        ("1e99999999", "9e99999998", "gt", true),
        ("-1e99999999", "-9e99999998", "lt", true),
        ("1.0000000000000000001", "1.0", "gt", true),
        ("1", "1.0", "eq", true),
        ("true", "1", "eq", false),
        (r#"{"b":2,"a":[1.0]}"#, r#"{"a":[1],"b":2}"#, "eq", true),
    ] {
        let env = format!(r#"{{"x":{left},"y":{right}}}"#);
        assert_eq!(
            builtin(
                "compare",
                json!({"op":literal(json!(op)),"left":path("x"),"right":path("y")}),
                json!({"result":"z"}),
                &env,
                "{}"
            )["output"]["z"],
            expected
        );
    }
    assert!(matches!(eval_builtin_json("math", &json!({"input":{"op":literal(json!("mul")),"left":path("x"),"right":path("x")},"output":{"result":"z"},"output_present":true}).to_string(),r#"{"x":1e308}"#,r#"{"x":{"type":"number"}}"#),Err(KernelError::Engine(_))));
}

#[test]
fn missing_slots_are_distinct_from_null_and_collections_keep_order() {
    assert_eq!(
        builtin(
            "array_concat",
            json!({"left":literal(json!([1,2])),"right":literal(json!([3,4]))}),
            json!({"result":"items"}),
            "{}",
            "{}"
        ),
        json!({"branch":null,"output":{"items":[1,2,3,4]}})
    );
    for op in ["array_get", "map_get"] {
        let (source, key) = if op == "array_get" {
            (json!([null]), json!({"index":literal(json!(-1))}))
        } else {
            (json!({"2":null}), json!({"key":literal(json!("2"))}))
        };
        let mut input = key.as_object().unwrap().clone();
        input.insert("source".into(), literal(source));
        assert_eq!(
            builtin(op, Value::Object(input), json!({"value":"v"}), "{}", "{}"),
            json!({"branch":"some","output":{"v":null}})
        );
    }
    assert_eq!(
        builtin(
            "array_shift",
            json!({"source":literal(json!([]))}),
            json!({"item":"v","rest":"r"}),
            "{}",
            "{}"
        ),
        json!({"branch":"empty","output":{}})
    );
    let result = builtin(
        "map_set",
        json!({"source":literal(json!({"2":2,"0":0})),"key":literal(json!("2")),"value":literal(json!(9))}),
        json!({"result":"m"}),
        "{}",
        "{}",
    );
    assert_eq!(
        result["output"]["m"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        vec!["2", "0"]
    );
    assert_eq!(
        builtin(
            "length",
            json!({"source":literal(json!("a😀e\u{301}"))}),
            json!({"result":"n"}),
            "{}",
            "{}"
        )["output"]["n"],
        4
    );
    assert_eq!(
        builtin(
            "array_get",
            json!({"source":literal(json!([1])),"index":literal("999999999999999999999999999999".parse::<Value>().unwrap()),"default":literal(json!(8))}),
            json!({"value":"v"}),
            "{}",
            "{}"
        )["output"]["v"],
        8
    );
}
