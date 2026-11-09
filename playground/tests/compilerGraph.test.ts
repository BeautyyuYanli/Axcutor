import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import init, { compile_workflow, get_available_vars_at_node } from '../src/wasm-pkg/axcutor_wasm';
import { defaultPreset } from './preset';
import { handleId, layoutGraph, liveOverlay, locateFrame, scopeBody } from '../src/utils/graphLayout';
import { JsonText } from '../src/utils/losslessJson';
import type { CompileOutput, Scope } from '../src/types/compiler';
import type { Frame } from '../src/client/types.gen';

beforeAll(async () => { await init({ module_or_path: readFileSync(new URL('../src/wasm-pkg/axcutor_wasm_bg.wasm', import.meta.url)) }); });

function compile(document: object, entry = 'folder.demo.main') {
  const raw = compile_workflow(JSON.stringify({ entry, packages: { [entry]: { format: 'json', text: JSON.stringify(document) } } }));
  const result = JSON.parse(raw) as CompileOutput;
  expect(result.diagnostics).toEqual([]);
  return { result, raw: new JsonText(raw) };
}
const run = { run: { executor: 'builtin', content: 'return Output()' } };
const frame = (address: string, kind: Frame['kind'] = 'function', cursor = 'start', children: Frame[] = []): Frame => ({
  address, kind, cursor, children, waiting: [], call_depth: 1, steps: 0, max_steps: null, variables: {},
});

describe('real WASM serialization and CFG', () => {
  it('compiles the repository preset, expands Root Run and resolves same-address iteration/function frames', () => {
    const preset = defaultPreset();
    const output = JSON.parse(compile_workflow(JSON.stringify({ entry: preset.entry, packages: preset.packages }))) as CompileOutput;
    expect(output.diagnostics).toEqual([]);
    const bundle = output.bundle!;
    const scope: Scope = { workflowId: 'demo.main', scopeGraphPath: [] };
    const graph = layoutGraph(bundle, scope, output.analysis[0]);
    expect(graph.nodes.map(node => node.id)).toEqual(['start', 'double', 'summarize', 'measure_spread', 'end']);
    expect(graph.edges).toHaveLength(5);
    expect(graph.nodes.every(node => Number.isFinite(node.position.x) && Number.isFinite(node.position.y))).toBe(true);
    const byId = Object.fromEntries(graph.nodes.map(node => [node.id, node]));
    expect(byId.start.position.x + byId.start.width).toBeLessThan(byId.double.position.x);
    expect(byId.double.position.x + byId.double.width).toBeLessThan(byId.summarize.position.x);
    expect(byId.summarize.position.x).toBe(byId.measure_spread.position.x);
    expect(byId.summarize.position.y).not.toBe(byId.measure_spread.position.y);
    expect(byId.summarize.position.x + byId.summarize.width).toBeLessThan(byId.end.position.x);
    expect(byId.start.height).toBeLessThan(byId.double.height);
    expect(layoutGraph(bundle, { workflowId: 'demo.double', scopeGraphPath: [] }, null).nodes.map(node => node.id)).toEqual(['start', 'run', 'end']);
    expect(locateFrame(bundle, frame('demo.main/double[0]', 'iteration', 'double'))).toEqual({ ...scope, nodeId: 'double', iteration: '[0]' });
    expect(locateFrame(bundle, frame('demo.main/double[0]', 'function', 'run'))).toEqual({ workflowId: 'demo.double', scopeGraphPath: [], nodeId: 'run' });
    const frames = [frame('demo.main', 'function', 'double', [frame('demo.main/double[0]', 'iteration', 'double'), frame('demo.main/double[1]', 'iteration', 'double')])];
    const overlay = liveOverlay(bundle, frames, scope);
    expect(overlay.active.has('double')).toBe(true);
    expect(overlay.iterations.get('double')).toEqual(['[0]', '[1]']);
  });

  it('maps real fork/join routes and named end/fail/catch targets without handle collisions', () => {
    const { result } = compile({ input: {}, output: {}, branches: { done: 'done' }, graph: {
      nodes: { a: run, b: run, join: { ...run, catch: { default: '*' } } },
      edges: ['start -> [a, b] -> join', 'join -> end.done', 'join.default -> fail'],
    } });
    const graph = layoutGraph(result.bundle, { workflowId: 'folder.demo.main', scopeGraphPath: [] }, result.analysis[0]);
    expect(graph.edges).toHaveLength(6);
    expect(graph.nodes.map(node => node.id)).toContain('end.done');
    expect(graph.nodes.map(node => node.id)).toContain('fail');
    expect(graph.edges.find(edge => edge.target === 'fail')?.sourceHandle).toBe('default');
    const byId = Object.fromEntries(graph.nodes.map(node => [node.id, node]));
    expect(byId.a.position.x).toBe(byId.b.position.x);
    expect(byId.a.position.y).not.toBe(byId.b.position.y);
    expect(byId.join.position.x).toBeGreaterThan(byId.a.position.x + byId.a.width);
    expect(handleId(null)).not.toBe(handleId('default'));
  });

  it('navigates inline scopes under entries containing slashes and overlays named branch routes', () => {
    const { result } = compile({ input: {}, output: {}, graph: {
      nodes: { block: { graph: { nodes: { work: { ...run, branches: { ok: 'done' } } }, edges: ['start -> work', 'work.ok -> end'] } } },
      edges: ['start -> block', 'block -> end'],
    } });
    const scope = { workflowId: 'folder.demo.main', scopeGraphPath: ['block'] };
    expect(scopeBody(result.bundle, scope)?.kind).toBe('graph');
    expect(locateFrame(result.bundle!, frame('folder.demo.main/block', 'block', 'work.ok'))).toEqual({ ...scope, nodeId: 'work' });
    const overlay = liveOverlay(result.bundle, [frame('folder.demo.main/block', 'block', 'work.ok')], scope);
    expect(overlay.cursors.has('work.ok')).toBe(true);
    expect(locateFrame(result.bundle!, frame('another/demo.main/block', 'block'))).toBeNull();
  });

  it('keeps exact enum tokens in the WASM analysis query and proves field projections/subtyping', () => {
    const source = '{"input":{"n":{"type":"integer","enum":[99999999999999999999999999999999999]},"user":{"type":"object","properties":{"age":{"type":"integer"},"name":{"type":"string"}},"required":["age"]}},"output":{},"graph":{"nodes":{"work":{"run":{"executor":"builtin","content":"return Output()"}}},"edges":["start -> work","work -> end"]}}';
    const output = new JsonText(compile_workflow(JSON.stringify({ entry: 'main', packages: { main: { format: 'json', text: source } } })));
    expect(JSON.parse(output.raw(['diagnostics'])!)).toEqual([]);
    const analysisRaw = output.raw(['analysis', 0])!;
    const integer = get_available_vars_at_node(analysisRaw, 'work', '{"type":"integer"}');
    expect(integer).toContain('99999999999999999999999999999999999');
    expect(JSON.parse(integer).map((candidate: { path: { root: string; fields: string[] } }) => [candidate.path.root, ...candidate.path.fields].join('.'))).toEqual(['n', 'user.age']);
    const number = JSON.parse(get_available_vars_at_node(analysisRaw, 'work', '{"type":"number"}'));
    expect(number).toHaveLength(2);
    const all = get_available_vars_at_node(analysisRaw, 'work');
    expect(all).toContain('"type":["string","null"]');
  });
});
