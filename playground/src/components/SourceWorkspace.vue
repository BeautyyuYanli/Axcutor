<script setup lang="ts">
import { computed, ref } from 'vue';
import type { WorkspaceFile } from '../client/types.gen';
import type { JsonText } from '../utils/losslessJson';

const props = defineProps<{
  files: WorkspaceFile[]; entry: string; selectedFile: string;
  readOnly: boolean; compilerRaw: JsonText | null;
}>();
const emit = defineEmits<{
  file: [path: string];
  text: [path: string, text: string]; add: [path: string]; remove: [path: string];
}>();
const newPath = ref('');
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
const contract = computed(() => {
  const path = ['bundle', 'workflows', props.entry, 'input'];
  return [...(props.compilerRaw?.at(path)?.entries?.keys() ?? [])].map(name => ({
    name, schema: props.compilerRaw?.format([...path, name]),
  }));
});
function add() {
  if (props.readOnly) return;
  emit('add', newPath.value);
  newPath.value = '';
}
</script>

<template>
  <section class="file-sidebar" aria-label="File browser">
    <h2>Workspace files</h2>
    <nav aria-label="Workspace files" class="file-tree">
      <div v-for="row in rows" :key="row.path" class="file-row" :style="{ paddingLeft: `${Math.min(row.depth, 6) * 0.65}rem` }">
        <button v-if="row.file" type="button" :title="row.path" :aria-label="row.path" :aria-pressed="row.path === selectedFile" @click="emit('file', row.path)">{{ row.name }}{{ row.file.generated ? ' (generated)' : '' }}</button>
        <span v-else :title="row.path">{{ row.name }}</span>
      </div>
    </nav>
    <form class="new-file" @submit.prevent="add">
      <label>New file path <input v-model="newPath" :disabled="readOnly" placeholder="workflows/demo/main/helper.py" required /></label>
      <button type="submit" :disabled="readOnly">Create file</button>
    </form>
  </section>
  <section class="source-editor" aria-label="Source workspace">
    <div class="source-toolbar">
      <h2>Source</h2>
      <button v-if="document" type="button" :disabled="document.generated || readOnly" @click="emit('remove', selectedFile)">Delete file</button>
    </div>
    <template v-if="document">
      <label for="source-text" class="source-path" :title="selectedFile">{{ selectedFile }}</label>
      <p class="source-state">{{ document.generated ? 'Generated contract · read-only' : readOnly ? 'Source · read-only' : 'Source · editable' }}</p>
      <textarea id="source-text" :value="document.text" :readonly="document.generated || readOnly" rows="10" spellcheck="false" wrap="off" @input="emit('text', selectedFile, ($event.target as HTMLTextAreaElement).value)" />
    </template>
    <p v-else>Select or create a source file.</p>
    <details v-if="compilerRaw?.at(['bundle', 'workflows', entry])" class="entry-contract">
      <summary>Entry input contract ({{ contract.length }} fields)</summary>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Field</th><th>TypeSpec</th></tr></thead>
          <tbody><tr v-for="field in contract" :key="field.name"><th>{{ field.name }}</th><td><pre><code>{{ field.schema }}</code></pre></td></tr></tbody>
        </table>
      </div>
    </details>
  </section>
</template>
