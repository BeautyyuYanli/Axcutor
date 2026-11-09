<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue';
import SourceWorkspace from './components/SourceWorkspace.vue';
import CfgCanvas from './components/CfgCanvas.vue';
import DiagnosticsPanel from './components/DiagnosticsPanel.vue';
import ExecutionInspector from './components/ExecutionInspector.vue';
import { compileSource, useCompiler } from './composables/useCompiler';
import { useExecution } from './composables/useExecution';
import { useWorkspace } from './composables/useWorkspace';
import { workspaceSource } from './utils/workspace';
import type { CompileOutput, Diagnostic, Scope } from './types/compiler';
import type { Frame } from './client/types.gen';
import type { JsonText } from './utils/losslessJson';
import { locateFrame, sameScope } from './utils/graphLayout';

const workspace = useWorkspace();
const { files, busy, serverBusy, dirty, loaded, problem: workspaceProblem } = workspace;
const entryPath = ref('workflows/demo/main/__axcutor__.yaml');
const inputPath = ref('inputs/demo.json');
const selectedFile = ref(entryPath.value);
const preview = computed(() => {
  try { return { source: workspaceSource(files.value, entryPath.value), error: null }; }
  catch (cause) { return { source: null, error: String(cause) }; }
});
const packages = computed(() => preview.value.source?.packages ?? {});
const entry = computed(() => preview.value.source?.entry ?? '__main__');
const scope = ref<Scope>({ workflowId: entry.value, scopeGraphPath: [] });
const selectedNode = ref<string | null>(null);
const selectedFrameKey = ref<string | null>(null);
const canvasSource = ref<'editor' | 'execution'>('editor');
const submitted = shallowRef<{ output: CompileOutput; raw: JsonText } | null>(null);
const { ready, error: compilerError, compileOutput, rawOutput, availableVariables } = useCompiler(entry, packages);
const { execution, observedId, slices, source: submittedSource, workspace: runWorkspace, error: httpError, submitting, cancelRequested, run, cancel } = useExecution();
const preparing = ref(false);
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
const writeBlocked = computed(() => serverBusy.value || preparing.value || submitting.value || observedId.value !== null);
const readOnly = computed(() => busy.value || writeBlocked.value || !loaded.value);
const workflowFiles = computed(() => files.value.filter(file => /\.(yaml|yml|json)$/.test(file.path)).sort((a, b) => a.path.localeCompare(b.path)));
const inputFiles = computed(() => workflowFiles.value.filter(file => file.path.endsWith('.json')));
const activePanel = ref<'execution' | 'diagnostics'>('execution');
const panelOpen = ref(false);
const diagnosticCount = computed(() => diagnostics.value.length + Number(!!(preview.value.error ?? compilerError.value)) + Number(!!workspaceProblem.value) + Number(!!httpError.value));
function showPanel(panel: 'execution' | 'diagnostics') { activePanel.value = panel; panelOpen.value = true; }
// Initialization errors must be visible before ready; other editor problems
// wait for loading. Execution polling must not change the selected panel.
watch(() => !compilerError.value && (!loaded.value || !ready.value) ? '' : JSON.stringify([
  preview.value.error, compilerError.value, compileOutput.value?.diagnostics ?? [],
]), (value, previous) => {
  if (value !== previous && (preview.value.error || compilerError.value || compileOutput.value?.diagnostics.length)) showPanel('diagnostics');
});
watch(workspaceProblem, value => { if (value) showPanel('diagnostics'); });

function navigate(target: Scope) { scope.value = target; selectedNode.value = null; }
function selectFile(path: string) {
  selectedFile.value = path;
  canvasSource.value = 'editor';
  const name = Object.keys(preview.value.source?.paths ?? {}).find(name => preview.value.source?.paths[name] === path);
  if (name) navigate({ workflowId: name, scopeGraphPath: [] });
}
function addFile(path: string) {
  if (workspace.add(path)) selectFile(path);
}
function removeFile(path: string) {
  workspace.remove(path);
  selectFile(files.value[0]?.path ?? '');
}
function editText(path: string, text: string) {
  const file = files.value.find(file => file.path === path);
  if (file && !file.generated) file.text = text;
  canvasSource.value = 'editor';
}
function locateDiagnostic(diagnostic: Diagnostic) {
  selectedFile.value = preview.value.source?.paths[diagnostic.workflow_id] ?? selectedFile.value;
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
  if (!compileOutput.value?.bundle || preparing.value) return;
  showPanel('execution');
  preparing.value = true;
  const path = entryPath.value;
  const input = inputPath.value || null;
  try {
    const revision = await workspace.save();
    if (!revision) { showPanel('diagnostics'); return; }
    // A new submission may return only an ID; its frames cannot use an older bundle.
    submitted.value = null;
    selectedFrameKey.value = null;
    canvasSource.value = 'editor';
    navigate({ workflowId: entry.value, scopeGraphPath: [] });
    const accepted = await run(path, input, revision);
    // An uncertain submission with an ID still belongs in the execution view.
    if (!accepted && observedId.value === null) showPanel('diagnostics');
    if (runWorkspace.value) workspace.accept(runWorkspace.value);
    else await workspace.observe();
    if (accepted && submittedSource.value) {
      submitted.value = compileSource(submittedSource.value);
      canvasSource.value = 'execution';
      navigate({ workflowId: submittedSource.value.entry, scopeGraphPath: [] });
      selectedFrameKey.value = null;
    }
  } finally { preparing.value = false; }
}
function changeCanvasSource() {
  const id = canvasSource.value === 'execution' ? submitted.value?.output.bundle?.entry : entry.value;
  navigate({ workflowId: id ?? entry.value, scopeGraphPath: [] });
}
watch(entryPath, value => { selectFile(value); });
watch(entry, value => { navigate({ workflowId: value, scopeGraphPath: [] }); });
onMounted(async () => {
  await workspace.load();
  if (!files.value.some(file => file.path === entryPath.value)) entryPath.value = files.value.find(file => /\.(yaml|yml|json)$/.test(file.path))?.path ?? '';
  if (!files.value.some(file => file.path === inputPath.value)) inputPath.value = '';
});
</script>

<template>
  <main class="app-shell">
    <header class="app-toolbar">
      <h1>Axcutor Playground</h1>
      <p role="status" data-workspace-state>{{ !loaded ? 'Loading workspace…' : dirty ? 'Unsaved changes' : 'Saved' }}</p>
      <div class="toolbar-actions" role="group" aria-label="Workspace actions">
        <button type="button" :disabled="readOnly" @click="workspace.save">Save files</button>
        <button type="button" :disabled="busy" @click="workspace.load">Reload (discard edits)</button>
        <button class="primary-action" type="button" :disabled="readOnly || !ready || !preview.source || !compileOutput?.bundle" @click="submit">{{ preparing || submitting ? 'Submitting…' : 'Run' }}</button>
        <button type="button" :disabled="observedId === null || cancelRequested" @click="cancel">{{ cancelRequested ? 'Cancelling…' : 'Cancel' }}</button>
      </div>
    </header>
    <section class="run-config" aria-label="Run configuration">
      <label>Entry
        <select v-model="entryPath" aria-label="Entry" :title="entryPath">
          <option v-for="file in workflowFiles" :key="file.path" :value="file.path">{{ file.path }}</option>
        </select>
      </label>
      <label>Input file
        <select v-model="inputPath" aria-label="Input file" :title="inputPath || 'Empty input {}'">
          <option value="">Empty input {}</option>
          <option v-for="file in inputFiles" :key="file.path" :value="file.path">{{ file.path }}</option>
        </select>
      </label>
      <label>Canvas source
        <select v-model="canvasSource" aria-label="Canvas source" @change="changeCanvasSource"><option value="editor">Editor compilation</option><option value="execution" :disabled="!submitted">Submitted snapshot</option></select>
      </label>
      <div class="workbench-status" role="status">
        <span>Compiler: {{ compilerError ? 'failed' : !ready ? 'loading' : preview.error || compileOutput?.diagnostics.length ? 'issues found' : 'ready' }}</span>
        <span>{{ writeBlocked ? 'Read-only · submission, execution or cleanup' : busy ? 'Updating files…' : 'Files editable' }}</span>
      </div>
    </section>
    <div class="workbench">
      <SourceWorkspace :files="files" :entry="entry" :selected-file="selectedFile" :read-only="readOnly" :compiler-raw="rawOutput" @file="selectFile" @text="editText" @add="addFile" @remove="removeFile" />
      <CfgCanvas :bundle="bundle" :scope="scope" :analysis="analysis" :analysis-raw="analysisRaw" :compiler-raw="currentRaw" :selected-node="selectedNode" :frames="frames" :query-variables="availableVariables" @select="selectNode" @navigate="navigate" />
    </div>
    <section class="bottom-panel" :data-expanded="panelOpen" aria-label="Execution and diagnostics">
      <div class="panel-toolbar">
        <div role="group" aria-label="Inspector panels">
          <button type="button" :aria-pressed="activePanel === 'execution'" aria-controls="execution-panel" @click="showPanel('execution')">Execution{{ execution ? ` · ${execution.status}` : observedId ? ' · awaiting status' : '' }}</button>
          <button type="button" :aria-pressed="activePanel === 'diagnostics'" aria-controls="diagnostics-panel" @click="showPanel('diagnostics')">Diagnostics ({{ diagnosticCount }})</button>
        </div>
        <button type="button" :aria-expanded="panelOpen" aria-controls="inspector-content" @click="panelOpen = !panelOpen">{{ panelOpen ? 'Collapse panel' : 'Expand panel' }}</button>
      </div>
      <div id="inspector-content" v-show="panelOpen" class="panel-content">
        <div id="execution-panel" v-show="activePanel === 'execution'">
          <ExecutionInspector :execution="execution" :pending-id="execution ? null : observedId" :slices="slices" :selected-key="selectedFrameKey" @select="selectFrame" />
        </div>
        <div id="diagnostics-panel" v-show="activePanel === 'diagnostics'">
          <DiagnosticsPanel :diagnostics="diagnostics" :compiler-error="compilerError ?? preview.error" :http-error="httpError" :workspace-error="workspaceProblem" @locate="locateDiagnostic" />
        </div>
      </div>
    </section>
  </main>
</template>
