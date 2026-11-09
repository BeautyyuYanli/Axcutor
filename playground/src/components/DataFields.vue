<script setup lang="ts">
import { computed } from 'vue';
import { JsonText } from '../utils/losslessJson';

const props = defineProps<{ raw: string | null; emptyText: string }>();
// Iterate token entries, including integer-like field names, in source order.
// Values never pass through JSON.parse or JavaScript numeric formatting.
const fields = computed(() => {
  const json = props.raw === null ? null : new JsonText(props.raw);
  return [...(json?.root.entries?.keys() ?? [])].map(name => ({ name, value: json!.format([name]) }));
});
</script>

<template>
  <div v-if="fields.length" class="table-scroll data-fields">
    <table>
      <thead><tr><th>Field</th><th>Value</th></tr></thead>
      <tbody><tr v-for="field in fields" :key="field.name"><th scope="row">{{ field.name }}</th><td><pre><code>{{ field.value }}</code></pre></td></tr></tbody>
    </table>
  </div>
  <p v-else>{{ emptyText }}</p>
</template>
