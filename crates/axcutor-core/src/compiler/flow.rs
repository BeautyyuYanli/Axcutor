//! Pass 4 validates routing and SESE; pass 5 solves a greatest fixed point.
//! Fork workers consume exactly the source snapshot. Only their boundary output
//! sets are added at the join, never at another worker's entry.
use super::*;
use bitvec::prelude::*;
use std::collections::{HashMap, HashSet};

fn target(route: &Route) -> String {
    match &route.edge {
        EdgeAst::Direct {
            target: TargetEndpoint::Fail,
        } => "fail".into(),
        EdgeAst::Direct {
            target: TargetEndpoint::Normal(t),
        }
        | EdgeAst::ForkJoin { join_target: t, .. } => t.key(),
    }
}
fn port_for<'a>(analysis: &'a GraphAnalysis, source: &SourceEndpoint) -> Option<&'a NodePort> {
    let (id, branch) = match source {
        SourceEndpoint::Start => return None,
        SourceEndpoint::Node { node_id } => (node_id, None),
        SourceEndpoint::Branch { node_id, branch_id } => (node_id, Some(branch_id)),
    };
    analysis
        .nodes
        .get(id)?
        .ports
        .as_ref()?
        .iter()
        .find(|p| p.branch_id.as_ref() == branch)
}

pub(super) fn topology(
    g: &mut NormalizedGraph,
    analysis: &GraphAnalysis,
    branches: &Ordered<BranchContract>,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) -> bool {
    let before = ds.len();
    let known_invalid = |id: &str| {
        ds.iter().any(|d| {
            d.workflow_id == a.workflow
                && d.scope_graph_path == a.scope
                && d.node_id.as_deref() == Some(id)
        })
    };
    let mut missing = HashSet::new();
    for route in g.routes.values() {
        if let Some(id) = route.source.node() {
            if !g.nodes.contains_key(id) && !known_invalid(id) {
                missing.insert((route.edge_index, id.to_owned()));
            }
        }
        let t = target(route);
        if !matches!(t.as_str(), "end" | "fail")
            && !t.starts_with("end.")
            && !g.nodes.contains_key(&t)
            && !known_invalid(&t)
        {
            missing.insert((route.edge_index, t));
        }
        if let EdgeAst::ForkJoin { workers, .. } = &route.edge {
            for w in workers {
                if !g.nodes.contains_key(w) && !known_invalid(w) {
                    missing.insert((route.edge_index, w.clone()));
                }
            }
        }
    }
    // Hash collections are lookup-only elsewhere. Sorting diagnostic material keeps
    // error arrays independent of process hash seeds and native/WASM environments.
    let mut missing: Vec<_> = missing.into_iter().collect();
    missing.sort();
    for (i, n) in missing {
        a.edge_error(ds, i, "topology.unknown_node", format!("unknown node {n}"));
    }
    let mut workers: Ordered<usize> = Ordered::new();
    for route in g.routes.values() {
        if let EdgeAst::ForkJoin { workers: ws, .. } = &route.edge {
            for w in ws {
                if workers.insert(w.clone(), route.edge_index).is_some() {
                    a.edge_error(
                        ds,
                        route.edge_index,
                        "topology.worker_exclusive",
                        format!("worker {w} belongs to multiple fork edges"),
                    );
                }
                if let Some(ports) = analysis.nodes.get(w).and_then(|n| n.ports.as_ref()) {
                    if ports.len() != 1
                        || ports[0].branch_id.is_some()
                        || ports[0].kind != PortKind::Normal
                    {
                        a.edge_error(
                            ds,
                            route.edge_index,
                            "topology.worker_ports",
                            format!("worker {w} exposes business or catch branches"),
                        );
                    }
                }
            }
            for (i, w) in ws.iter().enumerate() {
                for other in &ws[..i] {
                    if let (Some(left), Some(right)) = (g.nodes.get(w), g.nodes.get(other)) {
                        let overlap: Vec<_> = left
                            .out_vars()
                            .into_iter()
                            .filter(|n| right.out_vars().contains(n))
                            .collect();
                        if !overlap.is_empty() {
                            a.edge_error(
                                ds,
                                route.edge_index,
                                "topology.concurrent_write",
                                format!(
                                    "workers {other} and {w} both write {}",
                                    overlap.join(", ")
                                ),
                            );
                        }
                    }
                }
            }
        }
    }
    for (worker, owner) in &workers {
        for route in g.routes.values() {
            let touches = route.source.node() == Some(worker.as_str())
                || target(route) == *worker
                || (route.edge_index != *owner
                    && matches!(&route.edge,EdgeAst::ForkJoin { workers, .. } if workers.contains(worker)));
            if touches {
                a.edge_error(
                    ds,
                    route.edge_index,
                    "topology.worker_exclusive",
                    format!("worker {worker} may appear only in its concurrent group"),
                );
            }
        }
    }
    if !g.routes.contains_key("start") {
        a.port_error(
            ds,
            None,
            "topology.start",
            "start requires exactly one route",
        );
    }
    for (id, node) in &analysis.nodes {
        if workers.contains_key(id) {
            continue;
        }
        if let Some(ports) = &node.ports {
            for p in ports {
                let key = p
                    .branch_id
                    .as_ref()
                    .map_or_else(|| id.clone(), |b| format!("{id}.{b}"));
                if !g.routes.contains_key(&key) {
                    a.node(id).port_error(
                        ds,
                        p.branch_id.as_deref(),
                        "topology.missing_route",
                        format!("port {key} needs exactly one route"),
                    );
                }
            }
        }
    }
    for route in g.routes.values_mut() {
        if let Some(id) = route.source.node() {
            if analysis.nodes.get(id).is_some_and(|n| n.ports.is_some())
                && port_for(analysis, &route.source).is_none()
            {
                a.edge_error(
                    ds,
                    route.edge_index,
                    "topology.unknown_port",
                    format!("source {} is not an exposed port", route.source.key()),
                );
            }
            route.kill_vars = port_for(analysis, &route.source)
                .map(|p| p.kill_vars.clone())
                .unwrap_or_default();
        }
        let t = target(route);
        if t == "end" && !branches.is_empty()
            || t.starts_with("end.") && !branches.contains_key(&t[4..])
        {
            a.edge_error(
                ds,
                route.edge_index,
                "topology.end_branch",
                format!("terminal {t} does not match the host branches"),
            );
        }
    }
    let mut forward: HashMap<String, Vec<String>> = HashMap::new();
    let mut reverse: HashMap<String, Vec<String>> = HashMap::new();
    let mut add = |from: String, to: String| {
        forward.entry(from.clone()).or_default().push(to.clone());
        reverse.entry(to).or_default().push(from);
    };
    for route in g.routes.values() {
        let src = route.source.node().unwrap_or("start").to_owned();
        let t = target(route);
        match &route.edge {
            EdgeAst::Direct { .. } => add(src, t),
            EdgeAst::ForkJoin { workers, .. } => {
                for w in workers {
                    add(src.clone(), w.clone());
                    add(w.clone(), t.clone());
                }
            }
        }
    }
    fn reach(
        starts: impl IntoIterator<Item = String>,
        adj: &HashMap<String, Vec<String>>,
    ) -> HashSet<String> {
        let mut pending: VecDeque<_> = starts.into_iter().collect();
        let mut seen = HashSet::new();
        while let Some(n) = pending.pop_front() {
            if seen.insert(n.clone()) {
                if let Some(ns) = adj.get(&n) {
                    pending.extend(ns.iter().cloned());
                }
            }
        }
        seen
    }
    let reachable = reach(["start".into()], &forward);
    let terminals: Vec<_> = reverse
        .keys()
        .filter(|k| k.as_str() == "fail" || k.as_str() == "end" || k.starts_with("end."))
        .cloned()
        .collect();
    let exiting = reach(terminals, &reverse);
    for id in g.nodes.keys() {
        if !reachable.contains(id) {
            a.node(id).error(
                ds,
                "node",
                "topology.unreachable",
                format!("node {id} is unreachable from start"),
            );
        }
        if !exiting.contains(id) {
            a.node(id).error(
                ds,
                "node",
                "topology.no_exit",
                format!("node {id} has no path to a terminal"),
            );
        }
    }
    if !reachable
        .iter()
        .any(|n| n == "end" || n.starts_with("end."))
    {
        a.error(
            ds,
            "edge",
            "topology.no_normal_exit",
            "start must reach a normal end",
        );
    }
    ds.len() == before
}

pub(super) fn assignment(
    g: &NormalizedGraph,
    host: &Symbols,
    output: &Symbols,
    branches: &Ordered<BranchContract>,
    analysis: &mut GraphAnalysis,
    a: &Address,
    ds: &mut Vec<Diagnostic>,
) {
    let symbols = &analysis.symbols;
    let top: BitVec = bitvec![1; symbols.len()];
    let mut start: BitVec = bitvec![0; symbols.len()];
    for (i, name) in symbols.keys().enumerate() {
        if host.contains_key(name)
            || g.variables
                .get(name)
                .is_some_and(|v| matches!(v.initial, InitialValue::Default(_)))
        {
            start.set(i, true);
        }
    }
    let mut incoming: Ordered<BitVec> = g.nodes.keys().map(|n| (n.clone(), top.clone())).collect();
    for route in g.routes.values() {
        incoming.entry(target(route)).or_insert_with(|| top.clone());
    }
    let transfer = |bits: &BitVec, source: &SourceEndpoint| -> BitVec {
        let mut result = bits.clone();
        if let Some(port) = port_for(analysis, source) {
            for name in &port.kill_vars {
                result.set(
                    symbols.get_index_of(name).expect("checked kill variable"),
                    false,
                );
            }
            for name in &port.output {
                result.set(
                    symbols.get_index_of(name).expect("checked output variable"),
                    true,
                );
            }
        }
        result
    };
    loop {
        let mut next: Ordered<_> = incoming.keys().map(|n| (n.clone(), top.clone())).collect();
        for route in g.routes.values() {
            let source = route.source.node().map_or(&start, |id| &incoming[id]);
            let snapshot = transfer(source, &route.source);
            let mut joined = snapshot.clone();
            if let EdgeAst::ForkJoin { workers, .. } = &route.edge {
                for w in workers {
                    *next.get_mut(w).expect("checked worker") &= &snapshot;
                    for n in g.nodes[w].out_vars() {
                        joined.set(symbols.get_index_of(&n).expect("checked output"), true);
                    }
                }
            }
            *next.get_mut(&target(route)).expect("known target") &= &joined;
        }
        if next == incoming {
            break;
        }
        incoming = next;
    }
    // Locate an exit failure on each incoming route that actually lacks the
    // required value, rather than blaming an unrelated successful route.
    for route in g.routes.values() {
        let end = target(route);
        let required: Vec<_> = if end == "end" {
            output.keys().collect()
        } else if let Some(b) = end.strip_prefix("end.") {
            branches[b].output.iter().collect()
        } else {
            continue;
        };
        let source = route.source.node().map_or(&start, |id| &incoming[id]);
        let mut bits = transfer(source, &route.source);
        if let EdgeAst::ForkJoin { workers, .. } = &route.edge {
            for worker in workers {
                for name in g.nodes[worker].out_vars() {
                    bits.set(symbols.get_index_of(&name).expect("checked output"), true);
                }
            }
        }
        for name in required {
            if !bits[symbols.get_index_of(name).expect("host output symbol")] {
                a.edge_error(
                    ds,
                    route.edge_index,
                    "assignment.exit",
                    format!("{name} is not definitely assigned at {end}"),
                );
                ds.last_mut().unwrap().branch_id = end.strip_prefix("end.").map(str::to_owned);
            }
        }
    }
    for (id, node) in &g.nodes {
        let bits = &incoming[id];
        let facts = symbols
            .keys()
            .enumerate()
            .filter(|(i, _)| bits[*i])
            .map(|(_, s)| s.clone())
            .collect();
        analysis.nodes.get_mut(id).unwrap().definite_in = Some(facts);
        for (name, at) in reads(node, &a.node(id)) {
            if !bits[symbols.get_index_of(&name).expect("checked read symbol")] {
                at.error(
                    ds,
                    "variable",
                    "assignment.read",
                    format!("{name} is not definitely assigned at node {id}"),
                );
            }
        }
    }
}

fn reads(node: &NormalizedNode, a: &Address) -> Vec<(String, Address)> {
    let injected = node
        .modifiers
        .for_each
        .as_ref()
        .map(|f| {
            std::iter::once(f.item.clone())
                .chain(f.key.clone())
                .chain(f.index.clone())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let mut names: Ordered<Address> = Ordered::new();
    match &node.body {
        NodeBody::Run { input, .. } | NodeBody::Graph { input, .. } => {
            for (i, name) in input.iter().enumerate() {
                if !injected.contains(name) {
                    names
                        .entry(name.clone())
                        .or_insert_with(|| a.at("input").at(&i.to_string()));
                }
            }
        }
        NodeBody::Workflow { bindings, .. } | NodeBody::Builtin { bindings, .. } => {
            for (p, arg) in &bindings.input {
                if let ArgValue::Path { path } = arg {
                    if !injected.contains(&path.root) {
                        names
                            .entry(path.root.clone())
                            .or_insert_with(|| a.at("with").at("input").at(p));
                    }
                }
            }
        }
    }
    if let Some(iter) = &node.modifiers.for_each {
        names
            .entry(iter.source.root.clone())
            .or_insert_with(|| a.at("for_each").at("source"));
    }
    names.into_iter().collect()
}
