<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import SourceWorkspace from './components/SourceWorkspace.vue';
import CfgCanvas from './components/CfgCanvas.vue';
import DiagnosticsPanel from './components/DiagnosticsPanel.vue';
import ExecutionInspector from './components/ExecutionInspector.vue';
import { useCompiler } from './composables/useCompiler';
import { useExecution } from './composables/useExecution';
import { defaultPreset } from './presets';
import type { CompileOutput, Diagnostic, Scope, SourceFormat } from './types/compiler';
import type { Frame } from './client/types.gen';
import type { JsonText } from './utils/losslessJson';
import { locateFrame, sameScope } from './utils/graphLayout';

const initial = defaultPreset();
const packages = ref(initial.packages);
const entry = ref(initial.entry);
const inputRawText = ref(initial.inputRawText);
const selectedFile = ref(entry.value);
const scope = ref<Scope>({ workflowId: entry.value, scopeGraphPath: [] });
const selectedNode = ref<string | null>(null);
const selectedFrameKey = ref<string | null>(null);
const canvasSource = ref<'editor' | 'execution'>('editor');
const submitted = shallowRef<{ output: CompileOutput; raw: JsonText } | null>(null);
const { ready, error: compilerError, compileOutput, rawOutput, availableVariables } = useCompiler(entry, packages);
const { execution, slices, error: httpError, submitting, cancelRequested, run, cancel } = useExecution();
const current = computed(() => canvasSource.value === 'execution' ? submitted.value?.output ?? null : compileOutput.value);
const currentRaw = computed(() => canvasSource.value === 'execution' ? submitted.value?.raw ?? null : rawOutput.value);
const bundle = computed(() => current.value?.bundle ?? null);
const analysisIndex = computed(() => current.value?.analysis.findIndex(item => sameScope({
  workflowId: item.workflow_id, scopeGraphPath: item.scope_graph_path,
}, scope.value)) ?? -1);
const analysis = computed(() => current.value?.analysis[analysisIndex.value] ?? null);
const analysisRaw = computed(() => currentRaw.value?.raw(['analysis', analysisIndex.value]) ?? null);
const diagnostics = computed(() => [...(compileOutput.value?.diagnostics ?? []), ...(httpError.value?.diagnostics ?? [])]);
const frames = computed(() => canvasSource.value === 'execution' && execution.value?.status === 'running' ? execution.value.frames : []);

function navigate(target: Scope) { scope.value = target; selectedNode.value = null; }
function selectFile(path: string) {
  selectedFile.value = path;
  canvasSource.value = 'editor';
  navigate({ workflowId: path, scopeGraphPath: [] });
}
function reset() {
  const preset = defaultPreset();
  packages.value = preset.packages;
  entry.value = preset.entry;
  inputRawText.value = preset.inputRawText;
  selectFile(entry.value);
}
function addFile(path: string, format: SourceFormat) {
  packages.value = { ...packages.value, [path]: { format, text: '' } };
  if (!entry.value) entry.value = path;
  selectFile(path);
}
function removeFile(path: string) {
  const remaining = { ...packages.value };
  delete remaining[path];
  packages.value = remaining;
  const first = Object.keys(remaining)[0] ?? '';
  if (entry.value === path) entry.value = first;
  selectFile(first);
}
function editText(path: string, text: string) { packages.value[path].text = text; canvasSource.value = 'editor'; }
function editFormat(path: string, format: SourceFormat) { packages.value[path].format = format; canvasSource.value = 'editor'; }
function locateDiagnostic(diagnostic: Diagnostic) {
  selectedFile.value = diagnostic.workflow_id;
  canvasSource.value = 'editor';
  scope.value = { workflowId: diagnostic.workflow_id, scopeGraphPath: [...diagnostic.scope_graph_path] };
  selectedNode.value = diagnostic.node_id;
}
function selectFrame(frame: Frame, key: string) {
  if (!submitted.value?.output.bundle) return;
  const target = locateFrame(submitted.value.output.bundle, frame);
  if (!target) return;
  canvasSource.value = 'execution';
  scope.value = { workflowId: target.workflowId, scopeGraphPath: target.scopeGraphPath };
  selectedNode.value = target.nodeId ?? null;
  selectedFrameKey.value = key;
}
function selectNode(id: string) {
  selectedNode.value = id;
  if (canvasSource.value !== 'execution' || !submitted.value?.output.bundle) return;
  const snapshot = submitted.value.output.bundle;
  const find = (items: Frame[]): Frame | undefined => {
    for (const frame of items) {
      const target = locateFrame(snapshot, frame);
      if (target && sameScope(target, scope.value) && target.nodeId === id) return frame;
      const child = find(frame.children);
      if (child) return child;
    }
  };
  const frame = find(execution.value?.frames ?? []);
  selectedFrameKey.value = frame ? `${frame.kind}:${frame.address}` : null;
}
async function submit() {
  if (!compileOutput.value?.bundle || !rawOutput.value) return;
  submitted.value = { output: compileOutput.value, raw: rawOutput.value };
  canvasSource.value = 'execution';
  navigate({ workflowId: entry.value, scopeGraphPath: [] });
  selectedFrameKey.value = null;
  await run(entry.value, packages.value, inputRawText.value);
}
function changeCanvasSource() {
  const id = canvasSource.value === 'execution' ? submitted.value?.output.bundle?.entry : entry.value;
  navigate({ workflowId: id ?? entry.value, scopeGraphPath: [] });
}
watch(entry, value => { selectFile(value); });
</script>

<template>
  <main>
    <header>
      <h1>Axcutor Playground</h1>
      <p role="status">Compiler: {{ ready ? 'ready' : 'loading' }}</p>
      <button type="button" :disabled="!ready || !compileOutput?.bundle || submitting || execution?.status === 'running'" @click="submit">{{ submitting ? 'Submitting…' : 'Run' }}</button>
      <button type="button" :disabled="execution?.status !== 'running' || cancelRequested" @click="cancel">{{ cancelRequested ? 'Cancelling…' : 'Cancel' }}</button>
      <label>Canvas source
        <select v-model="canvasSource" aria-label="Canvas source" @change="changeCanvasSource"><option value="editor">Editor compilation</option><option value="execution" :disabled="!submitted">Submitted snapshot</option></select>
      </label>
    </header>
    <div class="workbench">
      <SourceWorkspace :packages="packages" :entry="entry" :selected-file="selectedFile" :input-raw-text="inputRawText" :diagnostics="diagnostics" :compiler-raw="rawOutput" @file="selectFile" @entry="entry = $event" @input="inputRawText = $event" @text="editText" @format="editFormat" @add="addFile" @remove="removeFile" @reset="reset" />
      <CfgCanvas :bundle="bundle" :scope="scope" :analysis="analysis" :analysis-raw="analysisRaw" :compiler-raw="currentRaw" :selected-node="selectedNode" :frames="frames" :query-variables="availableVariables" @select="selectNode" @navigate="navigate" />
      <ExecutionInspector :execution="execution" :slices="slices" :selected-key="selectedFrameKey" @select="selectFrame" />
    </div>
    <DiagnosticsPanel :diagnostics="diagnostics" :compiler-error="compilerError" :http-error="httpError" @locate="locateDiagnostic" />
  </main>
</template>
