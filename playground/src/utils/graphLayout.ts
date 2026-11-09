/** Convert only checked Rust topology. Runtime addresses are resolved against
 * the submitted bundle, starting from its dotted package or anonymous entry.
 * Iteration frames stay in the caller scope; a function/block at the same
 * address then enters the node body. Never guess scope from call_depth.
 */
import dagre from '@dagrejs/dagre';
import type { Edge, Node } from '@vue-flow/core';
import type { Frame } from '../client/types.gen';
import type { GraphAnalysis, NodePort, NormalizedGraph, NormalizedNode, NormalizedRoot, Scope, WorkflowBundle } from '../types/compiler';

export interface CanvasNodeData extends Record<string, unknown> {
  label: string;
  kind: string;
  node?: NormalizedNode;
  ports: NodePort[];
  active: boolean;
  iterations: string[];
}
export interface CanvasEdgeData extends Record<string, unknown> { cursor: string }
export type CanvasNode = Node<CanvasNodeData>;
export type CanvasEdge = Edge<CanvasEdgeData>;
export interface FrameLocation extends Scope { nodeId?: string; iteration?: string }

export function sameScope(a: Scope, b: Scope): boolean {
  return a.workflowId === b.workflowId && a.scopeGraphPath.join('/') === b.scopeGraphPath.join('/');
}

export function scopeBody(bundle: WorkflowBundle | null, scope: Scope): NormalizedRoot['body'] | null {
  let body = bundle?.workflows[scope.workflowId]?.body;
  for (const id of scope.scopeGraphPath) {
    if (body?.kind !== 'graph') return null;
    const child: NormalizedNode | undefined = body.graph.nodes[id];
    if (child?.body.kind !== 'graph') return null;
    body = child.body;
  }
  return body ?? null;
}

// '@' cannot occur in a DSL branch identifier, unlike the word 'default'.
export function handleId(branch: string | null): string { return branch ?? '@default'; }

export function layoutGraph(bundle: WorkflowBundle | null, scope: Scope, analysis: GraphAnalysis | null): {
  nodes: CanvasNode[]; edges: CanvasEdge[];
} {
  const nodes: CanvasNode[] = [];
  const edges: CanvasEdge[] = [];
  const add = (id: string, kind: string, ports: NodePort[], node?: NormalizedNode) => {
    if (nodes.some(existing => existing.id === id)) return;
    nodes.push({
      id, type: 'cfg', position: { x: 0, y: 0 }, width: 240, height: 150,
      data: { label: id, kind, node, ports, active: false, iterations: [] },
    });
  };
  const defaultPort: NodePort = { branch_id: null, kind: 'normal', output: [], kill_vars: [] };
  const body = scopeBody(bundle, scope);
  if (!body) {
    // Failed compilation can still publish independent, trustworthy node facts.
    for (const [id, facts] of Object.entries(analysis?.nodes ?? {})) add(id, 'unavailable', facts.ports ?? []);
  } else if (body.kind === 'run') {
    const root = bundle!.workflows[scope.workflowId];
    const ports = Object.keys(root.branches).length
      ? Object.entries(root.branches).map(([branch_id, contract]) => ({ ...defaultPort, branch_id, output: contract.output }))
      : [{ ...defaultPort, output: Object.keys(root.output) }];
    add('start', 'start', [defaultPort]);
    add('run', 'run', ports);
    add('end', 'end', []);
    edges.push({ id: 'root-start', source: 'start', sourceHandle: handleId(null), target: 'run', data: { cursor: 'start' } });
    for (const port of ports) edges.push({
      id: `root-end-${port.branch_id ?? ''}`, source: 'run', sourceHandle: handleId(port.branch_id), target: 'end',
      label: port.branch_id ?? undefined, data: { cursor: port.branch_id ? `run.${port.branch_id}` : 'run' },
    });
  } else {
    add('start', 'start', [defaultPort]);
    for (const [id, node] of Object.entries(body.graph.nodes)) add(id, node.body.kind, analysis?.nodes[id]?.ports ?? [], node);
    const terminal = (target: { kind: 'node'; node_id: string } | { kind: 'end'; branch_id: string | null }) => {
      const id = target.kind === 'node' ? target.node_id : target.branch_id ? `end.${target.branch_id}` : 'end';
      if (target.kind === 'end') add(id, 'end', []);
      return id;
    };
    for (const [cursor, route] of Object.entries(body.graph.routes)) {
      const source = route.source.kind === 'start' ? 'start' : route.source.node_id;
      const branch = route.source.kind === 'branch' ? route.source.branch_id : null;
      const edge = (suffix: string, from: string, target: string, sourceHandle?: string) => edges.push({
        id: `route-${route.edge_index}-${suffix}`, source: from, target, sourceHandle,
        label: from === source ? branch ?? undefined : undefined, data: { cursor },
      });
      if (route.edge.kind === 'direct') {
        let target: string;
        if (route.edge.target.kind === 'fail') { add('fail', 'fail', []); target = 'fail'; }
        else target = terminal(route.edge.target.target);
        edge('direct', source, target, handleId(branch));
      } else {
        const join = terminal(route.edge.join_target);
        route.edge.workers.forEach((worker, i) => {
          edge(`fork-${i}`, source, worker, handleId(branch));
          // SESE workers return through their default normal port.
          edge(`join-${i}`, worker, join, handleId(null));
        });
      }
    }
  }
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({ rankdir: 'TB', nodesep: 50, ranksep: 80 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) graph.setNode(node.id, { width: 240, height: 150 });
  for (const edge of edges) graph.setEdge(edge.source, edge.target, {}, edge.id);
  dagre.layout(graph);
  for (const node of nodes) {
    const position = graph.node(node.id);
    node.position = { x: position.x - 120, y: position.y - 75 };
  }
  return { nodes, edges };
}

function graphAt(bundle: WorkflowBundle, scope: Scope): NormalizedGraph | null {
  const body = scopeBody(bundle, scope);
  return body?.kind === 'graph' ? body.graph : null;
}

/** Function and iteration may share an address. The kind determines whether the
 * final call segment has been entered; nested workflow calls reset inline scope.
 */
export function locateFrame(bundle: WorkflowBundle, frame: Frame): FrameLocation | null {
  const entry = bundle.entry;
  if (frame.address !== entry && !frame.address.startsWith(`${entry}/`)) return null;
  let scope: Scope = { workflowId: entry, scopeGraphPath: [] };
  const suffix = frame.address.slice(entry.length).replace(/^\//, '');
  const segments = suffix ? suffix.split('/') : [];
  for (let i = 0; i < segments.length; i++) {
    const match = /^([A-Za-z_][A-Za-z0-9_]*)(\[\d+\])?$/.exec(segments[i]);
    if (!match) return null;
    const [, nodeId, iteration] = match;
    const node = graphAt(bundle, scope)?.nodes[nodeId];
    if (!node) return null;
    if (i === segments.length - 1 && frame.kind === 'iteration') return { ...scope, nodeId, iteration };
    if (node.body.kind === 'workflow') scope = { workflowId: node.body.workflow_id, scopeGraphPath: [] };
    else if (node.body.kind === 'graph') scope = { ...scope, scopeGraphPath: [...scope.scopeGraphPath, nodeId] };
    else return null;
  }
  return { ...scope, nodeId: frame.cursor.split('.')[0] };
}

export function liveOverlay(bundle: WorkflowBundle | null, frames: Frame[], scope: Scope): {
  active: Set<string>; cursors: Set<string>; iterations: Map<string, string[]>;
} {
  const active = new Set<string>();
  const cursors = new Set<string>();
  const iterations = new Map<string, string[]>();
  if (!bundle) return { active, cursors, iterations };
  const visit = (frame: Frame) => {
    const location = locateFrame(bundle, frame);
    if (location && sameScope(location, scope)) {
      if (location.nodeId) active.add(location.nodeId);
      cursors.add(frame.cursor);
      if (frame.kind === 'iteration' && location.nodeId && location.iteration) {
        const values = iterations.get(location.nodeId) ?? [];
        values.push(location.iteration);
        iterations.set(location.nodeId, values);
      }
    }
    frame.children.forEach(visit);
  };
  frames.forEach(visit);
  return { active, cursors, iterations };
}
