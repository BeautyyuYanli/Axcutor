//! Owned executable contract. Only fully checked documents enter a WorkflowBundle.
mod typespec;
pub use typespec::*;

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{Number, Value};

pub type Ordered<T> = IndexMap<String, T>;
pub type Symbols = Ordered<TypeSpec>;

/// A read-only path; writes always name one complete top-level variable.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FieldPath {
    pub root: String,
    pub fields: Vec<String>,
}

impl FieldPath {
    pub fn parse(text: &str) -> Option<Self> {
        let mut parts = text.split('.');
        let root = parts.next()?;
        let fields: Vec<_> = parts.map(str::to_owned).collect();
        (identifier(root) && fields.iter().all(|f| identifier(f))).then(|| Self {
            root: root.into(),
            fields,
        })
    }

    pub fn text(&self) -> String {
        std::iter::once(self.root.as_str())
            .chain(self.fields.iter().map(String::as_str))
            .collect::<Vec<_>>()
            .join(".")
    }
}

pub fn identifier(s: &str) -> bool {
    let mut chars = s.bytes();
    matches!(chars.next(), Some(b'a'..=b'z' | b'A'..=b'Z' | b'_'))
        && chars.all(|c| c.is_ascii_alphanumeric() || c == b'_')
}

pub fn control_id(s: &str) -> bool {
    identifier(s) && !matches!(s, "start" | "end" | "fail")
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ArgValue {
    Path { path: FieldPath },
    Const { value: Value },
}

impl<'de> Deserialize<'de> for ArgValue {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        use serde::de::Error;
        // Internally tagged enum buffering loses RawValue's JSON token kinds.
        // Read this small discriminator from the same bounded, exact decoder.
        let value = crate::source::deserialize_value(d)?;
        match value.get("kind").and_then(Value::as_str) {
            Some("path") => {
                let path = serde_json::from_value(
                    value
                        .get("path")
                        .cloned()
                        .ok_or_else(|| D::Error::custom("missing path"))?,
                )
                .map_err(D::Error::custom)?;
                Ok(Self::Path { path })
            }
            Some("const") => Ok(Self::Const {
                value: value
                    .get("value")
                    .cloned()
                    .ok_or_else(|| D::Error::custom("missing constant"))?,
            }),
            _ => Err(D::Error::custom("invalid ArgValue kind")),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Bindings {
    pub input: Ordered<ArgValue>,
    pub output: Ordered<String>,
    /// Presence matters for switch and branch-mode compare/logic, even when empty.
    pub output_present: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BranchContract {
    pub description: String,
    pub output: Vec<String>,
}

/// Option<Value> cannot represent a present null under serde. This enum can.
#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum InitialValue {
    Unbound,
    Default(Value),
}

#[derive(Clone, Debug, Serialize)]
pub struct VarDecl {
    pub typespec: TypeSpec,
    pub initial: InitialValue,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ForEach {
    pub source: FieldPath,
    pub item: String,
    pub key: Option<String>,
    pub index: Option<String>,
    pub concurrency: Number,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Retry {
    pub max_attempts: Number,
    pub backoff_ms: Number,
    pub on: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Modifiers {
    pub for_each: Option<ForEach>,
    pub timeout_ms: Option<Number>,
    pub retry: Option<Retry>,
    pub catch: Ordered<Vec<String>>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Builtin {
    Assign,
    Unwrap,
    Switch,
    Compare,
    Logic,
    Math,
    Length,
    ArrayGet,
    ArrayShift,
    ArrayAppend,
    ArrayConcat,
    ArrayFlatten,
    MapGet,
    MapSet,
    MapDelete,
    ObjectMerge,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum NodeBody {
    Run {
        input: Vec<String>,
        output: Symbols,
        branches: Ordered<BranchContract>,
        run: String,
        references: Vec<FieldPath>,
    },
    Graph {
        input: Vec<String>,
        output: Symbols,
        branches: Ordered<BranchContract>,
        graph: Box<NormalizedGraph>,
    },
    Workflow {
        workflow_id: String,
        bindings: Bindings,
    },
    Builtin {
        op: Builtin,
        bindings: Bindings,
    },
}

#[derive(Clone, Debug, Serialize)]
pub struct NormalizedNode {
    pub body: NodeBody,
    pub modifiers: Modifiers,
}

impl NormalizedNode {
    pub fn out_vars(&self) -> Vec<String> {
        match &self.body {
            NodeBody::Run { output, .. } | NodeBody::Graph { output, .. } => {
                output.keys().cloned().collect()
            }
            NodeBody::Workflow { bindings, .. } | NodeBody::Builtin { bindings, .. } => {
                bindings.output.values().cloned().collect()
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SourceEndpoint {
    Start,
    Node { node_id: String },
    Branch { node_id: String, branch_id: String },
}

impl SourceEndpoint {
    pub fn key(&self) -> String {
        match self {
            Self::Start => "start".into(),
            Self::Node { node_id } => node_id.clone(),
            Self::Branch { node_id, branch_id } => format!("{node_id}.{branch_id}"),
        }
    }
    pub fn node(&self) -> Option<&str> {
        match self {
            Self::Start => None,
            Self::Node { node_id } | Self::Branch { node_id, .. } => Some(node_id),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum JoinTarget {
    Node { node_id: String },
    End { branch_id: Option<String> },
}

impl JoinTarget {
    pub fn key(&self) -> String {
        match self {
            Self::Node { node_id } => node_id.clone(),
            Self::End { branch_id: None } => "end".into(),
            Self::End { branch_id: Some(b) } => format!("end.{b}"),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EdgeAst {
    Direct {
        target: TargetEndpoint,
    },
    ForkJoin {
        workers: Vec<String>,
        join_target: JoinTarget,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", content = "target", rename_all = "snake_case")]
pub enum TargetEndpoint {
    Normal(JoinTarget),
    Fail,
}

/// One routing representation. Declaration index/raw retain diagnostic provenance.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Route {
    pub source: SourceEndpoint,
    pub edge: EdgeAst,
    pub edge_index: usize,
    pub raw: String,
    pub kill_vars: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct NormalizedGraph {
    pub variables: Ordered<VarDecl>,
    pub nodes: Ordered<NormalizedNode>,
    pub routes: Ordered<Route>,
    pub max_steps: Number,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RootBody {
    Run {
        run: String,
        references: Vec<FieldPath>,
        timeout_ms: Option<Number>,
        retry: Option<Retry>,
    },
    Graph {
        graph: NormalizedGraph,
    },
}

#[derive(Clone, Debug, Serialize)]
pub struct NormalizedRoot {
    pub input: Symbols,
    pub output: Symbols,
    pub branches: Ordered<BranchContract>,
    pub body: RootBody,
}

pub type NormalizedAst = NormalizedRoot;

#[derive(Clone, Debug, Serialize)]
pub struct WorkflowBundle {
    pub entry: String,
    pub workflows: Ordered<NormalizedAst>,
}
