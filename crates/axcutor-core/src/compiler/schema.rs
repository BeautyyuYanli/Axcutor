//! Pass 1: closed structural grammar and local desugaring. Invalid siblings do
//! not stop traversal. Partial ASTs remain private and never become a bundle.
use super::*;
use num_bigint::BigInt;
use serde_json::{Map, Number};

type Ds = Vec<Diagnostic>;

fn object<'a>(v: &'a Value, a: &Address, ds: &mut Ds) -> Option<&'a Map<String, Value>> {
    if let Some(m) = v.as_object() {
        Some(m)
    } else {
        a.error(ds, "node", "schema.object", "expected a mapping object");
        None
    }
}
fn closed(m: &Map<String, Value>, allowed: &[&str], a: &Address, ds: &mut Ds) {
    for k in m.keys().filter(|k| !allowed.contains(&k.as_str())) {
        a.at(k).error(
            ds,
            "node",
            "schema.unknown_field",
            format!("field {k} is not allowed here"),
        );
    }
}
fn name(s: &str, a: &Address, ds: &mut Ds) -> bool {
    let valid = control_id(s);
    if !valid {
        a.error(
            ds,
            "node",
            "schema.identifier",
            format!("invalid identifier {s}"),
        );
    }
    valid
}
fn string(v: Option<&Value>, a: &Address, ds: &mut Ds) -> Option<String> {
    match v {
        Some(Value::String(s)) => Some(s.clone()),
        _ => {
            a.error(ds, "node", "schema.string", "expected a string");
            None
        }
    }
}
fn strings(v: Option<&Value>, a: &Address, ds: &mut Ds) -> Vec<String> {
    let Some(Value::Array(items)) = v else {
        a.error(ds, "node", "schema.list", "expected a list of identifiers");
        return vec![];
    };
    let mut result = vec![];
    for (i, v) in items.iter().enumerate() {
        let at = a.at(&i.to_string());
        if let Some(s) = string(Some(v), &at, ds) {
            if !name(&s, &at, ds) {
                continue;
            }
            if result.contains(&s) {
                at.error(ds, "node", "schema.duplicate", "duplicate list entry");
            } else {
                result.push(s);
            }
        }
    }
    result
}
fn integer(
    v: Option<&Value>,
    minimum: u8,
    default: Option<u64>,
    a: &Address,
    ds: &mut Ds,
) -> Option<Number> {
    if v.is_none() {
        if let Some(d) = default {
            return Some(d.into());
        }
    }
    if let Some(Value::Number(n)) = v {
        let s = n.as_str();
        if !s.contains(['.', 'e', 'E'])
            && s.parse::<BigInt>()
                .is_ok_and(|n| n >= BigInt::from(minimum))
        {
            return Some(n.clone());
        }
    }
    a.error(
        ds,
        "node",
        "schema.integer",
        format!("expected an integer >= {minimum}"),
    );
    None
}

fn types(v: Option<&Value>, required: bool, a: &Address, ds: &mut Ds) -> Symbols {
    if v.is_none() && !required {
        return Symbols::new();
    }
    let Some(m) = v.and_then(|v| object(v, a, ds)) else {
        if v.is_none() {
            a.error(
                ds,
                "node",
                "schema.required",
                "required type mapping is missing",
            );
        }
        return Symbols::new();
    };
    let mut result = Symbols::new();
    for (n, t) in m {
        let at = a.at(n);
        let valid = name(n, &at, ds);
        match TypeSpec::parse(t) {
            Ok(t) if valid => {
                result.insert(n.clone(), t);
            }
            Ok(_) => (),
            Err(e) => at.error(ds, "variable", "type.schema", e),
        }
    }
    result
}

fn branches(
    v: Option<&Value>,
    output: &Symbols,
    a: &Address,
    ds: &mut Ds,
) -> Ordered<BranchContract> {
    let mut out = Ordered::new();
    let Some(v) = v else {
        return out;
    };
    let Some(m) = object(v, a, ds) else {
        return out;
    };
    for (b, v) in m {
        let at = a.at(b);
        let valid = name(b, &at, ds);
        let spec = match v {
            Value::String(d) => Some(BranchContract {
                description: d.clone(),
                output: output.keys().cloned().collect(),
            }),
            Value::Object(m) => {
                closed(m, &["description", "output"], &at, ds);
                let description = string(m.get("description"), &at.at("description"), ds);
                let subset = strings(m.get("output"), &at.at("output"), ds);
                for s in &subset {
                    if !output.contains_key(s) {
                        at.at("output").error(
                            ds,
                            "port",
                            "schema.branch_output",
                            format!("branch output {s} is undeclared"),
                        );
                    }
                }
                description.map(|description| BranchContract {
                    description,
                    output: subset,
                })
            }
            _ => {
                at.error(
                    ds,
                    "port",
                    "schema.branch",
                    "branch must be a description or contract object",
                );
                None
            }
        };
        if valid {
            if let Some(s) = spec {
                out.insert(b.clone(), s);
            }
        }
    }
    out
}

pub(super) fn dependencies(raw: &Value, pending: &mut VecDeque<String>) {
    fn graph(v: &Value, pending: &mut VecDeque<String>) {
        if let Some(nodes) = v.get("nodes").and_then(Value::as_object) {
            for node in nodes.values() {
                if let Some(target) = node
                    .get("use")
                    .and_then(Value::as_str)
                    .and_then(|s| s.strip_prefix("workflow:"))
                {
                    pending.push_back(target.into());
                }
                if let Some(g) = node.get("graph") {
                    graph(g, pending);
                }
            }
        }
    }
    if let Some(g) = raw.get("graph") {
        graph(g, pending);
    }
}

pub(super) fn root(
    raw: &Value,
    a: &Address,
    ds: &mut Ds,
) -> (Option<NormalizedRoot>, Option<Signature>) {
    let Some(m) = object(raw, a, ds) else {
        return (None, None);
    };
    let before = ds.len();
    let input = types(m.get("input"), true, &a.at("input"), ds);
    let output = types(m.get("output"), true, &a.at("output"), ds);
    let branches = branches(m.get("branches"), &output, &a.at("branches"), ds);
    let signature = (ds.len() == before).then(|| Signature {
        input: input.clone(),
        output: output.clone(),
        branches: branches.clone(),
    });
    let count = ["run", "graph"]
        .iter()
        .filter(|k| m.contains_key(**k))
        .count();
    if count != 1 {
        a.error(
            ds,
            "node",
            "schema.body",
            "root requires exactly one of run or graph",
        );
    }
    let body = if let Some(g) = m.get("graph") {
        closed(m, &["input", "output", "branches", "graph"], a, ds);
        graph(g, &a.graph(false), ds).map(|graph| RootBody::Graph { graph })
    } else if m.contains_key("run") {
        closed(
            m,
            &["input", "output", "branches", "run", "timeout_ms", "retry"],
            a,
            ds,
        );
        let run = run(m.get("run"), &a.at("run"), ds);
        let timeout_ms = m
            .get("timeout_ms")
            .and_then(|v| integer(Some(v), 1, None, &a.at("timeout_ms"), ds));
        let retry = retry(m.get("retry"), true, &a.at("retry"), ds);
        run.map(|run| RootBody::Run {
            input_schema: input.clone(),
            run,
            timeout_ms,
            retry,
        })
    } else {
        closed(m, &["input", "output", "branches"], a, ds);
        None
    };
    (
        body.map(|body| NormalizedRoot {
            input,
            output,
            branches,
            body,
        }),
        signature,
    )
}

fn run(v: Option<&Value>, a: &Address, ds: &mut Ds) -> Option<RunSpec> {
    let m = object(v?, a, ds)?;
    let executor = string(m.get("executor"), &a.at("executor"), ds)?;
    if !identifier(&executor) {
        a.at("executor").error(
            ds,
            "node",
            "schema.executor",
            "executor must be an Identifier",
        );
        return None;
    }
    if executor != "builtin" {
        return Some(RunSpec::Extension {
            executor,
            config: m
                .iter()
                .filter(|(key, _)| key.as_str() != "executor")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect(),
        });
    }
    closed(m, &["executor", "content"], a, ds);
    let content = string(m.get("content"), &a.at("content"), ds)?;
    if content.trim().is_empty() {
        a.at("content")
            .error(ds, "node", "schema.run", "content must be nonempty");
        return None;
    }
    Some(RunSpec::Builtin { content })
}

fn graph(raw: &Value, a: &Address, ds: &mut Ds) -> Option<NormalizedGraph> {
    let m = object(raw, a, ds)?;
    closed(m, &["variables", "nodes", "edges", "max_steps"], a, ds);
    let max_steps = integer(m.get("max_steps"), 1, Some(1000), &a.at("max_steps"), ds)
        .unwrap_or_else(|| 1000.into());
    let mut variables = Ordered::new();
    if let Some(v) = m.get("variables") {
        if let Some(m) = object(v, &a.at("variables"), ds) {
            for (n, v) in m {
                let at = a.at("variables").at(n);
                let valid = name(n, &at, ds);
                if let Some(fields) = object(v, &at, ds) {
                    let mut t = fields.clone();
                    let initial = t
                        .remove("default")
                        .map_or(InitialValue::Unbound, InitialValue::Default);
                    match TypeSpec::parse(&Value::Object(t)) {
                        Ok(typespec) if valid => {
                            variables.insert(n.clone(), VarDecl { typespec, initial });
                        }
                        Ok(_) => (),
                        Err(e) => at.error(ds, "variable", "type.schema", e),
                    }
                }
            }
        }
    }
    let mut nodes = Ordered::new();
    match m.get("nodes").and_then(Value::as_object) {
        Some(ns) if !ns.is_empty() => {
            for (id, raw) in ns {
                let at = a.node(id);
                let valid = name(id, &at, ds);
                let node = node(raw, &at, ds);
                if valid {
                    if let Some(node) = node {
                        nodes.insert(id.clone(), node);
                    }
                }
            }
        }
        _ => a.at("nodes").error(
            ds,
            "node",
            "schema.nodes",
            "nodes must be a nonempty mapping",
        ),
    }
    let mut routes = Ordered::new();
    match m.get("edges").and_then(Value::as_array) {
        Some(es) if !es.is_empty() => {
            for (i, v) in es.iter().enumerate() {
                if let Some(raw) = v.as_str() {
                    match edge(raw, i) {
                        Ok(route) => {
                            let key = route.source.key();
                            if routes.contains_key(&key) {
                                a.edge_error(
                                    ds,
                                    i,
                                    "topology.duplicate_source",
                                    format!("source {key} has more than one route"),
                                );
                            } else {
                                routes.insert(key, route);
                            }
                        }
                        Err(e) => a.edge_error(ds, i, "schema.edge", e),
                    }
                } else {
                    a.edge_error(ds, i, "schema.edge", "edge must be a string");
                }
            }
        }
        _ => a
            .at("edges")
            .error(ds, "edge", "schema.edges", "edges must be a nonempty list"),
    }
    Some(NormalizedGraph {
        variables,
        nodes,
        routes,
        max_steps,
    })
}

fn node(raw: &Value, a: &Address, ds: &mut Ds) -> Option<NormalizedNode> {
    let m = object(raw, a, ds)?;
    let count = ["run", "graph", "use"]
        .iter()
        .filter(|k| m.contains_key(**k))
        .count();
    if count != 1 {
        a.error(
            ds,
            "node",
            "schema.body",
            "node requires exactly one of run, graph or use",
        );
    }
    let body = if m.contains_key("use") {
        let target = string(m.get("use"), &a.at("use"), ds);
        let bindings = bindings(m.get("with"), &a.at("with"), ds);
        if let Some(id) = target
            .as_ref()
            .and_then(|s| s.strip_prefix("workflow:"))
            .filter(|s| !s.is_empty())
        {
            closed(
                m,
                &["use", "with", "for_each", "timeout_ms", "retry", "catch"],
                a,
                ds,
            );
            Some(NodeBody::Workflow {
                workflow_id: id.into(),
                bindings,
            })
        } else if let Some(op) = target.as_ref().and_then(|s| s.strip_prefix("builtin:")) {
            closed(m, &["use", "with"], a, ds);
            match serde_json::from_value::<Builtin>(Value::String(op.into())) {
                Ok(op) => Some(NodeBody::Builtin { op, bindings }),
                Err(_) => {
                    a.at("use").error(
                        ds,
                        "node",
                        "builtin.unknown",
                        format!("unknown builtin {op}"),
                    );
                    None
                }
            }
        } else {
            a.at("use").error(
                ds,
                "node",
                "schema.use",
                "use must name a nonempty workflow: resource or builtin operator",
            );
            None
        }
    } else {
        closed(
            m,
            &[
                "run",
                "graph",
                "input",
                "output",
                "branches",
                "for_each",
                "timeout_ms",
                "retry",
                "catch",
            ],
            a,
            ds,
        );
        let input = m
            .get("input")
            .map(|v| strings(Some(v), &a.at("input"), ds))
            .unwrap_or_default();
        let output = types(m.get("output"), false, &a.at("output"), ds);
        let branches = branches(m.get("branches"), &output, &a.at("branches"), ds);
        if let Some(g) = m.get("graph") {
            graph(g, &a.graph(true), ds).map(|graph| NodeBody::Graph {
                input,
                output,
                branches,
                graph: Box::new(graph),
            })
        } else {
            run(m.get("run"), &a.at("run"), ds).map(|run| NodeBody::Run {
                input,
                input_schema: Symbols::new(),
                output,
                branches,
                run,
            })
        }
    };
    // Modifiers are still checked if the body failed, so unrelated errors survive.
    let atomic = m.contains_key("run");
    let for_each = for_each(m.get("for_each"), &a.at("for_each"), ds);
    let timeout_ms = m
        .get("timeout_ms")
        .and_then(|v| integer(Some(v), 1, None, &a.at("timeout_ms"), ds));
    let retry = retry(m.get("retry"), atomic, &a.at("retry"), ds);
    let catch = catches(m.get("catch"), atomic, &a.at("catch"), ds);
    let body = body?;
    let node = NormalizedNode {
        body,
        modifiers: Modifiers {
            for_each,
            timeout_ms,
            retry,
            catch,
        },
    };
    if node.modifiers.for_each.is_some() && node.out_vars().is_empty() {
        a.at("for_each").error(
            ds,
            "node",
            "schema.iteration_output",
            "for_each must bind at least one output",
        );
    }
    Some(node)
}

fn bindings(v: Option<&Value>, a: &Address, ds: &mut Ds) -> Bindings {
    let mut b = Bindings::default();
    let Some(v) = v else {
        return b;
    };
    let Some(m) = object(v, a, ds) else {
        return b;
    };
    closed(m, &["input", "output"], a, ds);
    if let Some(v) = m.get("input") {
        if let Some(m) = object(v, &a.at("input"), ds) {
            for (k, v) in m {
                let at = a.at("input").at(k);
                name(k, &at, ds);
                let arg = match v {
                    Value::String(s) => FieldPath::parse(s).map(|path| ArgValue::Path { path }),
                    Value::Object(m) if m.len() == 1 && m.contains_key("value") => {
                        Some(ArgValue::Const {
                            value: m["value"].clone(),
                        })
                    }
                    Value::Object(_) | Value::Array(_) => None,
                    _ => Some(ArgValue::Const { value: v.clone() }),
                };
                if let Some(arg) = arg {
                    b.input.insert(k.clone(), arg);
                } else {
                    at.error(
                        ds,
                        "node",
                        "schema.argument",
                        "expected a field path, scalar literal or {value: literal}",
                    );
                }
            }
        }
    }
    b.output_present = m.contains_key("output");
    if let Some(v) = m.get("output") {
        if let Some(m) = object(v, &a.at("output"), ds) {
            for (k, v) in m {
                let at = a.at("output").at(k);
                name(k, &at, ds);
                if let Some(n) = string(Some(v), &at, ds) {
                    if name(&n, &at, ds) {
                        if b.output.values().any(|v| v == &n) {
                            at.error(
                                ds,
                                "variable",
                                "schema.output_injective",
                                "two outputs cannot bind the same variable",
                            );
                        }
                        b.output.insert(k.clone(), n);
                    }
                }
            }
        }
    }
    b
}

fn for_each(v: Option<&Value>, a: &Address, ds: &mut Ds) -> Option<ForEach> {
    let m = object(v?, a, ds)?;
    closed(m, &["source", "item", "key", "index", "concurrency"], a, ds);
    let source = string(m.get("source"), &a.at("source"), ds).and_then(|s| {
        let p = FieldPath::parse(&s);
        if p.is_none() {
            a.at("source")
                .error(ds, "node", "schema.path", "invalid source field path");
        }
        p
    });
    let item = string(m.get("item"), &a.at("item"), ds);
    let key = m.get("key").and_then(|v| string(Some(v), &a.at("key"), ds));
    let index = m
        .get("index")
        .and_then(|v| string(Some(v), &a.at("index"), ds));
    let mut seen = vec![];
    for (label, n) in [
        ("item", item.as_ref()),
        ("key", key.as_ref()),
        ("index", index.as_ref()),
    ] {
        if let Some(n) = n {
            name(n, &a.at(label), ds);
            if seen.contains(&n) {
                a.at(label).error(
                    ds,
                    "variable",
                    "symbol.injected_collision",
                    "iteration names must be distinct",
                );
            }
            seen.push(n);
        }
    }
    let concurrency = integer(m.get("concurrency"), 1, Some(1), &a.at("concurrency"), ds);
    Some(ForEach {
        source: source?,
        item: item?,
        key,
        index,
        concurrency: concurrency?,
    })
}

fn selectors(v: &Value, atomic: bool, a: &Address, ds: &mut Ds) -> Vec<String> {
    let Some(values) = v.as_array().filter(|a| !a.is_empty()) else {
        a.error(
            ds,
            "port",
            "schema.selectors",
            "selectors must be a nonempty list",
        );
        return vec![];
    };
    let mut result = vec![];
    for v in values {
        if let Some(s) = v.as_str().filter(|s| {
            matches!(*s, "execution" | "timeout" | "contract") || (!atomic && *s == "max_steps")
        }) {
            if result.iter().any(|r| r == s) {
                a.error(ds, "port", "schema.selectors", "duplicate selector");
            } else {
                result.push(s.into());
            }
        } else {
            a.error(
                ds,
                "port",
                "schema.selectors",
                "invalid recoverable selector for this node",
            );
        }
    }
    result
}
fn retry(v: Option<&Value>, atomic: bool, a: &Address, ds: &mut Ds) -> Option<Retry> {
    let m = object(v?, a, ds)?;
    closed(m, &["max_attempts", "backoff_ms", "on"], a, ds);
    let max_attempts = integer(m.get("max_attempts"), 2, None, &a.at("max_attempts"), ds);
    let backoff_ms = integer(m.get("backoff_ms"), 0, Some(0), &a.at("backoff_ms"), ds);
    let on = m
        .get("on")
        .map(|v| selectors(v, atomic, &a.at("on"), ds))
        .unwrap_or_else(|| vec!["execution".into(), "timeout".into()]);
    Some(Retry {
        max_attempts: max_attempts?,
        backoff_ms: backoff_ms?,
        on,
    })
}
fn catches(v: Option<&Value>, atomic: bool, a: &Address, ds: &mut Ds) -> Ordered<Vec<String>> {
    let mut result = Ordered::new();
    let Some(v) = v else {
        return result;
    };
    let Some(m) = object(v, a, ds) else {
        return result;
    };
    if m.is_empty() {
        a.error(ds, "port", "schema.catch", "catch must be nonempty");
    }
    let mut wildcard = None;
    let mut used = vec![];
    for (b, v) in m {
        let at = a.at(b);
        name(b, &at, ds);
        if v.as_str() == Some("*") {
            if wildcard.is_some() {
                at.error(
                    ds,
                    "port",
                    "schema.catch",
                    "only one wildcard catch is allowed",
                );
            }
            wildcard = Some(b.clone());
            result.insert(b.clone(), vec![]);
        } else {
            let ss = selectors(v, atomic, &at, ds);
            for s in &ss {
                if used.contains(s) {
                    at.error(ds, "port", "schema.catch", "catch selectors overlap");
                }
                used.push(s.clone());
            }
            result.insert(b.clone(), ss);
        }
    }
    if let Some(b) = wildcard {
        let universe = if atomic {
            vec!["execution", "timeout", "contract"]
        } else {
            vec!["execution", "timeout", "contract", "max_steps"]
        };
        let remainder: Vec<_> = universe
            .into_iter()
            .filter(|s| !used.iter().any(|x| x == s))
            .map(str::to_owned)
            .collect();
        if remainder.is_empty() {
            a.at(&b).error(
                ds,
                "port",
                "schema.catch",
                "wildcard catch expands to an empty set",
            );
        }
        result.insert(b, remainder);
    }
    result
}

fn endpoint(s: &str) -> Result<SourceEndpoint, String> {
    if s == "start" {
        return Ok(SourceEndpoint::Start);
    }
    if let Some((node, branch)) = s.split_once('.') {
        if control_id(node) && control_id(branch) {
            return Ok(SourceEndpoint::Branch {
                node_id: node.into(),
                branch_id: branch.into(),
            });
        }
    } else if control_id(s) {
        return Ok(SourceEndpoint::Node { node_id: s.into() });
    }
    Err(format!("invalid source endpoint {s}"))
}
fn target(s: &str) -> Result<TargetEndpoint, String> {
    if s == "fail" {
        return Ok(TargetEndpoint::Fail);
    }
    if s == "end" {
        return Ok(TargetEndpoint::Normal(JoinTarget::End { branch_id: None }));
    }
    if let Some(b) = s.strip_prefix("end.").filter(|b| control_id(b)) {
        return Ok(TargetEndpoint::Normal(JoinTarget::End {
            branch_id: Some(b.into()),
        }));
    }
    if control_id(s) {
        return Ok(TargetEndpoint::Normal(JoinTarget::Node {
            node_id: s.into(),
        }));
    }
    Err(format!("invalid target endpoint {s}"))
}
fn edge(raw: &str, edge_index: usize) -> Result<Route, String> {
    if raw.contains(['\n', '\r']) {
        return Err("edge strings cannot contain newlines".into());
    }
    let parts: Vec<_> = raw
        .split("->")
        .map(|p| p.trim_matches([' ', '\t']))
        .collect();
    if !(2..=3).contains(&parts.len()) {
        return Err("expected a direct edge or fork-join edge".into());
    }
    let source = endpoint(parts[0])?;
    let edge = if parts.len() == 2 {
        EdgeAst::Direct {
            target: target(parts[1])?,
        }
    } else {
        let group = parts[1]
            .strip_prefix('[')
            .and_then(|s| s.strip_suffix(']'))
            .ok_or("three-part edges require a concurrent group")?;
        let mut workers = vec![];
        for w in group.split(',').map(|s| s.trim_matches([' ', '\t'])) {
            if !control_id(w) || workers.iter().any(|s| s == w) {
                return Err("workers must be distinct node identifiers".into());
            }
            workers.push(w.to_owned());
        }
        if workers.len() < 2 {
            return Err("concurrent groups require at least two workers".into());
        }
        let TargetEndpoint::Normal(join_target) = target(parts[2])? else {
            return Err("fork-join cannot join at fail".into());
        };
        EdgeAst::ForkJoin {
            workers,
            join_target,
        }
    };
    Ok(Route {
        source,
        edge,
        edge_index,
        raw: raw.into(),
        kill_vars: vec![],
    })
}
