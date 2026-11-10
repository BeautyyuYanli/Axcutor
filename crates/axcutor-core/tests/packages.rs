//! Package grammar, visibility and immutable compile envelope boundaries.
mod support;
use axcutor_core::{ast::*, *};
use serde_json::{Value, json};
use support::*;

fn atomic() -> Value {
    json!({"input":{},"output":{},"run":{"executor":"module","content":"."}})
}

fn caller(target: &str) -> Value {
    graph(
        json!({"call":{"use":format!("workflow:{target}")}}),
        json!(["start -> call", "call -> end"]),
    )
}

#[test]
fn closed_module_syntax_and_anonymous_rejection() {
    for content in [".", ".charge", ".sub._charge", ".start.end.fail"] {
        let mut doc = atomic();
        doc["run"]["content"] = json!(content);
        let out = compile(doc);
        ok(&out);
        let bundle = serde_json::to_value(out.bundle.unwrap()).unwrap();
        assert_eq!(bundle["workflows"]["main"]["body"]["workflow_id"], "main");
        assert_eq!(
            bundle["workflows"]["main"]["body"]["run"]["executor"],
            "module"
        );
    }
    for content in [
        "",
        "charge",
        "billing.charge",
        "..charge",
        "...",
        ".charge.",
        ".a..b",
        ".class",
        ".a-b",
        ".中文",
        ". a",
    ] {
        let mut doc = atomic();
        doc["run"]["content"] = json!(content);
        only_error(&compile(doc), "schema.run", "/run/content");
    }
    let mut extra = atomic();
    extra["run"]["extra"] = json!(true);
    only_error(&compile(extra), "schema.unknown_field", "/run/extra");
    let out = compile_packages("__main__", [("__main__".into(), source(atomic()))]);
    only_error(&out, "module.anonymous_package", "/run");
}

#[test]
fn absolute_workflows_and_public_entry_are_required() {
    for target in [
        "__main__",
        ".charge",
        "..charge",
        "billing/charge",
        "billing.charge.yaml",
        "billing.__axcutor__",
        "_private",
        "billing.class",
    ] {
        error(&compile(caller(target)), "schema.workflow_ref");
    }
    for entry in [
        "",
        "old.yaml",
        "billing._private",
        "billing._private.public",
    ] {
        error(
            &compile_packages(entry, [(entry.into(), source(atomic()))]),
            "schema.entry",
        );
    }
    assert!(package_path("billing.start"));
    assert!(!package_path("_billing.charge"));
}

#[test]
fn visibility_checks_every_private_segment_and_component_boundaries() {
    let target = "billing._internal.tools._secret.public";
    for (owner, visible) in [
        ("__main__", false),
        ("reporting.daily", false),
        ("billing", false),
        ("billing.checkout", false),
        ("billing._internal", false),
        ("billing._internal.tools_extra", false),
        ("billing._internal.tools", true),
        ("billing._internal.tools.client", true),
    ] {
        assert_eq!(workflow_visible(owner, target), visible);
        // A private caller is reached through a public entry, not exposed as entry.
        let out = compile_packages(
            "billing.entry",
            [
                ("billing.entry".into(), source(caller(owner))),
                (owner.into(), source(caller(target))),
                (target.into(), source(atomic())),
            ],
        );
        if visible {
            ok(&out);
        } else {
            assert!(!workflow_visible(owner, target));
            assert!(out.bundle.is_none());
        }
    }
    let out = compile_packages(
        "billing.caller",
        [
            ("billing.caller".into(), source(caller("billing._helper"))),
            ("billing._helper".into(), source(atomic())),
        ],
    );
    ok(&out);
    let out = compile_packages(
        "__main__",
        [
            ("__main__".into(), source(caller("billing._helper"))),
            ("billing._helper".into(), source(atomic())),
        ],
    );
    only_error(&out, "visibility.private_workflow", "/graph/nodes/call/use");
}

#[test]
fn signatures_resolve_cycles_in_either_snapshot_order() {
    for reverse in [false, true] {
        let mut packages = vec![
            ("billing.a".into(), source(caller("billing.b"))),
            ("billing.b".into(), source(caller("billing.a"))),
        ];
        if reverse {
            packages.reverse();
        }
        ok(&compile_packages("billing.a", packages));
    }
}

#[test]
fn duplicate_mounts_and_old_envelopes_never_silently_overwrite() {
    for text in [
        r#"{"entry":"main","sources":{}}"#,
        r#"{"entry":"main","packages":{}}"#,
        r#"{"entry":"main","mounts":{"main":{},"main":{}}}"#,
        r#"{"entry":"main","mounts":{"main":{"packages":{"main":{"format":"yaml","text":""},"main":{"format":"yaml","text":""}}}}}"#,
        r#"{"entry":"main","packages":{"main":{"format":"yaml","text":""},"main":{"format":"yaml","text":""}}}"#,
    ] {
        assert!(compile_workflow_json(text).is_err());
    }
    error(
        &compile_packages(
            "main",
            [
                ("main".into(), source(atomic())),
                ("bad/path".into(), source(atomic())),
            ],
        ),
        "schema.package_path",
    );
}
