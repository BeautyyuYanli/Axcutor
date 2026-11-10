#![allow(dead_code)]
use axcutor_core::{ast::*, *};
use serde_json::{Value, json};

pub fn compile(v: Value) -> CompileOutput {
    compile_packages(
        "main",
        [(
            "main".into(),
            SourceDocument {
                format: SourceFormat::Json,
                text: v.to_string(),
            },
        )],
    )
}
pub fn yaml(text: &str) -> CompileOutput {
    compile_packages(
        "main",
        [(
            "main".into(),
            SourceDocument {
                format: SourceFormat::Yaml,
                text: text.into(),
            },
        )],
    )
}
pub fn json_text(text: &str) -> CompileOutput {
    compile_packages(
        "main",
        [(
            "main".into(),
            SourceDocument {
                format: SourceFormat::Json,
                text: text.into(),
            },
        )],
    )
}
pub fn compile_packages(
    entry: &str,
    packages: impl IntoIterator<Item = (String, SourceDocument)>,
) -> CompileOutput {
    compile_workflow(CompileRequest {
        entry: entry.into(),
        mounts: mounts(packages),
    })
}
pub fn mounts(packages: impl IntoIterator<Item = (String, SourceDocument)>) -> Ordered<MountUnit> {
    let mut mounts: Ordered<MountUnit> = Ordered::new();
    for (id, source) in packages {
        mounts
            .entry(id.split('.').next().unwrap().into())
            .or_default()
            .packages
            .insert(id, source);
    }
    mounts
}
pub fn source(v: Value) -> SourceDocument {
    SourceDocument {
        format: SourceFormat::Json,
        text: v.to_string(),
    }
}
pub fn ok(out: &CompileOutput) {
    assert!(out.diagnostics.is_empty(), "{:#?}", out.diagnostics);
    assert!(out.bundle.is_some());
}
pub fn error(out: &CompileOutput, code: &str) {
    assert!(out.bundle.is_none(), "invalid input yielded a bundle");
    assert!(
        out.diagnostics.iter().any(|d| d.code == code),
        "expected {code}: {:#?}",
        out.diagnostics
    );
}
pub fn only_error(out: &CompileOutput, code: &str, path: &str) {
    error(out, code);
    assert_eq!(out.diagnostics.len(), 1, "{:#?}", out.diagnostics);
    assert_eq!(out.diagnostics[0].param_path, path);
}
pub fn no_facts(out: &CompileOutput) {
    for g in &out.analysis {
        for n in g.nodes.values() {
            assert!(n.definite_in.is_none());
        }
    }
}
pub fn graph(nodes: Value, edges: Value) -> Value {
    json!({"input":{},"output":{},"graph":{"nodes":nodes,"edges":edges}})
}
pub fn assign(arg: Value, target: &str) -> Value {
    json!({"use":"builtin:assign","with":{"input":{"source":arg},"output":{"value":target}}})
}
pub fn builtin(
    op: &str,
    args: Value,
    output: Option<Value>,
    input: Value,
    vars: Value,
    ports: &[&str],
) -> Value {
    let mut node = json!({"use":format!("builtin:{op}"),"with":{"input":args}});
    if let Some(output) = output {
        node["with"]["output"] = output;
    }
    let mut edges = vec![json!("start -> step")];
    if ports.is_empty() {
        edges.push(json!("step -> end"));
    } else {
        edges.extend(ports.iter().map(|p| json!(format!("step.{p} -> end"))));
    }
    json!({"input":input,"output":{},"graph":{"variables":vars,"nodes":{"step":node},"edges":edges}})
}
pub fn typespec(v: Value) -> TypeSpec {
    serde_json::from_value(v).unwrap()
}
