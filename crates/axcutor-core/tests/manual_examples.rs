//! Full, self-contained examples are compiled verbatim. Fragments, placeholders
//! and workflows requiring unavailable external resources are not complete inputs.
mod support;
use std::path::Path;
use support::*;

fn block(file: &str, index: usize) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../manual/docs")
        .join(file);
    let text = std::fs::read_to_string(path).unwrap();
    text.split("```yaml\n")
        .skip(1)
        .nth(index - 1)
        .unwrap()
        .split("```")
        .next()
        .unwrap()
        .to_owned()
}

#[test]
fn seventeen_complete_guide_and_cookbook_examples_compile_unchanged() {
    for (path, index) in [
        ("guide/01-foundations/01-basics.md", 3),
        ("guide/01-foundations/01-basics.md", 4),
        ("guide/01-foundations/01-basics.md", 5),
        ("guide/02-control-flow/01-branches.md", 8),
        ("guide/02-control-flow/02-cycles-and-back-edges.md", 6),
        ("guide/02-control-flow/03-builtin-operators.md", 6),
        ("guide/03-concurrency/01-for-each.md", 6),
        ("guide/04-state-and-resilience/01-frames-and-scoping.md", 7),
        (
            "guide/04-state-and-resilience/02-guardrails-and-timeouts.md",
            4,
        ),
        ("guide/04-state-and-resilience/03-error-handling.md", 4),
        (
            "cookbook/01-loops-and-queues/01-generate-verify-refine-loop.md",
            1,
        ),
        (
            "cookbook/01-loops-and-queues/02-short-circuit-fallback-chain.md",
            1,
        ),
        (
            "cookbook/02-collection-and-concurrency/01-stateful-sequential-fold.md",
            1,
        ),
        (
            "cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap.md",
            1,
        ),
        (
            "cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop.md",
            1,
        ),
        (
            "cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction.md",
            1,
        ),
        (
            "cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue.md",
            1,
        ),
    ] {
        let text = block(path, index);
        let out = yaml(&text);
        assert!(
            out.bundle.is_some(),
            "{path} block {index}: {:#?}",
            out.diagnostics
        );
        ok(&out);
    }
}

#[test]
fn documented_host_collision_counterexample_remains_invalid() {
    let out = yaml(&block(
        "guide/04-state-and-resilience/01-frames-and-scoping.md",
        4,
    ));
    error(&out, "symbol.host_collision");
    no_facts(&out);
}
