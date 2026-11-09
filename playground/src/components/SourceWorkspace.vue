<script setup lang="ts">
import { computed, ref } from 'vue';
import type { WorkspaceFile } from '../client/types.gen';

const props = defineProps<{
  files: WorkspaceFile[]; selectedFile: string;
  readOnly: boolean; filesOpen: boolean; editorOpen: boolean;
  busy: boolean; loaded: boolean; dirty: boolean;
}>();
const emit = defineEmits<{
  file: [path: string];
  text: [path: string, text: string]; add: [path: string]; remove: [path: string];
  toggleFiles: []; toggleEditor: []; save: []; discard: [];
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
function add() {
  if (props.readOnly) return;
  emit('add', newPath.value);
  newPath.value = '';
}
</script>

<template>
  <section id="workspace-files" class="file-sidebar" :data-open="filesOpen" aria-label="File browser">
    <div class="pane-header">
      <h2><button type="button" class="pane-toggle" aria-label="Workspace files" :title="filesOpen ? 'Collapse files' : 'Expand files'" :aria-expanded="filesOpen" aria-controls="workspace-files-content" @click="emit('toggleFiles')"><span v-if="filesOpen">▾ Workspace files</span><svg v-else width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M2 5h6l2 2h8v10H2z" /></svg></button></h2>
      <button v-show="filesOpen" type="button" :disabled="!document || document.generated || readOnly" @click="emit('remove', selectedFile)">Delete file</button>
    </div>
    <div id="workspace-files-content" v-show="filesOpen" class="pane-body">
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
    </div>
  </section>
  <section id="workspace-editor" class="source-editor" :data-open="editorOpen" aria-label="Source workspace">
    <div class="pane-header">
      <h2><button type="button" class="pane-toggle" aria-label="Editor" :title="editorOpen ? 'Collapse editor' : 'Expand editor'" :aria-expanded="editorOpen" aria-controls="workspace-editor-content" @click="emit('toggleEditor')"><span v-if="editorOpen">▾ Editor</span><span v-else aria-hidden="true">&lt;/&gt;</span></button></h2>
      <div v-show="editorOpen" class="editor-actions" role="group" aria-label="Editor actions">
        <button type="button" :disabled="readOnly" @click="emit('save')">Save files</button>
        <button type="button" :disabled="busy" @click="emit('discard')">Discard edits</button>
      </div>
    </div>
    <div id="workspace-editor-content" v-show="editorOpen" class="pane-body">
      <p class="source-state" role="status" data-workspace-state>{{ !loaded ? 'Loading workspace…' : dirty ? 'Unsaved changes' : 'Saved' }}</p>
      <template v-if="document">
        <label for="source-text" class="source-path" :title="selectedFile">{{ selectedFile }}</label>
        <p v-if="document.generated || readOnly" class="source-state">{{ document.generated ? 'Generated contract · read-only' : 'Read-only' }}</p>
        <textarea id="source-text" :value="document.text" :readonly="document.generated || readOnly" rows="10" spellcheck="false" wrap="off" @input="emit('text', selectedFile, ($event.target as HTMLTextAreaElement).value)" />
      </template>
      <p v-else>Select or create a source file.</p>
    </div>
  </section>
</template>
