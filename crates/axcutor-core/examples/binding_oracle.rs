//! Generate expected text with the native public API. Other hosts must execute
//! their actual extension/WASM and reproduce these exact strings.
use axcutor_core::ast::TypeSpec;
use axcutor_core::*;
use serde_json::{Value, json};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    let mut cases: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(&args[1])?)?;
    // Exercise both sides of the shared source-depth boundary without maintaining
    // hundreds of brackets in the source fixtures.
    for format in ["json", "yaml"] {
        for arrays in [127, 128] {
            let text = format!(
                r#"{{"input":{{}},"output":{{}},"run":"work","extra":{}0{}}}"#,
                "[".repeat(arrays),
                "]".repeat(arrays)
            );
            cases.push(json!({"entry":"main","sources":{"main":{"format":format,"text":text}}}));
        }
        // Valid source-boundary types must survive compile -> serialize -> query,
        // including the deeper injected TypeSpec position on an iteration node.
        for arrays in [124, 125, 126] {
            let mut t = json!({"type":"integer"});
            for _ in 0..arrays {
                t = json!({"type":"array","items":t});
            }
            for iterated in [false, true] {
                let mut doc = json!({"input":{"x":t},"output":{},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}});
                if iterated {
                    doc["graph"]["variables"] =
                        json!({"result":{"type":"array","items":{"type":"integer"}}});
                    doc["graph"]["nodes"]["n"] = json!({"run":"work","for_each":{"source":"x","item":"item"},"output":{"result":{"type":"integer"}}});
                }
                cases.push(json!({"entry":"main","sources":{"main":{"format":format,"text":doc.to_string()}}}));
            }
        }
    }
    for marker in [
        "$serde_json::private::Number",
        "$serde_json::private::RawValue",
    ] {
        let bad =
            json!({"input":{"x":{"type":"number","enum":[{marker:"1"}]}},"output":{},"run":"work"});
        for format in ["json", "yaml"] {
            cases.push(
                json!({"entry":"main","sources":{"main":{"format":format,"text":bad.to_string()}}}),
            );
        }
    }
    let marker_case = cases.len();
    let mut marker_doc: Value = serde_json::from_str(
        r#"{"input":{"x":{"type":"integer","enum":[1]},"precise":{"type":"number","enum":[123456789012345678901234567890,1.000000000000000000000000001,1e10000]}},"output":{},"graph":{"nodes":{"n":{"run":"work"}},"edges":["start -> n","n -> end"]}}"#,
    )?;
    marker_doc["graph"]["variables"] = json!({"marker_map":{"type":"object","additionalProperties":{"type":"string"},"default":{"$serde_json::private::Number":"1","$serde_json::private::RawValue":"1"}}});
    cases.push(
        json!({"entry":"main","sources":{"main":{"format":"json","text":marker_doc.to_string()}}}),
    );
    let mut oracle = vec![];
    for (case_index, case) in cases.into_iter().enumerate() {
        let request_json = serde_json::to_string(&case)?;
        let result = compile_workflow(serde_json::from_str(&request_json)?);
        let result_json = serde_json::to_string(&result)?;
        let mut queries = vec![];
        let mut invalid_queries = vec![];
        if case_index == marker_case {
            let analysis = serde_json::to_value(&result.analysis[0])?;
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
                    invalid_queries.push(json!({"analysis_json":analysis.to_string(),"node_id":"n","expected_json":schema.to_string(),"check_ports":false}));
                    for injected in [false, true] {
                        let mut invalid = analysis.clone();
                        if injected {
                            invalid["nodes"]["n"]["injected"]["item"] = schema.clone();
                        } else {
                            invalid["symbols"]["x"] = schema.clone();
                        }
                        invalid_queries.push(json!({"analysis_json":invalid.to_string(),"node_id":"n","expected_json":null,"check_ports":true}));
                    }
                }
            }
        }
        for analysis in result.analysis {
            let analysis_json = serde_json::to_string(&analysis)?;
            let mut expected_types = vec![
                None,
                Some("{\"type\":\"number\"}".to_owned()),
                Some("{\"type\":[\"string\",\"null\"]}".to_owned()),
            ];
            if let Some(t) = analysis.symbols.get("x") {
                expected_types.push(Some(serde_json::to_string(t)?));
                // The standalone expected argument has no analysis wrapper.
                // Two added array levels fill its budget for the deepest input.
                expected_types.push(Some(serde_json::to_string(&TypeSpec::array(
                    TypeSpec::array(t.clone()),
                ))?));
            }
            for node in analysis.nodes.keys().map(String::as_str).chain(["missing"]) {
                for expected in &expected_types {
                    queries.push(json!({"analysis_json":analysis_json,"node_id":node,"expected_json":expected,
                        "vars_json":available_vars_json(&analysis_json,node,expected.as_deref()).map_err(std::io::Error::other)?,
                        "ports_json":node_ports_json(&analysis_json,node).map_err(std::io::Error::other)?}));
                }
            }
        }
        oracle
            .push(json!({"request_json":request_json,"result_json":result_json,"queries":queries,"invalid_queries":invalid_queries}));
    }
    std::fs::write(&args[2], serde_json::to_string(&oracle)?)?;
    Ok(())
}
