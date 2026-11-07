//! Observable topology, fixed-point, iteration and canvas behavior.
mod support;
use axcutor_core::*;
use serde_json::{Value, json};
use support::*;

#[test]
fn call_arguments_and_iteration_sources_require_assigned_roots() {
    let target =
        source(json!({"input":{"p":{"type":"integer"}},"output":{},"run":"read variables.p"}));
    for (node, source_type, default, output_type, path) in [
        (
            assign(json!("source"), "x"),
            json!({"type":"integer"}),
            json!(1),
            json!({"type":"integer"}),
            "/graph/nodes/n/with/input/source",
        ),
        (
            json!({"use":"workflow:target","with":{"input":{"p":"source"}}}),
            json!({"type":"integer"}),
            json!(1),
            json!({"type":"integer"}),
            "/graph/nodes/n/with/input/p",
        ),
        (
            json!({"run":"work","for_each":{"source":"source","item":"item"},"output":{"x":{"type":"integer"}}}),
            json!({"type":"array","items":{"type":"integer"}}),
            json!([1]),
            json!({"type":"array","items":{"type":"integer"}}),
            "/graph/nodes/n/for_each/source",
        ),
    ] {
        for initialized in [false, true] {
            let mut doc = graph(json!({"n":node}), json!(["start -> n", "n -> end"]));
            doc["graph"]["variables"] = json!({"source":source_type,"x":output_type});
            if initialized {
                doc["graph"]["variables"]["source"]["default"] = default.clone();
            }
            let out = compile_sources(
                "main",
                [
                    ("main".into(), source(doc)),
                    ("target".into(), target.clone()),
                ],
            );
            if initialized {
                ok(&out);
            } else {
                only_error(&out, "assignment.read", path);
                no_facts(&out);
            }
        }
    }
}

#[test]
fn loop_greatest_fixed_point_preserves_entry_facts_without_inventing_first_iteration_values() {
    let doc = json!({"input":{"flag":{"type":"boolean"}},"output":{"x":{"type":"integer"}},"graph":{"variables":{"x":{"type":"integer","default":0},"late":{"type":"string"}},"nodes":{
        "test":{"input":["x","flag"],"branches":{"again":"continue","done":"stop"},"run":"test variables.x"},
        "body":{"input":["x"],"output":{"x":{"type":"integer"},"late":{"type":"string"}},"run":"work"}
    },"edges":["start -> test","test.again -> body","body -> test","test.done -> end"]}});
    let out = compile(doc.clone());
    ok(&out);
    assert_eq!(
        out.analysis[0].nodes["test"].definite_in.as_ref().unwrap(),
        &vec!["flag".to_owned(), "x".to_owned()]
    );
    let mut wrong = doc;
    wrong["graph"]["nodes"]["test"]["input"] = json!(["x", "flag", "late"]);
    let out = compile(wrong);
    error(&out, "assignment.read");
    no_facts(&out);
}

#[test]
fn branch_subsets_meet_at_joins_and_only_special_branches_kill_old_values() {
    let mut doc = json!({"input":{},"output":{},"graph":{"variables":{"x":{"type":"string"}},"nodes":{
        "choose":{"output":{"x":{"type":"string"}},"branches":{"yes":"write","no":{"description":"skip","output":[]}},"run":"choose"},
        "read":{"input":["x"],"run":"read variables.x"}
    },"edges":["start -> choose","choose.yes -> read","choose.no -> read","read -> end"]}});
    error(&compile(doc.clone()), "assignment.read");
    doc["graph"]["variables"]["x"]["default"] = json!("old");
    ok(&compile(doc));
    for op in ["unwrap", "array_get", "map_get", "math", "array_shift"] {
        let (input, output, success, failure) = match op {
            "unwrap" => (
                json!({"source":"maybe"}),
                json!({"value":"x"}),
                "some",
                "none",
            ),
            "array_get" => (
                json!({"source":"array","index":0}),
                json!({"value":"x"}),
                "some",
                "none",
            ),
            "map_get" => (
                json!({"source":"map","key":{"value":"id"}}),
                json!({"value":"x"}),
                "some",
                "none",
            ),
            "math" => (
                json!({"op":{"value":"mod"},"left":1,"right":0}),
                json!({"result":"x"}),
                "some",
                "none",
            ),
            _ => (
                json!({"source":"array"}),
                json!({"item":"x","rest":"rest"}),
                "some",
                "empty",
            ),
        };
        let doc = json!({"input":{"maybe":{"type":["integer","null"]},"array":{"type":"array","items":{"type":"integer"}},"map":{"type":"object","additionalProperties":{"type":"integer"}}},"output":{},"graph":{"variables":{"x":{"type":"integer","default":42},"rest":{"type":"array","items":{"type":"integer"}}},"nodes":{"step":{"use":format!("builtin:{op}"),"with":{"input":input,"output":output}},"read":{"input":["x"],"run":"read variables.x"}},"edges":["start -> step",format!("step.{success} -> end"),format!("step.{failure} -> read"),"read -> end"]}});
        let out = compile(doc);
        error(&out, "assignment.read");
        no_facts(&out);
        let p = derive_node_ports(&out.analysis[0], "step");
        assert_eq!(p[1].kill_vars, vec!["x"]);
    }
}

#[test]
fn shift_empty_neither_assigns_nor_erases_rest() {
    let mut doc = json!({"input":{"array":{"type":"array","items":{"type":"integer"}}},"output":{},"graph":{"variables":{"x":{"type":"integer"},"rest":{"type":"array","items":{"type":"integer"}}},"nodes":{
        "step":{"use":"builtin:array_shift","with":{"input":{"source":"array"},"output":{"item":"x","rest":"rest"}}},
        "read":{"input":["rest"],"run":"read variables.rest"}
    },"edges":["start -> step","step.some -> end","step.empty -> read","read -> end"]}});
    error(&compile(doc.clone()), "assignment.read");
    doc["graph"]["variables"]["rest"]["default"] = json!([]);
    ok(&compile(doc));
}

#[test]
fn catch_preserves_old_values_but_never_promises_new_outputs() {
    let mut doc = json!({"input":{"x":{"type":"string"}},"output":{"x":{"type":"string"}},"graph":{"nodes":{"n":{"output":{"x":{"type":"string"}},"catch":{"err":"*"},"run":"work"}},"edges":["start -> n","n -> end","n.err -> end"]}});
    let out = compile(doc.clone());
    ok(&out);
    let p = derive_node_ports(&out.analysis[0], "n");
    assert_eq!(p[1].kind, PortKind::Catch);
    assert!(p[1].output.is_empty());
    assert!(p[1].kill_vars.is_empty());
    doc["input"] = json!({});
    let out = compile(doc);
    error(&out, "assignment.exit");
    no_facts(&out);
}

#[test]
fn fork_uses_same_snapshot_then_unions_outputs_at_join() {
    let mut doc = graph(
        json!({
            "a":{"output":{"x":{"type":"string"}},"run":"produce"},
            "b":{"output":{"y":{"type":"integer"}},"run":"produce"},
            "join":{"input":["x","y"],"run":"consume variables.x and variables.y"}
        }),
        json!(["start -> [a,b] -> join", "join -> end"]),
    );
    let out = compile(doc.clone());
    ok(&out);
    let g = &out.analysis[0];
    assert!(g.nodes["a"].definite_in.as_ref().unwrap().is_empty());
    assert!(g.nodes["b"].definite_in.as_ref().unwrap().is_empty());
    assert_eq!(
        g.nodes["join"].definite_in.as_ref().unwrap(),
        &vec!["x".to_owned(), "y".to_owned()]
    );
    doc["graph"]["nodes"]["b"]["input"] = json!(["x"]);
    let out = compile(doc);
    error(&out, "assignment.read");
    no_facts(&out);
    let branch = json!({"input":{"maybe":{"type":["integer","null"]}},"output":{},"graph":{"variables":{"x":{"type":"integer","default":1}},"nodes":{
        "un":{"use":"builtin:unwrap","with":{"input":{"source":"maybe"},"output":{"value":"x"}}},
        "a":{"input":["x"],"run":"work"},"b":{"run":"work"}
    },"edges":["start -> un","un.some -> end","un.none -> [a,b] -> end"]}});
    error(&compile(branch), "assignment.read");
}

#[test]
fn concurrent_write_checks_only_parent_boundary_and_worker_exclusivity() {
    let node = |field: &str| json!({"output":{field:{"type":"string"}},"graph":{"variables":{"private":{"type":"integer","default":0}},"nodes":{"n":{"run":"work","output":{field:{"type":"string"}}}},"edges":["start -> n","n -> end"]}});
    let doc = graph(
        json!({"a":node("x"),"b":node("y")}),
        json!(["start -> [a,b] -> end"]),
    );
    ok(&compile(doc));
    let doc = graph(
        json!({"a":node("x"),"b":node("x")}),
        json!(["start -> [a,b] -> end"]),
    );
    error(&compile(doc), "topology.concurrent_write");
    for edges in [
        json!(["start -> a", "a -> [a,b] -> end"]),
        json!(["start -> [a,b] -> a"]),
        json!(["start -> [a,b] -> end", "a -> end"]),
        json!(["start -> [a,b] -> c", "c -> [a,d] -> end"]),
    ] {
        let out = compile(graph(
            json!({"a":{"run":"a"},"b":{"run":"b"},"c":{"run":"c"},"d":{"run":"d"}}),
            edges,
        ));
        error(&out, "topology.worker_exclusive");
        no_facts(&out);
    }
    let out = compile(graph(
        json!({"a":{"run":"a","catch":{"err":"*"}},"b":{"run":"b"}}),
        json!(["start -> [a,b] -> end"]),
    ));
    error(&out, "topology.worker_ports");
}

#[test]
fn topology_requires_complete_ports_reachability_and_escapable_cycles() {
    let cases = [
        (
            graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> n", "start -> end", "n -> end"]),
            ),
            "topology.duplicate_source",
        ),
        (
            graph(json!({"n":{"run":"work"}}), json!(["start -> n"])),
            "topology.missing_route",
        ),
        (
            graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> missing", "n -> end"]),
            ),
            "topology.unknown_node",
        ),
        (
            graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> end", "n -> end"]),
            ),
            "topology.unreachable",
        ),
        (
            graph(json!({"n":{"run":"work"}}), json!(["start -> n", "n -> n"])),
            "topology.no_exit",
        ),
        (
            graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> n", "n -> fail"]),
            ),
            "topology.no_normal_exit",
        ),
        (
            graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> n", "n -> end.unknown"]),
            ),
            "topology.end_branch",
        ),
    ];
    for (v, code) in cases {
        let out = compile(v);
        error(&out, code);
        no_facts(&out);
    }
    let doc = json!({"input":{},"output":{},"branches":{"yes":"ok"},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}});
    error(&compile(doc), "topology.end_branch");
}

fn iteration(inline: bool, map: bool) -> Value {
    let single = json!({"type":"string"});
    let collection = if map {
        json!({"type":"object","additionalProperties":single})
    } else {
        json!({"type":"array","items":single})
    };
    let mut iter = json!({"source":"items","item":"item","index":"index","concurrency":4});
    if map {
        iter["key"] = json!("key");
    }
    let node = if inline {
        json!({"for_each":iter,"output":{"results":single},"branches":{"keep":"yes","drop":{"description":"skip","output":[]}},"graph":{"nodes":{"test":{"input":["item"],"output":{"results":single},"branches":{"keep":"yes","drop":{"description":"skip","output":[]}},"run":"read variables.item"}},"edges":["start -> test","test.keep -> end.keep","test.drop -> end.drop"]}})
    } else {
        json!({"for_each":iter,"input":["item","index"],"output":{"results":single},"branches":{"keep":"yes","drop":{"description":"skip","output":[]}},"catch":{"err":"*"},"run":"read variables.item"})
    };
    json!({"input":{"items":collection},"output":{"results":collection},"graph":{"nodes":{"each":node},"edges":["start -> each","each -> end"]}})
}

#[test]
fn foreach_action_inline_and_call_have_single_outputs_and_aggregate_boundaries() {
    for inline in [false, true] {
        for map in [false, true] {
            let out = compile(iteration(inline, map));
            ok(&out);
            let g = &out.analysis[0];
            let ports = derive_node_ports(g, "each");
            assert_eq!(ports.len(), 1);
            assert_eq!(ports[0].branch_id, None);
            assert_eq!(ports[0].output, vec!["results"]);
            assert_eq!(g.nodes["each"].injected["item"].base(), "string");
            assert_eq!(
                g.symbols["results"].base(),
                if map { "object" } else { "array" }
            );
            if inline {
                assert_eq!(out.analysis[1].symbols["results"].base(), "string");
                assert!(
                    out.analysis[1].nodes["test"]
                        .definite_in
                        .as_ref()
                        .unwrap()
                        .contains(&"item".into())
                );
            }
            let candidates = get_available_vars_at_node(g, "each", None);
            assert!(candidates.iter().any(|v| v.path.root == "item"));
            assert!(!candidates.iter().any(|v| v.path.root == "results"));
        }
    }
    let mut main = iteration(false, true);
    main["graph"]["nodes"]["each"] = json!({"use":"workflow:target","for_each":{"source":"items","item":"item","key":"key"},"with":{"input":{"p":"item"},"output":{"out":"results"}}});
    // Single iteration output is Array<string>, so aggregation yields Map<Array<string>>.
    main["output"]["results"]["additionalProperties"] =
        json!({"type":"array","items":{"type":"string"}});
    let target = source(
        json!({"input":{"p":{"type":"string"}},"output":{"out":{"type":"array","items":{"type":"string"}}},"branches":{"some":"keep","empty":{"description":"skip","output":[]}},"run":"work"}),
    );
    let out = compile_sources(
        "main",
        [("main".into(), source(main)), ("target".into(), target)],
    );
    ok(&out);
}

#[test]
fn foreach_rejects_missing_lift_targets_capture_collisions_and_wrong_sources() {
    let mut doc = iteration(false, false);
    doc["output"]["results"] = json!({"type":"string"});
    error(&compile(doc), "type.output");
    let mut doc = iteration(false, false);
    doc["output"] = json!({});
    error(&compile(doc), "symbol.undeclared_output");
    let mut doc = iteration(false, false);
    doc["graph"]["nodes"]["each"]["input"] = json!(["index"]);
    error(&compile(doc), "symbol.capture");
    let mut doc = iteration(true, false);
    doc["graph"]["nodes"]["each"]["input"] = json!(["item"]);
    error(&compile(doc), "symbol.injected_collision");
    let mut doc = iteration(false, false);
    doc["input"]["item"] = json!({"type":"string"});
    error(&compile(doc), "symbol.injected_collision");
    let mut doc = iteration(true, false);
    doc["input"]["item"] = json!({"type":"integer"});
    ok(&compile(doc));
    let mut doc = iteration(false, false);
    doc["graph"]["nodes"]["each"]["for_each"]["key"] = json!("key");
    error(&compile(doc), "type.iteration_key");
    for bad in [
        json!({"type":["array","null"],"items":{"type":"string"}}),
        json!({"type":"string"}),
        json!({"type":"object","properties":{"value":{"type":"string"}}}),
    ] {
        let mut doc = iteration(false, false);
        doc["input"]["items"] = bad;
        error(&compile(doc), "type.iteration_source");
    }
    let mut doc = iteration(true, false);
    doc["graph"]["nodes"]["each"]["graph"]["variables"] = json!({"item":{"type":"string"}});
    error(&compile(doc), "symbol.host_collision");
    let mut doc = iteration(false, false);
    doc["graph"]["edges"] = json!(["start -> each", "each.keep -> end"]);
    error(&compile(doc), "topology.unknown_port");
}

#[test]
fn inline_capture_isolation_and_independent_partial_analysis() {
    let mut doc = graph(
        json!({
            "bad":{"input":["missing"],"run":"work"},
            "inside":{"output":{"x":{"type":"string"}},"graph":{"nodes":{"n":assign(json!({"value":"ready"}),"x")},"edges":["start -> n","n -> end"]}}
        }),
        json!(["start -> bad", "bad -> inside", "inside -> end"]),
    );
    let out = compile(doc.clone());
    error(&out, "symbol.undeclared");
    assert!(out.analysis[0].nodes["inside"].definite_in.is_none());
    assert!(out.analysis[1].nodes["n"].definite_in.is_some());
    doc["input"] = json!({"parent":{"type":"string"}});
    doc["graph"]["nodes"]["inside"]["graph"]["nodes"]["n"] = assign(json!("parent"), "x");
    error(&compile(doc.clone()), "type.projection");
    doc["graph"]["nodes"]["inside"]["input"] = json!(["parent"]);
    doc["graph"]["nodes"].as_object_mut().unwrap().remove("bad");
    doc["graph"]["edges"] = json!(["start -> inside", "inside -> end"]);
    ok(&compile(doc));
}

#[test]
fn completion_enumerates_legal_paths_and_uses_same_subtype_filter() {
    let doc = json!({"input":{"user":{"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"},"profile":{"type":"object","properties":{"bio":{"type":"string"}}},"optional":{"type":"object","properties":{"secret":{"type":"string"}}}},"required":["name","age","profile"]},"map":{"type":"object","additionalProperties":{"type":"integer"}},"array":{"type":"array","items":{"type":"integer"}}},"output":{},"graph":{"variables":{"unbound":{"type":"string"}},"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}});
    let out = compile(doc);
    ok(&out);
    let analysis = &out.analysis[0];
    let all = get_available_vars_at_node(analysis, "n", None);
    assert_eq!(
        all.iter().map(|v| v.path.text()).collect::<Vec<_>>(),
        vec![
            "user",
            "user.name",
            "user.age",
            "user.profile",
            "user.profile.bio",
            "user.optional",
            "map",
            "array"
        ]
    );
    assert!(
        all.iter()
            .find(|v| v.path.text() == "user.optional")
            .unwrap()
            .typespec
            .nullable
    );
    let filtered =
        get_available_vars_at_node(analysis, "n", Some(&typespec(json!({"type":"number"}))));
    assert_eq!(
        filtered.iter().map(|v| v.path.text()).collect::<Vec<_>>(),
        vec!["user.age"]
    );
    assert!(get_available_vars_at_node(analysis, "missing", None).is_empty());
    let text = serde_json::to_string(analysis).unwrap();
    assert_eq!(
        available_vars_json(&text, "n", None).unwrap(),
        serde_json::to_string(&all).unwrap()
    );
    assert_eq!(
        node_ports_json(&text, "n").unwrap(),
        serde_json::to_string(&derive_node_ports(analysis, "n")).unwrap()
    );
    assert!(available_vars_json(&text, "n", Some("{\"type\":\"any\"}")).is_err());
    assert!(compile_workflow_json("{}").is_err());
    assert!(node_ports_json("{}", "n").is_err());
}

#[test]
fn compiled_source_depth_boundary_round_trips_through_canvas_json() {
    for arrays in [124, 125, 126] {
        let mut t = json!({"type":"integer"});
        for _ in 0..arrays {
            t = json!({"type":"array","items":t});
        }
        for iterated in [false, true] {
            let mut doc = graph(
                json!({"n":{"run":"work"}}),
                json!(["start -> n", "n -> end"]),
            );
            doc["input"] = json!({"x":t});
            if iterated {
                doc["graph"]["variables"] =
                    json!({"result":{"type":"array","items":{"type":"integer"}}});
                doc["graph"]["nodes"]["n"] = json!({"run":"work","for_each":{"source":"x","item":"item"},"output":{"result":{"type":"integer"}}});
            }
            let out = compile(doc);
            if arrays == 126 {
                only_error(&out, "parse.source", "");
                continue;
            }
            ok(&out);
            let analysis = &out.analysis[0];
            let text = serde_json::to_string(analysis).unwrap();
            let all = get_available_vars_at_node(analysis, "n", None);
            assert_eq!(all.len(), if iterated { 2 } else { 1 });
            assert_eq!(all[0].path.text(), "x");
            if iterated {
                assert_eq!(all[1].path.text(), "item");
            }
            assert_eq!(
                available_vars_json(&text, "n", None).unwrap(),
                serde_json::to_string(&all).unwrap()
            );
            let expected = serde_json::to_string(&t).unwrap();
            let matching = available_vars_json(&text, "n", Some(&expected)).unwrap();
            assert_eq!(matching, serde_json::to_string(&all[..1]).unwrap());
            assert_eq!(
                node_ports_json(&text, "n").unwrap(),
                if iterated {
                    r#"[{"branch_id":null,"kind":"normal","output":["result"],"kill_vars":[]}]"#
                } else {
                    r#"[{"branch_id":null,"kind":"normal","output":[],"kill_vars":[]}]"#
                }
            );
        }
    }
}

#[test]
fn marker_objects_are_rejected_in_every_query_typespec_position() {
    let doc = json!({"input":{"x":{"type":"integer","enum":[1]}},"output":{},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}});
    let out = compile(doc.clone());
    ok(&out);
    let analysis = serde_json::to_value(&out.analysis[0]).unwrap();
    let text = analysis.to_string();
    let valid = r#"{"type":"number","enum":[1]}"#;
    let candidates = available_vars_json(&text, "n", Some(valid)).unwrap();
    assert_eq!(
        candidates,
        serde_json::to_string(&get_available_vars_at_node(&out.analysis[0], "n", None)).unwrap()
    );
    for marker in [
        "$serde_json::private::Number",
        "$serde_json::private::RawValue",
    ] {
        let bad = json!({"type":"number","enum":[{marker:"1"}]});
        for schema in [
            bad.clone(),
            json!({"type":"array","items":bad}),
            json!({"type":"object","properties":{"field":bad}}),
            json!({"type":"object","additionalProperties":bad}),
            json!({"type":"array","items":{"type":"object","properties":{"field":bad}}}),
        ] {
            let mut invalid_doc = doc.clone();
            invalid_doc["input"]["x"] = schema.clone();
            for format in [SourceFormat::Json, SourceFormat::Yaml] {
                let source = SourceDocument {
                    format,
                    text: invalid_doc.to_string(),
                };
                only_error(
                    &compile_sources("main", [("main".into(), source)]),
                    "type.schema",
                    "/input/x",
                );
            }
            assert!(
                serde_json::from_str::<axcutor_core::ast::TypeSpec>(&schema.to_string()).is_err()
            );
            assert!(
                available_vars_json(&text, "n", Some(&schema.to_string())).is_err(),
                "{marker}: {schema}"
            );
            for injected in [false, true] {
                let mut invalid = analysis.clone();
                if injected {
                    invalid["nodes"]["n"]["injected"]["item"] = schema.clone();
                } else {
                    invalid["symbols"]["x"] = schema.clone();
                }
                let invalid = invalid.to_string();
                assert!(
                    available_vars_json(&invalid, "n", None).is_err(),
                    "{marker}: {schema}"
                );
                assert!(
                    node_ports_json(&invalid, "n").is_err(),
                    "{marker}: {schema}"
                );
            }
        }
        // These strings remain ordinary keys when their surrounding data is legal.
        ok(&compile(builtin(
            "assign",
            json!({"source":{"value":{marker:"1"}}}),
            Some(json!({"value":"m"})),
            json!({}),
            json!({"m":{"type":"object","additionalProperties":{"type":"string"}}}),
            &[],
        )));
    }
}

#[test]
fn canvas_json_arguments_are_bounded_and_preserve_numbers_and_ordinary_keys() {
    let out = json_text(
        r#"{"input":{"x":{"type":"number","enum":[123456789012345678901234567890,1.000000000000000000000000001,1e10000]}},"output":{},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}}"#,
    );
    ok(&out);
    let mut analysis = out.analysis[0].clone();
    let number_key = "$serde_json::private::Number";
    let symbols = std::mem::take(&mut analysis.symbols);
    for key in [number_key, "$serde_json::private::RawValue"] {
        analysis.symbols.insert(key.into(), symbols["x"].clone());
        analysis.nodes["n"]
            .definite_in
            .as_mut()
            .unwrap()
            .push(key.into());
    }
    analysis.symbols.extend(symbols);
    let text = serde_json::to_string(&analysis).unwrap();
    let candidates = available_vars_json(&text, "n", None).unwrap();
    assert_eq!(
        candidates,
        serde_json::to_string(&get_available_vars_at_node(&analysis, "n", None)).unwrap()
    );
    assert!(
        candidates.starts_with(r#"[{"path":{"root":"$serde_json::private::Number","fields":[]}"#)
    );
    assert!(candidates.contains(r#""root":"$serde_json::private::RawValue""#));
    for number in [
        "123456789012345678901234567890",
        "1.000000000000000000000000001",
        "1e+10000",
    ] {
        assert!(candidates.contains(number));
    }
    for arrays in [127, 128, 2048] {
        // A standalone TypeSpec has no GraphAnalysis wrapper. Its type-name
        // leaf is at depth arrays + 1, so 127 arrays exactly fill its budget.
        let expected = format!(
            r#"{}{{"type":"integer"}}{}"#,
            r#"{"type":"array","items":"#.repeat(arrays),
            "}".repeat(arrays)
        );
        let result = available_vars_json(&text, "n", Some(&expected));
        if arrays == 127 {
            assert_eq!(result.unwrap(), "[]");
        } else {
            assert!(result.is_err());
        }
    }
    let too_deep = format!(
        r#"{}, "extra":{}0{}}}"#,
        text.strip_suffix('}').unwrap(),
        "[".repeat(2048),
        "]".repeat(2048)
    );
    for invalid in [
        "{}",
        "null",
        "not JSON",
        &format!("{text} false"),
        &too_deep,
    ] {
        assert!(available_vars_json(invalid, "n", None).is_err());
        assert!(node_ports_json(invalid, "n").is_err());
    }
    for invalid in [
        "{}",
        "null",
        r#"{"type":"any"}"#,
        r#"{"type":"integer"} false"#,
    ] {
        assert!(available_vars_json(&text, "n", Some(invalid)).is_err());
    }
}

#[test]
fn invalid_callee_bodies_cannot_justify_caller_assignment_facts() {
    let main = graph(
        json!({"call":{"use":"workflow:bad","with":{"output":{"x":"x"}}},"independent":{"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}}}),
        json!(["start -> call", "call -> independent", "independent -> end"]),
    );
    let mut main = main;
    main["output"] = json!({"x":{"type":"integer"}});
    let bad = json!({"input":{},"output":{"x":{"type":"integer"}},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}});
    let out = compile_sources(
        "main",
        [("main".into(), source(main)), ("bad".into(), source(bad))],
    );
    error(&out, "assignment.exit");
    assert!(out.analysis[0].nodes["independent"].definite_in.is_none());
    assert!(out.analysis[1].nodes["n"].definite_in.is_some());
}

#[test]
fn exit_diagnostic_names_the_route_that_lacks_the_output() {
    let out = compile(
        json!({"input":{},"output":{"x":{"type":"string"}},"graph":{"nodes":{"n":{"run":"work","output":{"x":{"type":"string"}},"branches":{"yes":"write","no":{"description":"skip","output":[]}}}},"edges":["start -> n","n.yes -> end","n.no -> end"]}}),
    );
    error(&out, "assignment.exit");
    let exits: Vec<_> = out
        .diagnostics
        .iter()
        .filter(|d| d.code == "assignment.exit")
        .collect();
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0].edge_index, Some(2));
}

#[test]
fn inline_iteration_completion_respects_shadowing() {
    let mut doc = iteration(true, false);
    doc["input"]["item"] = json!({"type":"integer"});
    let out = compile(doc);
    ok(&out);
    let items = get_available_vars_at_node(&out.analysis[0], "each", None)
        .into_iter()
        .filter(|v| v.path.root == "item")
        .collect::<Vec<_>>();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].typespec.base(), "string");
}

#[test]
fn invalid_iteration_boundaries_do_not_inject_trusted_child_facts() {
    let mut doc = iteration(true, false);
    doc["graph"]["nodes"]["each"]["for_each"]["key"] = json!("key");
    let out = compile(doc);
    error(&out, "type.iteration_key");
    no_facts(&out);
}
