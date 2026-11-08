/** Rust's serialized, checked contract (ast/mod.rs and compiler/mod.rs).
 * Numeric/constant/enum values are only a metadata view here. Keep compiler JSON
 * text for probe calls and display; never round-trip this view back into WASM.
 */
export type SourceFormat = 'yaml' | 'json';
export interface SourceDocument { format: SourceFormat; text: string }
export interface CompileRequest { entry: string; packages: Record<string, SourceDocument> }
export interface TypeSpec {
  type: string | string[];
  description?: string;
  items?: TypeSpec;
  properties?: Record<string, TypeSpec>;
  required?: string[];
  additionalProperties?: TypeSpec;
  enum?: unknown[];
}
export type Symbols = Record<string, TypeSpec>;
export interface Diagnostic {
  workflow_id: string;
  scope_graph_path: string[];
  target_kind: string;
  node_id: string | null;
  branch_id: string | null;
  edge_index: number | null;
  param_path: string;
  code: string;
  message: string;
  source_location: { line: number; column: number } | null;
}
export interface NodePort {
  branch_id: string | null;
  kind: 'normal' | 'catch';
  output: string[];
  kill_vars: string[];
}
export interface NodeAnalysis {
  definite_in: string[] | null;
  injected: Symbols;
  ports: NodePort[] | null;
}
export interface GraphAnalysis {
  workflow_id: string;
  scope_graph_path: string[];
  symbols: Symbols;
  nodes: Record<string, NodeAnalysis>;
}
export interface FieldPath { root: string; fields: string[] }
export interface VariableCandidate { path: FieldPath; typespec: TypeSpec }
export interface BranchContract { description: string; output: string[] }
export interface Bindings {
  input: Record<string, { kind: 'path'; path: FieldPath } | { kind: 'const'; value: unknown }>;
  output: Record<string, string>;
  output_present: boolean;
}
export interface RunSpec { executor: string; [key: string]: unknown }
export interface Retry { max_attempts: number; backoff_ms: number; on: string[] }
export interface Modifiers {
  for_each: { source: FieldPath; item: string; key: string | null; index: string | null; concurrency: number } | null;
  retry: Retry | null;
  timeout_ms: number | null;
  catch: Record<string, string[]>;
}
export type NodeBody =
  | { kind: 'run'; workflow_id: string; input: string[]; input_schema: Symbols; output: Symbols; branches: Record<string, BranchContract>; run: RunSpec }
  | { kind: 'graph'; input: string[]; output: Symbols; branches: Record<string, BranchContract>; graph: NormalizedGraph }
  | { kind: 'workflow'; workflow_id: string; bindings: Bindings }
  | { kind: 'builtin'; op: string; bindings: Bindings };
export interface NormalizedNode { body: NodeBody; modifiers: Modifiers }
export type SourceEndpoint =
  | { kind: 'start' }
  | { kind: 'node'; node_id: string }
  | { kind: 'branch'; node_id: string; branch_id: string };
export type JoinTarget = { kind: 'node'; node_id: string } | { kind: 'end'; branch_id: string | null };
export type RouteEdge =
  | { kind: 'direct'; target: { kind: 'normal'; target: JoinTarget } | { kind: 'fail' } }
  | { kind: 'fork_join'; workers: string[]; join_target: JoinTarget };
export interface Route {
  source: SourceEndpoint;
  edge: RouteEdge;
  edge_index: number;
  raw: string;
  kill_vars: string[];
}
export interface NormalizedGraph {
  variables: Record<string, { typespec: TypeSpec; initial: { kind: 'unbound' } | { kind: 'default'; value: unknown } }>;
  nodes: Record<string, NormalizedNode>;
  routes: Record<string, Route>;
  max_steps: number;
}
export interface NormalizedRoot {
  input: Symbols;
  output: Symbols;
  branches: Record<string, BranchContract>;
  body: { kind: 'graph'; graph: NormalizedGraph } | {
    kind: 'run'; workflow_id: string; run: RunSpec; input_schema: Symbols; timeout_ms: number | null; retry: Retry | null;
  };
}
export interface WorkflowBundle { entry: string; workflows: Record<string, NormalizedRoot> }
export type NormalizedAst = NormalizedRoot;
export interface CompileOutput { diagnostics: Diagnostic[]; analysis: GraphAnalysis[]; bundle: WorkflowBundle | null }
export interface Scope { workflowId: string; scopeGraphPath: string[] }
