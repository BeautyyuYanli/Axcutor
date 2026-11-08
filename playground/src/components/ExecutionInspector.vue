<script setup lang="ts">
import type { Execution, Frame } from '../client/types.gen';
import type { RawExecutionSlices } from '../utils/losslessJson';
import FrameTree from './FrameTree.vue';

defineProps<{ execution: Execution | null; slices: RawExecutionSlices; selectedKey: string | null }>();
const emit = defineEmits<{ select: [frame: Frame, key: string] }>();
</script>

<template>
  <section aria-label="Execution inspector" :data-status="execution?.status ?? 'idle'">
    <h2>Execution</h2>
    <p v-if="!execution">Run a compiled workflow to inspect its active frames.</p>
    <template v-else>
      <dl>
        <dt>ID</dt><dd>{{ execution.execution_id }}</dd>
        <dt>Status</dt><dd role="status">{{ execution.status }}</dd>
        <dt>Started</dt><dd>{{ execution.started_at }}</dd>
        <dt>Finished</dt><dd>{{ execution.finished_at ?? '—' }}</dd>
      </dl>
      <h3>Active frames</h3>
      <FrameTree v-if="execution.frames.length" :frames="execution.frames" :slices="slices" :selected-key="selectedKey" @select="(frame, key) => emit('select', frame, key)" />
      <p v-else>No active frames.</p>
      <section v-if="execution.status === 'completed' && execution.result" aria-label="Execution result">
        <h3>Result · {{ execution.result.branch ?? 'end' }}</h3>
        <pre><code>{{ slices.resultOutputRaw }}</code></pre>
      </section>
      <section v-if="execution.status === 'failed' && execution.error" aria-label="Execution failure">
        <h3>{{ execution.error.category }} / {{ execution.error.type }}</h3>
        <p>{{ execution.error.message }}</p>
      </section>
    </template>
  </section>
</template>
