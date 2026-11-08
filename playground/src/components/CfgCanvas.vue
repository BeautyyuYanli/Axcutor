<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { Handle, Position, VueFlow, useVueFlow, type NodeMouseEvent } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import type { Frame } from '../client/types.gen';
import type { GraphAnalysis, Scope, VariableCandidate, WorkflowBundle } from '../types/compiler';
import { handleId, layoutGraph, liveOverlay } from '../utils/graphLayout';
import { JsonText } from '../utils/losslessJson';

const props = defineProps<{
  bundle: WorkflowBundle | null; scope: Scope; analysis: GraphAnalysis | null;
  analysisRaw: string | null; compilerRaw: JsonText | null; selectedNode: string | null;
  frames: Frame[];
  queryVariables: (raw: string, node: string, expected: string) => { candidate: VariableCandidate; typespecRaw: string }[];
}>();
const emit = defineEmits<{ select: [id: string]; navigate: [scope: Scope] }>();
const { fitView, onNodesInitialized } = useVueFlow('cfg');
const expected = ref('');
const topology = computed(() => layoutGraph(props.bundle, props.scope, props.analysis));
const overlay = computed(() => liveOverlay(props.bundle, props.frames, props.scope));
const nodes = computed(() => topology.value.nodes.map(node => ({
  ...node, selected: node.id === props.selectedNode,
  data: { ...node.data!, active: overlay.value.active.has(node.id), iterations: overlay.value.iterations.get(node.id) ?? [] },
})));
const edges = computed(() => topology.value.edges.map(edge => ({ ...edge, animated: overlay.value.cursors.has(edge.data!.cursor) })));
const facts = computed(() => props.selectedNode ? props.analysis?.nodes[props.selectedNode] : null);
const analysisText = computed(() => props.analysisRaw ? new JsonText(props.analysisRaw) : null);
const variables = computed(() => {
  try { return { rows: props.analysisRaw && props.selectedNode ? props.queryVariables(props.analysisRaw, props.selectedNode, expected.value) : [], error: '' }; }
  catch (error) { return { rows: [], error: String(error) }; }
});
const definiteVariables = computed(() => {
  const names = new Set([...(facts.value?.definite_in ?? []), ...Object.keys(facts.value?.injected ?? {})]);
  return [...names].map(name => ({
    name, injected: Object.hasOwn(facts.value?.injected ?? {}, name),
  }));
});
// Injected types live in the selected node's facts, not at analysis root.
function typeText(name: string, injected: boolean): string {
  return analysisText.value?.format(injected ? ['nodes', props.selectedNode!, 'injected', name] : ['symbols', name]) ?? '';
}
function nodePath(id: string): string[] {
  const path = ['bundle', 'workflows', props.scope.workflowId, 'body'];
  for (const part of props.scope.scopeGraphPath) path.push('graph', 'nodes', part, 'body');
  return path.concat('graph', 'nodes', id, 'modifiers');
}
function tags(id: string): string[] {
  const path = props.bundle?.workflows[props.scope.workflowId]?.body.kind === 'run' && !props.scope.scopeGraphPath.length
    ? ['bundle', 'workflows', props.scope.workflowId, 'body'] : nodePath(id);
  if (id === 'start' || id === 'end' || id === 'fail' || id.startsWith('end.')) return [];
  const labels: string[] = [];
  for (const field of ['for_each', 'retry', 'timeout_ms']) {
    const raw = props.compilerRaw?.raw([...path, field]);
    if (raw && raw !== 'null') labels.push(field === 'timeout_ms' ? `timeout_ms: ${raw}` : field);
  }
  return labels;
}
function openNode(id: string) {
  const node = topology.value.nodes.find(item => item.id === id)?.data?.node;
  if (node?.body.kind === 'workflow') emit('navigate', { workflowId: node.body.workflow_id, scopeGraphPath: [] });
  if (node?.body.kind === 'graph') emit('navigate', { ...props.scope, scopeGraphPath: [...props.scope.scopeGraphPath, id] });
}
function doubleClick(event: NodeMouseEvent) { openNode(event.node.id); }
onNodesInitialized(() => { void fitView({ padding: 0.2 }); });
watch(topology, async () => { await nextTick(); await fitView({ padding: 0.2 }); });
</script>

<template>
  <section aria-label="Control flow graph">
    <h2>CFG</h2>
    <nav aria-label="Scope navigation">
      <label>Workflow
        <select aria-label="Workflow" :value="scope.workflowId" @change="emit('navigate', { workflowId: ($event.target as HTMLSelectElement).value, scopeGraphPath: [] })">
          <option v-for="(_, id) in bundle?.workflows ?? {}" :key="id" :value="id">{{ id }}</option>
        </select>
      </label>
      <button type="button" @click="emit('navigate', { ...scope, scopeGraphPath: [] })">{{ scope.workflowId || 'No entry' }}</button>
      <button v-for="(part, index) in scope.scopeGraphPath" :key="index" type="button" @click="emit('navigate', { ...scope, scopeGraphPath: scope.scopeGraphPath.slice(0, index + 1) })">/ {{ part }}</button>
      <button type="button" @click="fitView({ padding: 0.2 })">Fit graph</button>
    </nav>
    <div class="canvas" aria-label="Graph canvas">
      <VueFlow id="cfg" :nodes="nodes" :edges="edges" :nodes-draggable="false" :nodes-connectable="false" :edges-updatable="false" :zoom-on-double-click="false" fit-view-on-init @node-click="emit('select', $event.node.id)" @node-double-click="doubleClick">
        <template #node-cfg="{ id, data }">
          <fieldset class="cfg-node" :data-node-kind="data.kind" :data-active="data.active" :data-selected="id === selectedNode">
            <legend><button type="button" @click.stop="emit('select', id)">{{ data.label }}</button></legend>
            <Handle v-if="data.kind !== 'start'" type="target" :position="Position.Top" />
            <div>{{ data.kind }} <span v-for="tag in tags(id)" :key="tag"> · {{ tag }}</span></div>
            <div v-if="data.iterations.length">running: {{ data.iterations.join(', ') }}</div>
            <button v-if="data.node?.body.kind === 'workflow' || data.node?.body.kind === 'graph'" type="button" @click.stop="openNode(id)">Open {{ data.node.body.kind }}</button>
            <div v-for="port in data.ports" :key="handleId(port.branch_id)" :data-port-kind="port.kind">{{ port.branch_id ?? 'default' }} ({{ port.kind }})</div>
            <Handle v-for="(port, index) in data.ports" :id="handleId(port.branch_id)" :key="handleId(port.branch_id)" type="source" :position="Position.Bottom" :data-port-kind="port.kind" :style="{ left: `${((index + 1) / (data.ports.length + 1)) * 100}%` }" />
          </fieldset>
        </template>
      </VueFlow>
    </div>
    <section aria-label="Static node probe">
      <h3>Static probe · {{ selectedNode ?? 'Select a node' }}</h3>
      <template v-if="facts">
        <p v-if="facts.definite_in === null">Entry assignment is not proved for this graph.</p>
        <p v-else>definite_in: {{ facts.definite_in.join(', ') || '(empty)' }}</p>
        <table>
          <thead><tr><th>Variable</th><th>TypeSpec</th><th>Origin</th></tr></thead>
          <tbody><tr v-for="variable in definiteVariables" :key="variable.name"><th>{{ variable.name }}</th><td><pre><code>{{ typeText(variable.name, variable.injected) }}</code></pre></td><td>{{ variable.injected ? 'injected' : 'definite_in' }}</td></tr></tbody>
        </table>
        <label>Expected type
          <select v-model="expected" aria-label="Expected type"><option value="">All</option><option>integer</option><option>number</option><option>string</option><option>boolean</option></select>
        </label>
        <p v-if="variables.error" role="alert">{{ variables.error }}</p>
        <table>
          <caption>Available variables and field projections</caption>
          <thead><tr><th>Path</th><th>TypeSpec</th></tr></thead>
          <tbody><tr v-for="item in variables.rows" :key="[item.candidate.path.root, ...item.candidate.path.fields].join('.')"><th>{{ [item.candidate.path.root, ...item.candidate.path.fields].join('.') }}</th><td><pre><code>{{ item.typespecRaw }}</code></pre></td></tr></tbody>
        </table>
        <p v-if="facts.ports === null">Port contracts are unavailable.</p>
        <table v-else>
          <caption>Derived ports</caption>
          <thead><tr><th>Port</th><th>Kind</th><th>Output</th><th>Kill variables</th></tr></thead>
          <tbody><tr v-for="port in facts.ports" :key="handleId(port.branch_id)" :data-port-kind="port.kind"><th>{{ port.branch_id ?? 'default' }}</th><td>{{ port.kind }}</td><td>{{ port.output.join(', ') }}</td><td>{{ port.kill_vars.join(', ') }}</td></tr></tbody>
        </table>
      </template>
      <p v-else-if="selectedNode">No graph analysis for this node (root run and terminal nodes have no node facts).</p>
    </section>
  </section>
</template>
