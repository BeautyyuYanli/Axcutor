//! Passes 2 and 3: explicit declarations, isolated graph frames and modular
//! type checking. A node only consumes the pre-collected workflow signatures.
use super::*;

pub(super) fn type_of(symbols: &Symbols, path: &FieldPath) -> Result<TypeSpec, String> {
    let mut t = symbols
        .get(&path.root)
        .ok_or_else(|| format!("undeclared variable {}", path.root))?
        .clone();
    for (i, f) in path.fields.iter().enumerate() {
        if t.nullable {
            return Err(format!(
                "cannot project through nullable prefix in {}",
                path.text()
            ));
        }
        let TypeKind::TypedDict {
            properties,
            required,
        } = &t.kind
        else {
            return Err(format!(
                "only TypedDict supports field projection: {}",
                path.text()
            ));
        };
        let field = properties
            .get(f)
            .ok_or_else(|| format!("unknown field {f} in {}", path.text()))?;
        if !required.contains(f) {
            if i + 1 != path.fields.len() {
                return Err(format!("cannot traverse optional field {f}"));
            }
            t = field.make_nullable();
        } else {
            t = field.clone();
        }
    }
    Ok(t)
}

pub(crate) fn arg_type(arg: &ArgValue, symbols: &Symbols) -> Option<TypeSpec> {
    match arg {
        ArgValue::Path { path } => type_of(symbols, path).ok(),
        ArgValue::Const { value } => {
            let base = match value {
                Value::String(_) => Scalar::String,
                Value::Bool(_) => Scalar::Boolean,
                Value::Number(_) if integer_literal(value) => Scalar::Integer,
                Value::Number(_) => Scalar::Number,
                _ => return None,
            };
            Some(TypeSpec::scalar(base))
        }
    }
}

pub(super) fn argument(
    arg: &ArgValue,
    expected: &TypeSpec,
    symbols: &Symbols,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) {
    let compatible = match arg {
        ArgValue::Path { path } => match type_of(symbols, path) {
            Ok(t) => t.is_subtype_of(expected),
            // References are checked once by the caller, at their own parameter.
            Err(_) => return,
        },
        ArgValue::Const { value } => expected.accepts_literal(value),
    };
    if !compatible {
        a.error(
            ds,
            "variable",
            "type.compatibility",
            format!("argument does not satisfy {}", expected.to_value()),
        );
    }
}

pub(super) fn produced(
    actual: &TypeSpec,
    target: &str,
    symbols: &Symbols,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) {
    match symbols.get(target) {
        None => a.error(
            ds,
            "variable",
            "symbol.undeclared_output",
            format!("output variable {target} needs an explicit TypeSpec declaration"),
        ),
        Some(expected) if !actual.is_subtype_of(expected) => a.error(
            ds,
            "variable",
            "type.output",
            format!(
                "output type {} is incompatible with {target}: {}",
                actual.to_value(),
                expected.to_value()
            ),
        ),
        _ => (),
    }
}

fn register(
    symbols: &mut Symbols,
    name: &str,
    t: &TypeSpec,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) {
    if let Some(old) = symbols.get(name) {
        if !old.equivalent(t) {
            a.error(
                ds,
                "variable",
                "symbol.conflicting_type",
                format!("{name} has incompatible explicit declarations"),
            );
        }
    } else {
        symbols.insert(name.into(), t.clone());
    }
}

pub(super) fn root(
    root: &mut NormalizedRoot,
    a: &Address,
    signatures: &Ordered<Signature>,
    ds: &mut Vec<Diagnostic>,
    analyses: &mut Vec<GraphAnalysis>,
) {
    match &mut root.body {
        RootBody::Run { run, .. } => check_signal(run, &root.output, a, ds),
        RootBody::Graph { graph: g } => graph(
            g,
            &root.input,
            &root.output,
            &root.branches,
            &a.graph(false),
            signatures,
            ds,
            analyses,
        ),
    }
}

#[allow(clippy::too_many_arguments)]
fn graph(
    g: &mut NormalizedGraph,
    host: &Symbols,
    output: &Symbols,
    branches: &Ordered<BranchContract>,
    a: &Address,
    signatures: &Ordered<Signature>,
    ds: &mut Vec<Diagnostic>,
    analyses: &mut Vec<GraphAnalysis>,
) {
    let mut symbols = host.clone();
    for (n, t) in output {
        let mut host_address = a.clone();
        host_address.pointer = a
            .pointer
            .strip_suffix("/graph")
            .unwrap_or(&a.pointer)
            .into();
        register(&mut symbols, n, t, &host_address.at("output").at(n), ds);
    }
    for (n, v) in &g.variables {
        let at = a.at("variables").at(n);
        if host.contains_key(n) {
            at.error(
                ds,
                "variable",
                "symbol.host_collision",
                format!("host input {n} cannot be redeclared in variables"),
            );
        }
        register(&mut symbols, n, &v.typespec, &at, ds);
        if let InitialValue::Default(value) = &v.initial {
            if !v.typespec.accepts_literal(value) {
                at.at("default").error(
                    ds,
                    "variable",
                    "type.default",
                    "default literal does not satisfy its TypeSpec",
                );
            }
        }
    }
    for (id, n) in &g.nodes {
        if n.modifiers.for_each.is_none() {
            if let NodeBody::Run { output, .. } | NodeBody::Graph { output, .. } = &n.body {
                for (name, t) in output {
                    register(&mut symbols, name, t, &a.node(id).at("output").at(name), ds);
                }
            }
        }
    }
    let index = analyses.len();
    analyses.push(GraphAnalysis {
        workflow_id: a.workflow.clone(),
        scope_graph_path: a.scope.clone(),
        symbols: symbols.clone(),
        nodes: Ordered::new(),
    });
    for (id, node) in &mut g.nodes {
        let at = a.node(id);
        // All body kinds use one authority for collisions. Check the boundary
        // before descending into an inline graph, so an invalid host cannot
        // justify trusted child facts.
        let normal_branches = match &node.body {
            NodeBody::Run { branches, .. } | NodeBody::Graph { branches, .. } => Some(branches),
            NodeBody::Workflow { workflow_id, .. } => {
                signatures.get(workflow_id).map(|s| &s.branches)
            }
            NodeBody::Builtin { .. } => None,
        };
        for b in node.modifiers.catch.keys() {
            if normal_branches.is_some_and(|branches| branches.contains_key(b)) {
                at.at("catch").at(b).error(
                    ds,
                    "port",
                    "type.branch_collision",
                    "catch collides with a normal business branch",
                );
            }
        }
        let (injected, source_type) = injection(node, &symbols, &at, ds);
        let mut local = symbols.clone();
        for (name, t) in &injected {
            local.insert(name.clone(), t.clone());
        }
        let mut single = Symbols::new();
        let mut normal_ports = vec![];
        match &mut node.body {
            NodeBody::Run {
                run,
                input,
                input_schema,
                output,
                branches,
                ..
            } => {
                check_signal(run, output, &at, ds);
                for (i, n) in input.iter().enumerate() {
                    if let Some(t) = local.get(n) {
                        input_schema.insert(n.clone(), t.clone());
                    } else {
                        at.at("input").at(&i.to_string()).error(
                            ds,
                            "variable",
                            "symbol.undeclared",
                            format!("undeclared input {n}"),
                        );
                    }
                }
                single = output.clone();
                normal_ports = contract_ports(output.keys().cloned().collect(), branches, None);
            }
            NodeBody::Graph {
                input,
                output,
                branches,
                graph: inner,
            } => {
                let mut captured = Symbols::new();
                for (i, n) in input.iter().enumerate() {
                    if let Some(t) = symbols.get(n) {
                        captured.insert(n.clone(), t.clone());
                    } else {
                        at.at("input").at(&i.to_string()).error(
                            ds,
                            "variable",
                            "symbol.undeclared",
                            format!("undeclared capture {n}"),
                        );
                    }
                }
                for (n, t) in &injected {
                    captured.insert(n.clone(), t.clone());
                }
                let boundary_valid = !ds.iter().any(|d| {
                    d.workflow_id == a.workflow
                        && d.scope_graph_path == a.scope
                        && d.node_id.as_deref() == Some(id)
                });
                let first_child = analyses.len();
                graph(
                    inner,
                    &captured,
                    output,
                    branches,
                    &at.graph(true),
                    signatures,
                    ds,
                    analyses,
                );
                if !boundary_valid {
                    for child in &mut analyses[first_child..] {
                        for node in child.nodes.values_mut() {
                            node.definite_in = None;
                        }
                    }
                }
                single = output.clone();
                normal_ports = contract_ports(output.keys().cloned().collect(), branches, None);
            }
            NodeBody::Workflow {
                workflow_id,
                bindings,
            } => {
                check_references(bindings, &local, &at, ds);
                if !workflow_visible(&a.workflow, workflow_id) {
                    at.at("use").error(
                        ds,
                        "node",
                        "visibility.private_workflow",
                        format!("workflow {workflow_id} is private to its parent package"),
                    );
                }
                if let Some(target) = signatures.get(workflow_id) {
                    for p in target.input.keys() {
                        if !bindings.input.contains_key(p) {
                            at.at("with").at("input").at(p).error(
                                ds,
                                "node",
                                "type.call_input",
                                format!("missing required parameter {p}"),
                            );
                        }
                    }
                    for (p, arg) in &bindings.input {
                        let addr = at.at("with").at("input").at(p);
                        if let Some(t) = target.input.get(p) {
                            argument(arg, t, &local, &addr, ds);
                        } else {
                            addr.error(
                                ds,
                                "node",
                                "type.call_input",
                                format!("unknown parameter {p}"),
                            );
                        }
                    }
                    for (field, n) in &bindings.output {
                        if let Some(t) = target.output.get(field) {
                            single.insert(n.clone(), t.clone());
                        } else {
                            at.at("with").at("output").at(field).error(
                                ds,
                                "variable",
                                "type.call_output",
                                format!("unknown target output {field}"),
                            );
                        }
                    }
                    normal_ports = contract_ports(
                        node_outputs(bindings),
                        &target.branches,
                        Some(&bindings.output),
                    );
                } else {
                    at.at("use").error(
                        ds,
                        "node",
                        "type.unresolved_workflow",
                        format!("workflow {workflow_id} is missing or has an invalid signature"),
                    );
                }
            }
            NodeBody::Builtin { op, bindings } => {
                check_references(bindings, &local, &at, ds);
                normal_ports = builtins::check(op, bindings, &local, &at, ds);
            }
        }
        for (n, t) in &single {
            let output_address = if let NodeBody::Workflow { bindings, .. } = &node.body {
                let field = bindings
                    .output
                    .iter()
                    .find_map(|(field, target)| (target == n).then_some(field))
                    .expect("mapped single output");
                at.at("with").at("output").at(field)
            } else {
                at.at("output").at(n)
            };
            if let Some(source) = &source_type {
                let lifted = match source.kind {
                    TypeKind::Array(_) => TypeSpec::array(t.clone()),
                    _ => TypeSpec::map(t.clone()),
                };
                produced(&lifted, n, &symbols, &output_address, ds);
            } else if node.modifiers.for_each.is_none() {
                produced(t, n, &symbols, &output_address, ds);
            }
        }
        let mut ports = if node.modifiers.for_each.is_some() {
            vec![NodePort {
                branch_id: None,
                kind: PortKind::Normal,
                output: node.out_vars(),
                kill_vars: vec![],
            }]
        } else {
            normal_ports.extend(node.modifiers.catch.keys().map(|b| NodePort {
                branch_id: Some(b.clone()),
                kind: PortKind::Catch,
                output: vec![],
                kill_vars: vec![],
            }));
            normal_ports
        };
        // Keep output and kill sets in declaration order, including ignored workflow
        // outputs mapped out of branch contracts.
        for p in &mut ports {
            p.output.dedup();
        }
        let valid = !ds.iter().any(|d| {
            d.workflow_id == a.workflow
                && d.scope_graph_path == a.scope
                && d.node_id.as_deref() == Some(id)
        });
        analyses[index].nodes.insert(
            id.clone(),
            NodeAnalysis {
                definite_in: None,
                injected,
                ports: valid.then_some(ports),
            },
        );
    }
    let topology_ok = flow::topology(g, &analyses[index], branches, a, ds);
    // Errors anywhere inside this graph's frame subtree invalidate its executable
    // behavior. Independent sibling frames can still prove their own facts.
    let safe = topology_ok
        && !ds
            .iter()
            .any(|d| d.workflow_id == a.workflow && d.scope_graph_path.starts_with(&a.scope));
    if safe {
        let before_assignment = ds.len();
        flow::assignment(g, host, output, branches, &mut analyses[index], a, ds);
        if ds.len() != before_assignment {
            for node in analyses[index].nodes.values_mut() {
                node.definite_in = None;
            }
        }
    }
}

fn check_signal(run: &RunSpec, output: &Symbols, a: &Address, ds: &mut Vec<Diagnostic>) {
    if matches!(run, RunSpec::Signal) && output.values().any(TypeSpec::contains_handle) {
        a.at("output").error(
            ds,
            "node",
            "type.signal_handle",
            "signal output must contain only pure values",
        );
    }
}

fn injection(
    node: &NormalizedNode,
    symbols: &Symbols,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) -> (Symbols, Option<TypeSpec>) {
    let mut injected = Symbols::new();
    let Some(iter) = &node.modifiers.for_each else {
        return (injected, None);
    };
    let source = match type_of(symbols, &iter.source) {
        Ok(t) => t,
        Err(e) => {
            a.at("for_each")
                .at("source")
                .error(ds, "variable", "type.projection", e);
            return (injected, None);
        }
    };
    let elem = match &source.kind {
        TypeKind::Array(t) if !source.nullable => {
            if iter.key.is_some() {
                a.at("for_each").at("key").error(
                    ds,
                    "variable",
                    "type.iteration_key",
                    "array iteration cannot inject key",
                );
            }
            t.as_ref()
        }
        TypeKind::OrderedMap(t) if !source.nullable => t.as_ref(),
        _ => {
            a.at("for_each").at("source").error(
                ds,
                "variable",
                "type.iteration_source",
                "for_each needs a non-null array or OrderedMap",
            );
            return (injected, None);
        }
    };
    injected.insert(iter.item.clone(), elem.clone());
    if let Some(k) = &iter.key {
        injected.insert(k.clone(), TypeSpec::scalar(Scalar::String));
    }
    if let Some(i) = &iter.index {
        injected.insert(i.clone(), TypeSpec::scalar(Scalar::Integer));
    }
    for n in injected.keys() {
        let collision = match &node.body {
            NodeBody::Graph { input, output, .. } => input.contains(n) || output.contains_key(n),
            _ => symbols.contains_key(n),
        };
        if collision {
            a.at("for_each").error(
                ds,
                "variable",
                "symbol.injected_collision",
                format!("iteration variable {n} collides at the frame boundary"),
            );
        }
    }
    (injected, Some(source))
}

fn check_references(bindings: &Bindings, symbols: &Symbols, a: &Address, ds: &mut Vec<Diagnostic>) {
    for (p, arg) in &bindings.input {
        if let ArgValue::Path { path } = arg {
            if let Err(e) = type_of(symbols, path) {
                a.at("with")
                    .at("input")
                    .at(p)
                    .error(ds, "variable", "type.projection", e);
            }
        }
    }
}
fn node_outputs(b: &Bindings) -> Vec<String> {
    b.output.values().cloned().collect()
}

fn contract_ports(
    output: Vec<String>,
    branches: &Ordered<BranchContract>,
    mapping: Option<&Ordered<String>>,
) -> Vec<NodePort> {
    if branches.is_empty() {
        return vec![NodePort {
            branch_id: None,
            kind: PortKind::Normal,
            output,
            kill_vars: vec![],
        }];
    }
    branches
        .iter()
        .map(|(b, s)| NodePort {
            branch_id: Some(b.clone()),
            kind: PortKind::Normal,
            output: s
                .output
                .iter()
                .filter_map(|n| match mapping {
                    Some(m) => m.get(n).cloned(),
                    None => Some(n.clone()),
                })
                .collect(),
            kill_vars: vec![],
        })
        .collect()
}
