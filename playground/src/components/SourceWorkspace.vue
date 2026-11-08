<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Diagnostic, SourceDocument, SourceFormat } from '../types/compiler';
import type { JsonText } from '../utils/losslessJson';

const props = defineProps<{
  packages: Record<string, SourceDocument>;
  entry: string;
  selectedFile: string;
  inputRawText: string;
  diagnostics: Diagnostic[];
  compilerRaw: JsonText | null;
}>();
const emit = defineEmits<{
  file: [path: string]; entry: [path: string]; input: [text: string];
  text: [path: string, text: string]; format: [path: string, format: SourceFormat];
  add: [path: string, format: SourceFormat]; remove: [path: string]; reset: [];
}>();
const newPath = ref('');
const newFormat = ref<SourceFormat>('yaml');
const message = ref('');
const document = computed(() => props.packages[props.selectedFile]);
const contract = computed(() => {
  const path = ['bundle', 'workflows', props.entry, 'input'];
  return [...(props.compilerRaw?.at(path)?.entries?.keys() ?? [])].map(name => ({
    name, schema: props.compilerRaw?.format([...path, name]),
  }));
});
function add() {
  if (Object.hasOwn(props.packages, newPath.value)) { message.value = 'Package already exists'; return; }
  emit('add', newPath.value, newFormat.value);
  newPath.value = '';
  message.value = '';
}
</script>

<template>
  <section aria-label="Source workspace">
    <h2>Packages</h2>
    <button type="button" @click="emit('reset')">Load default example</button>
    <label>Entry
      <select aria-label="Entry" :value="entry" @change="emit('entry', ($event.target as HTMLSelectElement).value)">
        <option v-for="(_, path) in packages" :key="path" :value="path">{{ path }}</option>
      </select>
    </label>
    <nav aria-label="Source files">
      <button v-for="(_, path) in packages" :key="path" type="button" :aria-pressed="path === selectedFile" @click="emit('file', String(path))">
        {{ path }} ({{ diagnostics.filter(item => item.workflow_id === path).length }})
      </button>
    </nav>
    <form @submit.prevent="add">
      <label>New package name <input v-model="newPath" placeholder="billing.charge or __main__" required /></label>
      <label>New file format <select v-model="newFormat" aria-label="New file format"><option>yaml</option><option>json</option></select></label>
      <button type="submit">Add package</button>
    </form>
    <p v-if="message" role="alert">{{ message }}</p>
    <template v-if="document">
      <label>Format
        <select aria-label="Format" :value="document.format" @change="emit('format', selectedFile, ($event.target as HTMLSelectElement).value as SourceFormat)">
          <option>yaml</option><option>json</option>
        </select>
      </label>
      <button type="button" @click="emit('remove', selectedFile)">Delete package</button>
      <label :for="'source-text'">{{ selectedFile }}</label>
      <textarea id="source-text" :value="document.text" rows="26" spellcheck="false" @input="emit('text', selectedFile, ($event.target as HTMLTextAreaElement).value)" />
    </template>
    <p v-else>No workflow packages. Add a package or load the example.</p>
    <details v-if="compilerRaw?.at(['bundle', 'workflows', entry])">
      <summary>Entry input contract ({{ contract.length }} fields)</summary>
      <table>
        <thead><tr><th>Field</th><th>TypeSpec</th></tr></thead>
        <tbody><tr v-for="field in contract" :key="field.name"><th>{{ field.name }}</th><td><pre><code>{{ field.schema }}</code></pre></td></tr></tbody>
      </table>
    </details>
    <label for="input-text">Input JSON</label>
    <textarea id="input-text" :value="inputRawText" rows="9" spellcheck="false" @input="emit('input', ($event.target as HTMLTextAreaElement).value)" />
  </section>
</template>
