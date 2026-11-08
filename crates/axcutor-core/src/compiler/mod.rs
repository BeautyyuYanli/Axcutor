//! Five-pass compiler and stateless canvas queries. Failures never yield a bundle.
mod builtins;
mod check;
pub(crate) use check::arg_type;
mod flow;
use crate::source as parse;
pub use crate::source::{SourceDocument, SourceFormat, SourceLocation};
mod schema;

use crate::ast::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::VecDeque;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CompileRequest {
    pub entry: String,
    pub packages: Ordered<SourceDocument>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Diagnostic {
    pub workflow_id: String,
    pub scope_graph_path: Vec<String>,
    pub target_kind: String,
    pub node_id: Option<String>,
    pub branch_id: Option<String>,
    pub edge_index: Option<usize>,
    pub param_path: String,
    pub code: String,
    pub message: String,
    pub source_location: Option<SourceLocation>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortKind {
    Normal,
    Catch,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NodePort {
    /// None is the default normal port; catch ports are always named.
    pub branch_id: Option<String>,
    pub kind: PortKind,
    pub output: Vec<String>,
    pub kill_vars: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct NodeAnalysis {
    /// None means no trustworthy entry fact has been proved for this graph.
    pub definite_in: Option<Vec<String>>,
    pub injected: Symbols,
    /// None means type/structure prerequisites for this node's ports failed.
    pub ports: Option<Vec<NodePort>>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GraphAnalysis {
    pub workflow_id: String,
    pub scope_graph_path: Vec<String>,
    pub symbols: Symbols,
    pub nodes: Ordered<NodeAnalysis>,
}

#[derive(Clone, Debug, Serialize)]
pub struct CompileOutput {
    pub diagnostics: Vec<Diagnostic>,
    pub analysis: Vec<GraphAnalysis>,
    pub bundle: Option<WorkflowBundle>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VariableCandidate {
    pub path: FieldPath,
    pub typespec: TypeSpec,
}

#[derive(Clone)]
struct Address {
    workflow: String,
    scope: Vec<String>,
    node: Option<String>,
    pointer: String,
}

impl Address {
    fn root(id: &str) -> Self {
        Self {
            workflow: id.into(),
            scope: vec![],
            node: None,
            pointer: String::new(),
        }
    }
    fn at(&self, key: &str) -> Self {
        let mut a = self.clone();
        a.pointer.push('/');
        a.pointer
            .push_str(&key.replace('~', "~0").replace('/', "~1"));
        a
    }
    fn node(&self, id: &str) -> Self {
        let mut a = self.at("nodes").at(id);
        a.node = Some(id.into());
        a
    }
    fn graph(&self, inline: bool) -> Self {
        let mut a = self.at("graph");
        if inline {
            if let Some(id) = &self.node {
                a.scope.push(id.clone());
            }
        }
        a.node = None;
        a
    }
    fn error(&self, ds: &mut Vec<Diagnostic>, kind: &str, code: &str, message: impl Into<String>) {
        ds.push(Diagnostic {
            workflow_id: self.workflow.clone(),
            scope_graph_path: self.scope.clone(),
            target_kind: kind.into(),
            node_id: self.node.clone(),
            branch_id: None,
            edge_index: None,
            param_path: self.pointer.clone(),
            code: code.into(),
            message: message.into(),
            source_location: None,
        });
    }
    fn edge_error(
        &self,
        ds: &mut Vec<Diagnostic>,
        index: usize,
        code: &str,
        message: impl Into<String>,
    ) {
        self.at("edges")
            .at(&index.to_string())
            .error(ds, "edge", code, message);
        ds.last_mut().unwrap().edge_index = Some(index);
    }
    fn port_error(
        &self,
        ds: &mut Vec<Diagnostic>,
        branch: Option<&str>,
        code: &str,
        message: impl Into<String>,
    ) {
        self.error(ds, "port", code, message);
        ds.last_mut().unwrap().branch_id = branch.map(str::to_owned);
    }
}

#[derive(Clone)]
struct Signature {
    input: Symbols,
    output: Symbols,
    branches: Ordered<BranchContract>,
}

struct Document {
    raw: Value,
    root: Option<NormalizedRoot>,
    signature: Option<Signature>,
}

/// Compile only the entry's dependency closure, using a complete signature snapshot
/// before implementation checking. Recursive calls never expand another graph body.
pub fn compile_workflow(request: CompileRequest) -> CompileOutput {
    let mut diagnostics = vec![];
    for id in request.packages.keys() {
        if id != "__main__" && !package_path(id) {
            Address::root(id).error(
                &mut diagnostics,
                "document",
                "schema.package_path",
                "packages must use absolute dotted paths with public roots",
            );
        }
    }
    if request.entry != "__main__" && !public_package_path(&request.entry) {
        Address::root(&request.entry).error(
            &mut diagnostics,
            "document",
            "schema.entry",
            "entry must be __main__ or a public absolute package path",
        );
    }
    let mut documents: Ordered<Document> = Ordered::new();
    let mut pending = VecDeque::from([request.entry.clone()]);
    let mut discovered = std::collections::HashSet::new();
    while let Some(id) = pending.pop_front() {
        if !discovered.insert(id.clone()) {
            continue;
        }
        let a = Address::root(&id);
        if id != "__main__" && !package_path(&id) {
            continue;
        }
        let Some(source) = request.packages.get(&id) else {
            if id == request.entry {
                a.error(
                    &mut diagnostics,
                    "document",
                    "schema.missing_package",
                    "entry package is missing",
                );
            }
            continue;
        };
        let raw = match parse::load(source) {
            Ok(v) => v,
            Err((message, location)) => {
                a.error(&mut diagnostics, "document", "parse.source", message);
                diagnostics.last_mut().unwrap().source_location = location;
                documents.insert(
                    id,
                    Document {
                        raw: Value::Null,
                        root: None,
                        signature: None,
                    },
                );
                continue;
            }
        };
        schema::dependencies(&raw, &mut pending);
        let (root, signature) = schema::root(&raw, &a, &mut diagnostics);
        documents.insert(
            id,
            Document {
                raw,
                root,
                signature,
            },
        );
    }
    let signatures: Ordered<_> = documents
        .iter()
        .filter_map(|(id, d)| d.signature.clone().map(|s| (id.clone(), s)))
        .collect();
    let mut analysis = vec![];
    for (id, d) in &mut documents {
        if let Some(root) = &mut d.root {
            check::root(
                root,
                &Address::root(id),
                &signatures,
                &mut diagnostics,
                &mut analysis,
            );
        }
    }
    suppress_invalid_dependencies(&documents, &diagnostics, &mut analysis);
    // Structural JSON pointers, rather than pass execution order, define diagnostic
    // order. Stable ties retain the order of independent checks at the same address.
    diagnostics.sort_by_key(|d| {
        let order = documents.get_index_of(&d.workflow_id).unwrap_or(0);
        let mut rank = vec![];
        if let Some(document) = documents.get(&d.workflow_id) {
            let mut value = &document.raw;
            for part in d.param_path.split('/').skip(1) {
                let key = part.replace("~1", "/").replace("~0", "~");
                if let Some(m) = value.as_object() {
                    rank.push(m.keys().position(|k| k == &key).unwrap_or(m.len()));
                    value = m.get(&key).unwrap_or(&Value::Null);
                } else if let Some(a) = value.as_array() {
                    let i = key.parse().unwrap_or(a.len());
                    rank.push(i);
                    value = a.get(i).unwrap_or(&Value::Null);
                }
            }
        }
        (order, rank)
    });
    let bundle = diagnostics.is_empty().then(|| WorkflowBundle {
        entry: request.entry,
        workflows: documents
            .into_iter()
            .filter_map(|(id, d)| d.root.map(|r| (id, r)))
            .collect(),
    });
    CompileOutput {
        diagnostics,
        analysis,
        bundle,
    }
}

/// A valid signature is enough to check a recursive call, but an invalid callee
/// implementation cannot justify published definite-assignment facts. Propagate
/// this dependency only across affected frame subtrees, preserving independent
/// inline graph analysis in an otherwise invalid document.
fn suppress_invalid_dependencies(
    documents: &Ordered<Document>,
    diagnostics: &[Diagnostic],
    analyses: &mut [GraphAnalysis],
) {
    fn references_invalid(
        g: &NormalizedGraph,
        invalid: &std::collections::HashSet<String>,
    ) -> bool {
        g.nodes.values().any(|n| match &n.body {
            NodeBody::Workflow { workflow_id, .. } => invalid.contains(workflow_id),
            NodeBody::Graph { graph, .. } => references_invalid(graph, invalid),
            _ => false,
        })
    }
    let mut invalid: std::collections::HashSet<_> =
        diagnostics.iter().map(|d| d.workflow_id.clone()).collect();
    loop {
        let before = invalid.len();
        for (id, d) in documents {
            if let Some(NormalizedRoot {
                body: RootBody::Graph { graph },
                ..
            }) = &d.root
            {
                if references_invalid(graph, &invalid) {
                    invalid.insert(id.clone());
                }
            }
        }
        if before == invalid.len() {
            break;
        }
    }
    for analysis in analyses {
        let Some(NormalizedRoot {
            body: RootBody::Graph { graph },
            ..
        }) = documents
            .get(&analysis.workflow_id)
            .and_then(|d| d.root.as_ref())
        else {
            continue;
        };
        let mut graph = graph;
        for id in &analysis.scope_graph_path {
            let NodeBody::Graph { graph: child, .. } = &graph.nodes[id].body else {
                unreachable!("analysis path names an inline graph")
            };
            graph = child;
        }
        if references_invalid(graph, &invalid) {
            for node in analysis.nodes.values_mut() {
                node.definite_in = None;
            }
        }
    }
}

/// Only proved entry variables are candidates. Optional leaf fields are nullable;
/// nullable prefixes, dynamic map keys and array indices are never traversed.
pub fn get_available_vars_at_node(
    analysis: &GraphAnalysis,
    node_id: &str,
    expected: Option<&TypeSpec>,
) -> Vec<VariableCandidate> {
    let Some(node) = analysis.nodes.get(node_id) else {
        return vec![];
    };
    let Some(assigned) = &node.definite_in else {
        return vec![];
    };
    fn visit(
        path: FieldPath,
        t: TypeSpec,
        expected: Option<&TypeSpec>,
        out: &mut Vec<VariableCandidate>,
    ) {
        if expected.is_none_or(|e| t.is_subtype_of(e)) {
            out.push(VariableCandidate {
                path: path.clone(),
                typespec: t.clone(),
            });
        }
        if !t.nullable {
            if let TypeKind::TypedDict {
                properties,
                required,
            } = &t.kind
            {
                for (name, t) in properties {
                    let t = if required.contains(name) {
                        t.clone()
                    } else {
                        t.make_nullable()
                    };
                    let mut p = path.clone();
                    p.fields.push(name.clone());
                    visit(p, t, expected, out);
                }
            }
        }
    }
    let mut out = vec![];
    for (name, t) in analysis
        .symbols
        .iter()
        .filter(|(n, _)| assigned.contains(n) && !node.injected.contains_key(*n))
        .chain(node.injected.iter())
    {
        visit(
            FieldPath {
                root: name.clone(),
                fields: vec![],
            },
            t.clone(),
            expected,
            &mut out,
        );
    }
    out
}

pub fn derive_node_ports(analysis: &GraphAnalysis, node_id: &str) -> Vec<NodePort> {
    analysis
        .nodes
        .get(node_id)
        .and_then(|n| n.ports.clone())
        .unwrap_or_default()
}

/// Malformed envelopes are host argument errors. Source/DSL errors are results.
pub fn compile_workflow_json(text: &str) -> Result<String, String> {
    // Reject duplicate package keys before serde's map can overwrite a mount.
    let request: CompileRequest = json_argument(text, parse::MAX_MODEL_DEPTH)?;
    serde_json::to_string(&compile_workflow(request)).map_err(|e| e.to_string())
}

fn json_argument<T: serde::de::DeserializeOwned>(
    text: &str,
    max_depth: usize,
) -> Result<T, String> {
    let value = parse::load_json(text, max_depth).map_err(|(message, _)| message)?;
    serde_json::from_value(value).map_err(|e| e.to_string())
}

pub fn available_vars_json(
    analysis: &str,
    node: &str,
    expected: Option<&str>,
) -> Result<String, String> {
    let analysis = json_argument(analysis, parse::MAX_MODEL_DEPTH)?;
    let expected: Option<TypeSpec> = expected
        .map(|text| json_argument(text, parse::MAX_SOURCE_DEPTH))
        .transpose()?;
    serde_json::to_string(&get_available_vars_at_node(
        &analysis,
        node,
        expected.as_ref(),
    ))
    .map_err(|e| e.to_string())
}
pub fn node_ports_json(analysis: &str, node: &str) -> Result<String, String> {
    let analysis = json_argument(analysis, parse::MAX_MODEL_DEPTH)?;
    serde_json::to_string(&derive_node_ports(&analysis, node)).map_err(|e| e.to_string())
}
