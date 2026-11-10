//! Root ownership, static resource declarations and per-root executor policy.
mod support;
use axcutor_core::{ast::*, *};
use serde_json::{Value, json};
use support::*;

fn atomic(executor: &str) -> Value {
    let mut run = json!({"executor":executor});
    if matches!(executor, "builtin" | "module") {
        run["content"] = json!(if executor == "module" { "." } else { "pass" });
    }
    json!({"input":{},"output":{},"run":run})
}

fn request(doc: Value) -> CompileRequest {
    CompileRequest {
        entry: "app".into(),
        mounts: mounts([("app".into(), source(doc))]),
    }
}

#[test]
fn roots_own_all_documents_including_unreachable_ones() {
    for root in [
        "",
        "a.b",
        "_private",
        "__axcutor__",
        "__ax_resources__",
        "class",
        "bad/name",
    ] {
        let mut r = request(atomic("signal"));
        r.mounts.insert(root.into(), MountUnit::default());
        let out = compile_workflow(r);
        only_error(&out, "schema.mount_root", "");
        assert_eq!(out.diagnostics[0].workflow_id, root);
    }
    for (root, id, resource) in [
        ("app", "std._private", false),
        ("app", "std._private", true),
        ("app", "__main__", false),
        ("app", "app.__ax_resources__", true),
        ("app", "app.__axcutor__", false),
        ("__main__", "app", false),
        ("__main__", "__main__", true),
    ] {
        let mut r = request(atomic("signal"));
        let unit = r.mounts.entry(root.into()).or_default();
        let sources = if resource {
            &mut unit.resources
        } else {
            &mut unit.packages
        };
        sources.insert(id.into(), source(json!({})));
        let out = compile_workflow(r);
        only_error(&out, "schema.package_path", "");
        assert_eq!(out.diagnostics[0].workflow_id, id);
        assert_eq!(
            out.diagnostics[0].message,
            format!("package path must belong to mount root {root}")
        );
    }
}

#[test]
fn resource_headers_are_closed_and_checked_even_without_workflow_references() {
    for (header, code, path) in [
        (json!({}), "schema.resources", "/resources"),
        (json!({"resources":{}}), "schema.resources", "/resources"),
        (json!({"resources":[]}), "schema.resources", "/resources"),
        (
            json!({"resources":{"Tx":null},"input":{}}),
            "schema.unknown_field",
            "/input",
        ),
        (
            json!({"resources":{"_Tx":null}}),
            "schema.identifier",
            "/resources/_Tx",
        ),
        (
            json!({"resources":{"class":null}}),
            "schema.identifier",
            "/resources/class",
        ),
        (
            json!({"resources":{"Tx":[]}}),
            "schema.object",
            "/resources/Tx",
        ),
        (
            json!({"resources":{"Tx":{"other":true}}}),
            "schema.unknown_field",
            "/resources/Tx/other",
        ),
        (
            json!({"resources":{"Tx":{"description":null}}}),
            "schema.string",
            "/resources/Tx/description",
        ),
    ] {
        let mut r = request(atomic("signal"));
        r.mounts.insert(
            "infra".into(),
            MountUnit {
                resources: [("infra".into(), source(header))].into_iter().collect(),
                ..MountUnit::default()
            },
        );
        let out = compile_workflow(r);
        only_error(&out, code, path);
        assert_eq!(out.diagnostics[0].target_kind, "resource");
    }
    let mut r = request(atomic("signal"));
    r.mounts["app"].resources.insert(
        "app".into(),
        SourceDocument {
            format: SourceFormat::Yaml,
            text: "resources:\n  Tx:\n  Tx:\n".into(),
        },
    );
    let out = compile_workflow(r);
    error(&out, "parse.source");
    assert_eq!(out.diagnostics[0].target_kind, "resource");
    assert!(out.diagnostics[0].source_location.is_some());
}

#[test]
fn handles_require_declarations_recursively_and_keep_private_visibility() {
    let h = json!({"type":"handle","kind":"infra._private:Tx"});
    let nested = json!({"type":"array","items":{"type":"object","additionalProperties":{"type":"object","properties":{"tx":h}}}});
    let mut doc = graph(
        json!({"scope":{"graph":{"nodes":{"work":{"run":{"executor":"signal"}}},"edges":["start -> work","work -> end"]},"output":{"tx":h}}}),
        json!(["start -> scope", "scope -> end"]),
    );
    doc["input"] = json!({"values":nested});
    doc["output"] = json!({"tx":h});
    doc["graph"]["variables"] = json!({"cached":h});
    let mut r = request(doc);
    let out = compile_workflow(r.clone());
    for path in [
        "/input/values/items/additionalProperties/properties/tx/kind",
        "/output/tx/kind",
        "/graph/variables/cached/kind",
        "/graph/nodes/scope/output/tx/kind",
    ] {
        assert!(
            out.diagnostics
                .iter()
                .any(|d| d.code == "type.unknown_resource" && d.param_path == path),
            "{:#?}",
            out.diagnostics
        );
    }
    r.mounts.insert("infra".into(), MountUnit {
        resources: [("infra._private".into(), source(json!({"resources":{"Tx":{"description":"transaction"},"Conn":null,"Other":{}}})))].into_iter().collect(),
        ..MountUnit::default()
    });
    let out = compile_workflow(r);
    error(&out, "visibility.private_resource");
    assert!(
        !out.diagnostics
            .iter()
            .any(|d| d.code == "type.unknown_resource")
    );
    // TypeSpec remains a context-free lexical parser.
    assert!(TypeSpec::parse(&h).is_ok());
}

#[test]
fn policies_apply_to_root_runs_inline_graphs_and_extensions() {
    for executor in ["builtin", "module", "signal", "remote", "class"] {
        let mut r = request(atomic(executor));
        ok(&compile_workflow(r.clone()));
        r.mounts["app"].allowed_executors = Some(vec![executor.into()]);
        ok(&compile_workflow(r.clone()));
        r.mounts["app"].allowed_executors = Some(vec![]);
        let out = compile_workflow(r);
        only_error(&out, "schema.executor_not_allowed", "/run/executor");
        assert_eq!(
            out.diagnostics[0].message,
            format!("executor {executor} is not allowed in mount app")
        );
    }
    let mut r = request(graph(
        json!({"scope":{"graph":{"nodes":{"work":{"run":{"executor":"remote"}}},"edges":["start -> work","work -> end"]}}}),
        json!(["start -> scope", "scope -> end"]),
    ));
    r.mounts["app"].allowed_executors = Some(vec!["signal".into()]);
    only_error(
        &compile_workflow(r),
        "schema.executor_not_allowed",
        "/graph/nodes/scope/graph/nodes/work/run/executor",
    );
    let mut r = request(atomic("signal"));
    r.mounts.insert(
        "unused".into(),
        MountUnit {
            allowed_executors: Some(vec!["bad.name".into()]),
            ..MountUnit::default()
        },
    );
    only_error(
        &compile_workflow(r),
        "schema.executor",
        "/allowed_executors/0",
    );
}

#[test]
fn cross_root_calls_use_callee_policy_and_the_complete_checking_pipeline() {
    let doc = json!({"input":{"x":{"type":"integer"}},"output":{"y":{"type":"integer"}},"graph":{"nodes":{"call":{"use":"workflow:std.step","with":{"input":{"x":"x"},"output":{"y":"y"}}}},"edges":["start -> call","call -> end"]}});
    let mut r = request(doc);
    r.mounts["app"].allowed_executors = Some(vec![]);
    r.mounts["app"]
        .packages
        .insert("app.unused".into(), source(atomic("builtin")));
    let mut step = atomic("module");
    step["input"] = json!({"x":{"type":"integer"}});
    step["output"] = json!({"y":{"type":"integer"}});
    r.mounts.insert(
        "std".into(),
        MountUnit {
            packages: [("std.step".into(), source(step))].into_iter().collect(),
            allowed_executors: Some(vec!["module".into()]),
            ..MountUnit::default()
        },
    );
    let out = compile_workflow(r.clone());
    ok(&out);
    assert_eq!(out.bundle.as_ref().unwrap().workflows.len(), 2);
    assert_eq!(
        out.analysis[0].nodes["call"].definite_in,
        Some(vec!["x".into()])
    );
    let RootBody::Run { input_schema, .. } = &out.bundle.unwrap().workflows["std.step"].body else {
        panic!()
    };
    assert!(input_schema.contains_key("x"));
    r.mounts["std"].allowed_executors = Some(vec!["signal".into()]);
    let out = compile_workflow(r);
    only_error(&out, "schema.executor_not_allowed", "/run/executor");
    assert_eq!(out.diagnostics[0].workflow_id, "std.step");
    no_facts(&out);
}
