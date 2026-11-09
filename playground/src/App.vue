<script setup lang="ts">
import { computed, nextTick, onMounted, ref, shallowRef, watch } from 'vue';
import SourceWorkspace from './components/SourceWorkspace.vue';
import CfgCanvas from './components/CfgCanvas.vue';
import DiagnosticsPanel from './components/DiagnosticsPanel.vue';
import ExecutionInspector from './components/ExecutionInspector.vue';
import TraceCanvas from './components/TraceCanvas.vue';
import TraceInspector from './components/TraceInspector.vue';
import { compileSource, useCompiler } from './composables/useCompiler';
import { useExecution } from './composables/useExecution';
import { useLatestTrace } from './composables/useLatestTrace';
import { useWorkspace } from './composables/useWorkspace';
import { workspaceSource } from './utils/workspace';
import type { CompileOutput, Diagnostic, Scope } from './types/compiler';
import type { Frame } from './client/types.gen';
import { locateFrame, sameScope } from './utils/graphLayout';
import { executionSteps, executionSummary, flattenSteps } from './utils/executionView';
import { locateSpan } from './utils/traceView';

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
const selectedSpanId = ref<string | null>(null);
const canvasSource = ref<'editor' | 'execution'>('editor');
const submitted = shallowRef<CompileOutput | null>(null);
const { ready, error: compilerError, compileOutput } = useCompiler(entry, packages);
const { execution, observedId, slices, source: submittedSource, workspace: runWorkspace, error: httpError, submitting, cancelRequested, run, cancel, restore: restoreExecution } = useExecution();
const trace = useLatestTrace();
const { view: traceView, problem: traceProblem } = trace;
const currentId = computed(() => observedId.value ?? execution.value?.execution_id ?? null);
watch(currentId, id => {
  selectedNode.value = null;
  selectedFrameKey.value = null;
  selectedSpanId.value = null;
  if (id) trace.track(id);
});
const runSource = computed(() => traceView.value?.metadata.execution_id === currentId.value
  ? traceView.value.metadata.source : submittedSource.value);
let snapshotId: string | null = null;
watch([ready, runSource, currentId], () => {
  if (currentId.value !== snapshotId) { submitted.value = null; canvasSource.value = 'editor'; }
  if (!ready.value || !runSource.value || !currentId.value || snapshotId === currentId.value) return;
  submitted.value = compileSource(runSource.value);
  snapshotId = currentId.value;
  // A delayed recovery may arrive after editing began. Make the submitted
  // snapshot available without replacing the user's editor view or scope.
  if (!loaded.value || !dirty.value) {
    canvasSource.value = 'execution';
    navigate({ workflowId: runSource.value.entry, scopeGraphPath: [] });
  }
});
watch(traceView, value => {
  const spans = value?.metadata.spans ?? [];
  if (!spans.some(span => span.id === selectedSpanId.value)) {
    const parents = new Set(spans.map(span => span.parent_id));
    selectedSpanId.value = spans.find(span => !parents.has(span.id))?.id ?? null;
  }
});
const preparing = ref(false);
// Every first successful observation can restore the page, including Retry
// after bootstrap failed. Execution guards protect an already observed ID;
// preparation also covers Save/Run before a new submission has returned its ID.
watch([traceView, preparing], ([value, isPreparing]) => {
  if (value && !isPreparing) restoreExecution(
    value.json.raw(['execution']), value.metadata.execution_id, value.metadata.source,
  );
});
const current = computed(() => canvasSource.value === 'execution' ? submitted.value : compileOutput.value);
const bundle = computed(() => current.value?.bundle ?? null);
const analysis = computed(() => current.value?.analysis.find(item => sameScope({
  workflowId: item.workflow_id, scopeGraphPath: item.scope_graph_path,
}, scope.value)) ?? null);
const diagnostics = computed(() => [...(compileOutput.value?.diagnostics ?? []), ...(httpError.value?.diagnostics ?? [])]);
const frames = computed(() => canvasSource.value === 'execution' && execution.value?.status === 'running' ? execution.value.frames : []);
const writeBlocked = computed(() => serverBusy.value || preparing.value || submitting.value || observedId.value !== null);
const readOnly = computed(() => busy.value || writeBlocked.value || !loaded.value);
const workflowFiles = computed(() => files.value.filter(file => /\.(yaml|yml|json)$/.test(file.path)).sort((a, b) => a.path.localeCompare(b.path)));
const inputFiles = computed(() => workflowFiles.value.filter(file => file.path.endsWith('.json')));
const entryOptions = computed(() => workflowFiles.value.map(file => {
  // Use the same discovery as preview and Run; standalone documents are __main__.
  try { return { path: file.path, name: workspaceSource(files.value, file.path).entry }; }
  catch { return { path: file.path, name: 'Invalid entry' }; }
}));
const activePanel = ref<'execution' | 'trace' | 'diagnostics'>('execution');
const panelOpen = ref(false);
const traceDetails = ref<HTMLElement | null>(null);
const filesOpen = ref(true);
const editorOpen = ref(true);
const summary = computed(() => executionSummary({
  preparing: preparing.value, submitting: submitting.value, cancelRequested: cancelRequested.value,
  execution: execution.value, observedId: observedId.value, requestProblem: !!httpError.value,
}));
const steps = computed(() => execution.value?.status === 'running' && !preparing.value
  ? executionSteps(execution.value.frames, submitted.value?.bundle ?? null) : []);
// A vanished step must not leave stale variables on screen. Automatic selection
// follows a real leaf without changing the user's canvas source or scope.
watch(steps, value => {
  const all = flattenSteps(value);
  if (!all.some(step => step.key === selectedFrameKey.value)) {
    selectedFrameKey.value = (all.find(step => !step.children.length) ?? all[0])?.key ?? null;
    if (canvasSource.value === 'execution') selectedNode.value = null;
  }
});
const workspaceNotice = computed(() => {
  if (preparing.value || submitting.value) return '';
  if (serverBusy.value && execution.value && execution.value.status !== 'running') {
    return 'Execution finished. The workspace is still busy with local work or cleanup.';
  }
  if (cancelRequested.value || (observedId.value && (httpError.value || !execution.value))) return summary.value.message;
  if (serverBusy.value && !currentId.value) return 'The shared workspace is busy in another page.';
  return '';
});
const diagnosticCount = computed(() => diagnostics.value.length + Number(!!(preview.value.error ?? compilerError.value)) + Number(!!workspaceProblem.value) + Number(!!httpError.value));
function showPanel(panel: 'execution' | 'trace' | 'diagnostics') { activePanel.value = panel; panelOpen.value = true; }
function togglePanel(panel: 'execution' | 'trace' | 'diagnostics') {
  if (activePanel.value === panel) panelOpen.value = !panelOpen.value;
  else showPanel(panel);
}
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
  editorOpen.value = true;
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
  editorOpen.value = true;
  selectedFile.value = preview.value.source?.paths[diagnostic.workflow_id] ?? selectedFile.value;
  canvasSource.value = 'editor';
  scope.value = { workflowId: diagnostic.workflow_id, scopeGraphPath: [...diagnostic.scope_graph_path] };
  selectedNode.value = diagnostic.node_id;
}
function selectFrame(frame: Frame, key: string) {
  selectedFrameKey.value = key;
  if (!submitted.value?.bundle) return;
  const target = locateFrame(submitted.value.bundle, frame);
  if (!target) return;
  canvasSource.value = 'execution';
  scope.value = { workflowId: target.workflowId, scopeGraphPath: target.scopeGraphPath };
  selectedNode.value = target.nodeId ?? null;
}
function selectNode(id: string) {
  selectedNode.value = id;
  if (canvasSource.value !== 'execution' || !submitted.value?.bundle) return;
  const snapshot = submitted.value.bundle;
  const find = (items: Frame[]): Frame | undefined => {
    for (const frame of items) {
      const target = locateFrame(snapshot, frame);
      if (target && sameScope(target, scope.value) && target.nodeId === id) return frame;
      const child = find(frame.children);
      if (child) return child;
    }
  };
  const frame = find(execution.value?.frames ?? []);
  if (frame) selectedFrameKey.value = `${frame.kind}:${frame.address}`;
}
function selectSpan(id: string) {
  selectedSpanId.value = id;
  showPanel('trace');
  void nextTick(() => traceDetails.value?.scrollTo({ top: 0 }));
}
function locateSelectedSpan() {
  const span = traceView.value?.metadata.spans.find(item => item.id === selectedSpanId.value);
  if (!span || !submitted.value?.bundle) return;
  const target = locateSpan(submitted.value.bundle, span);
  if (!target) return;
  canvasSource.value = 'execution';
  scope.value = { workflowId: target.workflowId, scopeGraphPath: target.scopeGraphPath };
  selectedNode.value = target.nodeId ?? null;
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
    const accepted = await run(path, input, revision);
    // An uncertain submission with an ID still belongs in the execution view.
    if (!accepted && observedId.value === null) showPanel('diagnostics');
    if (runWorkspace.value) workspace.accept(runWorkspace.value);
    else await workspace.observe();
  } finally { preparing.value = false; }
}
watch(entryPath, value => { selectFile(value); });
watch(entry, value => { if (canvasSource.value === 'editor') navigate({ workflowId: value, scopeGraphPath: [] }); });
onMounted(async () => {
  void trace.restore();
  await workspace.load();
  if (!files.value.some(file => file.path === entryPath.value)) entryPath.value = files.value.find(file => /\.(yaml|yml|json)$/.test(file.path))?.path ?? '';
  if (!files.value.some(file => file.path === inputPath.value)) inputPath.value = '';
});
</script>

<template>
  <main class="app-shell">
    <section class="run-config" aria-label="Run configuration">
      <label>Entry
        <select v-model="entryPath" aria-label="Entry" :title="entryPath">
          <option v-for="option in entryOptions" :key="option.path" :value="option.path" :title="option.path">{{ option.name }}</option>
        </select>
      </label>
      <label>Input file
        <select v-model="inputPath" aria-label="Input file" :title="inputPath || 'Empty input {}'">
          <option value="">Empty input {}</option>
          <option v-for="file in inputFiles" :key="file.path" :value="file.path">{{ file.path }}</option>
        </select>
      </label>
      <div class="run-actions" role="group" aria-label="Run actions">
        <button class="primary-action" type="button" :disabled="readOnly || !ready || !preview.source || !compileOutput?.bundle" @click="submit">{{ preparing || submitting ? 'Submitting…' : 'Run' }}</button>
        <button type="button" :disabled="observedId === null || cancelRequested" @click="cancel">{{ cancelRequested ? 'Cancelling…' : 'Cancel' }}</button>
      </div>
      <div v-if="compilerError || !ready || preview.error || compileOutput?.diagnostics.length" class="workbench-status" role="status">
        <span>Compiler: {{ compilerError ? 'failed' : !ready ? 'loading' : preview.error || compileOutput?.diagnostics.length ? 'issues found' : 'ready' }}</span>
      </div>
    </section>
    <div class="workbench" :data-files-open="filesOpen" :data-editor-open="editorOpen">
      <SourceWorkspace :files="files" :files-open="filesOpen" :editor-open="editorOpen" :selected-file="selectedFile" :read-only="readOnly" :busy="busy" :loaded="loaded" :dirty="dirty" @toggle-files="filesOpen = !filesOpen" @toggle-editor="editorOpen = !editorOpen" @save="workspace.save" @discard="workspace.load" @file="selectFile" @text="editText" @add="addFile" @remove="removeFile" />
      <section class="main-canvas" aria-label="Workflow canvas" :data-canvas-source="canvasSource">
        <CfgCanvas :bundle="bundle" :scope="scope" :analysis="analysis" :selected-node="selectedNode" :frames="frames" @select="selectNode" @navigate="navigate" />
      </section>
    </div>
    <section class="bottom-panel" :data-expanded="panelOpen" :data-panel="activePanel" aria-label="Execution and diagnostics">
      <div class="panel-toolbar">
        <div role="group" aria-label="Inspector panels">
          <button type="button" :aria-pressed="activePanel === 'execution'" :aria-expanded="activePanel === 'execution' && panelOpen" aria-controls="execution-panel" @click="togglePanel('execution')">Execution · {{ summary.label }}</button>
          <button type="button" :aria-pressed="activePanel === 'trace'" :aria-expanded="activePanel === 'trace' && panelOpen" aria-controls="trace-panel" @click="togglePanel('trace')">Trace</button>
          <button type="button" :aria-pressed="activePanel === 'diagnostics'" :aria-expanded="activePanel === 'diagnostics' && panelOpen" aria-controls="diagnostics-panel" @click="togglePanel('diagnostics')">Diagnostics ({{ diagnosticCount }})</button>
        </div>
      </div>
      <p v-if="workspaceNotice" class="execution-summary" role="status">{{ workspaceNotice }}</p>
      <div id="inspector-content" v-show="panelOpen" class="panel-content">
        <div id="execution-panel" v-show="activePanel === 'execution'">
          <ExecutionInspector :execution="execution" :pending-id="observedId" :preparing="preparing || submitting" :slices="slices" :steps="steps" :selected-key="selectedFrameKey" @select="selectFrame" />
        </div>
        <div id="trace-panel" v-show="activePanel === 'trace'" class="trace-panel">
          <TraceCanvas :spans="traceView?.metadata.spans ?? []" :observed-at="traceView?.metadata.observed_at ?? null" :selected-id="selectedSpanId" :problem="traceProblem" :complete="traceView?.metadata.complete ?? false" @select="selectSpan" @retry="trace.retry" />
          <div ref="traceDetails" class="trace-details">
            <TraceInspector v-if="traceView && !preparing && !submitting" :view="traceView" :selected-id="selectedSpanId" @locate="locateSelectedSpan" />
          </div>
        </div>
        <div id="diagnostics-panel" v-show="activePanel === 'diagnostics'">
          <DiagnosticsPanel :diagnostics="diagnostics" :compiler-error="compilerError ?? preview.error" :http-error="httpError" :workspace-error="workspaceProblem" @locate="locateDiagnostic" />
        </div>
      </div>
    </section>
  </main>
</template>
