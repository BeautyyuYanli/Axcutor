<script setup lang="ts">
import { computed, ref } from 'vue';
import type { WorkspaceFile } from '../client/types.gen';
import type { JsonText } from '../utils/losslessJson';

const props = defineProps<{
  files: WorkspaceFile[]; entry: string; entryPath: string; inputPath: string;
  selectedFile: string; dirty: boolean; busy: boolean; writeBlocked: boolean; loaded: boolean;
  problem: string | null; compilerRaw: JsonText | null;
}>();
const emit = defineEmits<{
  file: [path: string]; entry: [path: string]; input: [path: string];
  text: [path: string, text: string]; add: [path: string]; remove: [path: string];
  save: []; reload: [];
}>();
const newPath = ref('');
const readOnly = computed(() => props.busy || props.writeBlocked || !props.loaded);
const document = computed(() => props.files.find(file => file.path === props.selectedFile));
const sorted = computed(() => [...props.files].sort((a, b) => a.path.localeCompare(b.path)));
// Flat rows expose directory hierarchy without inventing directory operations.
const rows = computed(() => {
  const directories = new Set<string>();
  return sorted.value.flatMap(file => {
    const parts = file.path.split('/');
    const parents = parts.slice(0, -1).flatMap((_, i) => {
      const path = parts.slice(0, i + 1).join('/');
      if (directories.has(path)) return [];
      directories.add(path);
      return [{ path, depth: i, name: parts[i] + '/', file: null as WorkspaceFile | null }];
    });
    return [...parents, { path: file.path, depth: parts.length - 1, name: parts.at(-1)!, file }];
  });
});
const workflows = computed(() => sorted.value.filter(file => /\.(yaml|yml|json)$/.test(file.path)));
const inputs = computed(() => sorted.value.filter(file => file.path.endsWith('.json')));
const contract = computed(() => {
  const path = ['bundle', 'workflows', props.entry, 'input'];
  return [...(props.compilerRaw?.at(path)?.entries?.keys() ?? [])].map(name => ({
    name, schema: props.compilerRaw?.format([...path, name]),
  }));
});
function add() {
  if (readOnly.value) return;
  emit('add', newPath.value);
  newPath.value = '';
}
</script>

<template>
  <section aria-label="Source workspace">
    <h2>Workspace files</h2>
    <p role="status" data-workspace-state>{{ !loaded ? 'Loading workspace…' : dirty ? 'Unsaved changes' : 'Saved' }}</p>
    <button type="button" :disabled="readOnly" @click="emit('save')">Save files</button>
    <p v-if="writeBlocked" role="status">Files are read-only during submission, execution or cleanup.</p>
    <button type="button" :disabled="busy" @click="emit('reload')">Reload (discard edits)</button>
    <p v-if="problem" role="alert">{{ problem }}</p>
    <label>Entry
      <select aria-label="Entry" :value="entryPath" @change="emit('entry', ($event.target as HTMLSelectElement).value)">
        <option v-for="file in workflows" :key="file.path" :value="file.path">{{ file.path }}</option>
      </select>
    </label>
    <label>Input file
      <select aria-label="Input file" :value="inputPath" @change="emit('input', ($event.target as HTMLSelectElement).value)">
        <option value="">Empty input {}</option>
        <option v-for="file in inputs" :key="file.path" :value="file.path">{{ file.path }}</option>
      </select>
    </label>
    <nav aria-label="Workspace files" class="file-tree">
      <div v-for="row in rows" :key="row.path" :style="{ paddingLeft: `${row.depth}rem` }">
        <button v-if="row.file" type="button" :aria-label="row.path" :aria-pressed="row.path === selectedFile" @click="emit('file', row.path)">{{ row.name }}{{ row.file.generated ? ' (generated)' : '' }}</button>
        <span v-else>{{ row.name }}</span>
      </div>
    </nav>
    <form @submit.prevent="add">
      <label>New file path <input v-model="newPath" :disabled="readOnly" placeholder="workflows/demo/main/helper.py" required /></label>
      <button type="submit" :disabled="readOnly">Create file</button>
    </form>
    <template v-if="document">
      <p v-if="document.generated">Generated contract · read-only</p>
      <button type="button" :disabled="document.generated || readOnly" @click="emit('remove', selectedFile)">Delete file</button>
      <label :for="'source-text'">{{ selectedFile }}</label>
      <textarea id="source-text" :value="document.text" :readonly="document.generated || readOnly" rows="26" spellcheck="false" @input="emit('text', selectedFile, ($event.target as HTMLTextAreaElement).value)" />
    </template>
    <p v-else>Select or create a source file.</p>
    <details v-if="compilerRaw?.at(['bundle', 'workflows', entry])">
      <summary>Entry input contract ({{ contract.length }} fields)</summary>
      <table>
        <thead><tr><th>Field</th><th>TypeSpec</th></tr></thead>
        <tbody><tr v-for="field in contract" :key="field.name"><th>{{ field.name }}</th><td><pre><code>{{ field.schema }}</code></pre></td></tr></tbody>
      </table>
    </details>
  </section>
</template>
