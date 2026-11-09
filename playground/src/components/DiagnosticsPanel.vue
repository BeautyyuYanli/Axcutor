<script setup lang="ts">
import type { Diagnostic } from '../types/compiler';
import type { ExecutionProblem } from '../composables/useExecution';

defineProps<{ diagnostics: Diagnostic[]; compilerError: string | null; httpError: ExecutionProblem | null; workspaceError: string | null }>();
const emit = defineEmits<{ locate: [diagnostic: Diagnostic] }>();
</script>

<template>
  <section aria-label="Diagnostics">
    <h2 class="sr-only">Diagnostics</h2>
    <p v-if="workspaceError" role="alert">{{ workspaceError }}</p>
    <p v-if="compilerError" role="alert">{{ compilerError }}</p>
    <div v-if="httpError" role="alert">
      <p>HTTP {{ httpError.status ?? '—' }} / {{ httpError.code }}: {{ httpError.message }}</p>
      <details v-if="httpError.detailsRaw && httpError.detailsRaw !== 'null'">
        <summary>Response details</summary><pre><code>{{ httpError.detailsRaw }}</code></pre>
      </details>
    </div>
    <p v-if="!diagnostics.length && !compilerError && !httpError && !workspaceError">No diagnostics.</p>
    <div v-if="diagnostics.length" class="table-scroll"><table>
      <thead><tr><th>Location</th><th>Code</th><th>Message</th></tr></thead>
      <tbody>
        <tr v-for="(item, index) in diagnostics" :key="index" @click="emit('locate', item)">
          <td><button type="button" @click.stop="emit('locate', item)">
            {{ [item.workflow_id, ...item.scope_graph_path, item.node_id].filter(Boolean).join(' / ') }}
            {{ item.source_location ? `:${item.source_location.line}:${item.source_location.column}` : item.param_path }}
          </button></td>
          <td>{{ item.code }}</td><td>{{ item.message }}</td>
        </tr>
      </tbody>
    </table></div>
  </section>
</template>
