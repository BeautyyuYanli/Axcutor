<script setup lang="ts">
import { computed, nextTick, watch } from 'vue';
import { Handle, Position, VueFlow, useVueFlow, type NodeMouseEvent } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import type { Frame } from '../client/types.gen';
import type { GraphAnalysis, Scope, WorkflowBundle } from '../types/compiler';
import { handleId, layoutGraph, liveOverlay, scopeBody } from '../utils/graphLayout';

const props = defineProps<{
  bundle: WorkflowBundle | null; scope: Scope; analysis: GraphAnalysis | null;
  selectedNode: string | null;
  frames: Frame[];
}>();
const emit = defineEmits<{ select: [id: string]; navigate: [scope: Scope] }>();
const { fitView, zoomIn, zoomOut, onNodesInitialized, dimensions } = useVueFlow('cfg');
const fitGraph = () => fitView({ padding: 0.2, minZoom: 0.1, maxZoom: 1 });
const topology = computed(() => layoutGraph(props.bundle, props.scope, props.analysis));
const overlay = computed(() => liveOverlay(props.bundle, props.frames, props.scope));
const nodes = computed(() => topology.value.nodes.map(node => ({
  ...node, selected: node.id === props.selectedNode,
  data: { ...node.data!, active: overlay.value.active.has(node.id), iterations: overlay.value.iterations.get(node.id) ?? [] },
})));
const edges = computed(() => topology.value.edges.map(edge => ({ ...edge, animated: overlay.value.cursors.has(edge.data!.cursor) })));
function tags(id: string): string[] {
  if (id === 'start' || id === 'end' || id === 'fail' || id.startsWith('end.')) return [];
  const body = scopeBody(props.bundle, props.scope);
  const modifiers = body?.kind === 'run'
    ? { ...body, for_each: null } : topology.value.nodes.find(node => node.id === id)?.data?.node?.modifiers;
  if (!modifiers) return [];
  const labels: string[] = [];
  if (modifiers.for_each) labels.push('Parallel items');
  if (modifiers.retry) labels.push('Retries enabled');
  if (modifiers.timeout_ms !== null) labels.push('Timeout configured');
  return labels;
}
function openNode(id: string) {
  const node = topology.value.nodes.find(item => item.id === id)?.data?.node;
  if (node?.body.kind === 'workflow') emit('navigate', { workflowId: node.body.workflow_id, scopeGraphPath: [] });
  if (node?.body.kind === 'graph') emit('navigate', { ...props.scope, scopeGraphPath: [...props.scope.scopeGraphPath, id] });
}
function doubleClick(event: NodeMouseEvent) { openNode(event.node.id); }
onNodesInitialized(() => { void fitGraph(); });
watch(topology, async () => { await nextTick(); await fitGraph(); });
// Vue Flow observes its pane's size, including layout toggles. Fit only when
// dimensions change so ordinary polls and manual zoom keep their viewport.
watch(() => `${dimensions.value.width}:${dimensions.value.height}`, async () => { await nextTick(); await fitGraph(); });
</script>

<template>
  <section class="cfg-panel" aria-label="Control flow graph">
    <div class="cfg-toolbar">
      <h2>CFG</h2>
      <label>Workflow
        <select aria-label="Workflow" :title="scope.workflowId" :value="scope.workflowId" @change="emit('navigate', { workflowId: ($event.target as HTMLSelectElement).value, scopeGraphPath: [] })">
          <option v-for="(_, id) in bundle?.workflows ?? {}" :key="id" :value="id">{{ id }}</option>
        </select>
      </label>
      <div class="graph-controls" role="group" aria-label="Graph view">
        <button type="button" aria-label="Zoom out" title="Zoom out" @click="zoomOut()">−</button>
        <button type="button" aria-label="Zoom in" title="Zoom in" @click="zoomIn()">+</button>
        <button type="button" @click="fitGraph">Fit graph</button>
      </div>
    </div>
    <nav class="scope-navigation" aria-label="Scope navigation">
      <button type="button" @click="emit('navigate', { ...scope, scopeGraphPath: [] })">{{ scope.workflowId || 'No entry' }}</button>
      <button v-for="(part, index) in scope.scopeGraphPath" :key="index" type="button" @click="emit('navigate', { ...scope, scopeGraphPath: scope.scopeGraphPath.slice(0, index + 1) })">/ {{ part }}</button>
    </nav>
    <div class="canvas" aria-label="Graph canvas">
      <VueFlow id="cfg" :nodes="nodes" :edges="edges" :nodes-draggable="false" :nodes-connectable="false" :edges-updatable="false" :zoom-on-double-click="false" :min-zoom="0.1" fit-view-on-init @node-click="emit('select', $event.node.id)" @node-double-click="doubleClick">
        <template #node-cfg="{ id, data }">
          <fieldset class="cfg-node" :data-node-kind="data.kind" :data-active="data.active" :data-selected="id === selectedNode">
            <legend><button type="button" :aria-pressed="id === selectedNode" @click.stop="emit('select', id)">{{ data.label }}</button></legend>
            <Handle v-if="data.kind !== 'start'" type="target" :position="Position.Top" />
            <div v-if="data.active || id === selectedNode" class="node-state">{{ [id === selectedNode ? 'Selected' : '', data.active ? 'Running' : ''].filter(Boolean).join(' · ') }}</div>
            <div v-for="tag in tags(id)" :key="tag">{{ tag }}</div>
            <div v-if="data.iterations.length">Running items: {{ data.iterations.join(', ') }}</div>
            <button v-if="data.node?.body.kind === 'workflow' || data.node?.body.kind === 'graph'" type="button" @click.stop="openNode(id)">Open {{ data.node.body.kind }}</button>
            <template v-for="port in data.ports" :key="handleId(port.branch_id)">
              <div v-if="port.branch_id !== null" :data-port-kind="port.kind">{{ port.branch_id }}{{ port.kind === 'catch' ? ' · on error' : '' }}</div>
            </template>
            <Handle v-for="(port, index) in data.ports" :id="handleId(port.branch_id)" :key="handleId(port.branch_id)" type="source" :position="Position.Bottom" :data-port-kind="port.kind" :style="{ left: `${((index + 1) / (data.ports.length + 1)) * 100}%` }" />
          </fieldset>
        </template>
      </VueFlow>
    </div>
  </section>
</template>
