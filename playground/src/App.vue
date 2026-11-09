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
  preparing.value = true;
  const path = entryPath.value;
  const input = inputPath.value || null;
  try {
    const revision = await workspace.save();
    if (!revision) return;
    // A new submission may return only an ID; its frames cannot use an older bundle.
    submitted.value = null;
    selectedFrameKey.value = null;
    canvasSource.value = 'editor';
    navigate({ workflowId: entry.value, scopeGraphPath: [] });
    const accepted = await run(path, input, revision);
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
  <main>
    <header>
      <h1>Axcutor Playground</h1>
      <p role="status">Compiler: {{ ready ? 'ready' : 'loading' }}</p>
      <button type="button" :disabled="!loaded || busy || serverBusy || !ready || !preview.source || !compileOutput?.bundle || preparing || submitting || observedId !== null" @click="submit">{{ preparing || submitting ? 'Submitting…' : 'Run' }}</button>
      <button type="button" :disabled="observedId === null || cancelRequested" @click="cancel">{{ cancelRequested ? 'Cancelling…' : 'Cancel' }}</button>
      <label>Canvas source
        <select v-model="canvasSource" aria-label="Canvas source" @change="changeCanvasSource"><option value="editor">Editor compilation</option><option value="execution" :disabled="!submitted">Submitted snapshot</option></select>
      </label>
    </header>
    <div class="workbench">
      <SourceWorkspace :files="files" :entry="entry" :entry-path="entryPath" :input-path="inputPath" :selected-file="selectedFile" :dirty="dirty" :busy="busy" :write-blocked="serverBusy || preparing || submitting || observedId !== null" :loaded="loaded" :problem="workspaceProblem" :compiler-raw="rawOutput" @file="selectFile" @entry="entryPath = $event" @input="inputPath = $event" @text="editText" @add="addFile" @remove="removeFile" @save="workspace.save" @reload="workspace.load" />
      <CfgCanvas :bundle="bundle" :scope="scope" :analysis="analysis" :analysis-raw="analysisRaw" :compiler-raw="currentRaw" :selected-node="selectedNode" :frames="frames" :query-variables="availableVariables" @select="selectNode" @navigate="navigate" />
      <ExecutionInspector :execution="execution" :pending-id="execution ? null : observedId" :slices="slices" :selected-key="selectedFrameKey" @select="selectFrame" />
    </div>
    <DiagnosticsPanel :diagnostics="diagnostics" :compiler-error="preview.error ?? compilerError" :http-error="httpError" @locate="locateDiagnostic" />
  </main>
</template>
