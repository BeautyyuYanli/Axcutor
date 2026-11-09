<script setup lang="ts">
import { computed } from 'vue';
import type { Execution, Frame } from '../client/types.gen';
import type { RawExecutionSlices } from '../utils/losslessJson';
import { flattenSteps, type ExecutionStep } from '../utils/executionView';
import FrameTree from './FrameTree.vue';
import DataFields from './DataFields.vue';

const props = defineProps<{
  execution: Execution | null; pendingId: string | null; preparing: boolean;
  slices: RawExecutionSlices; steps: ExecutionStep[]; selectedKey: string | null;
}>();
const emit = defineEmits<{ select: [frame: Frame, key: string] }>();
const selected = computed(() => flattenSteps(props.steps).find(step => step.key === props.selectedKey));
</script>

<template>
  <section class="execution-inspector" aria-label="Execution inspector" :data-status="preparing ? 'submitting' : execution?.status ?? (pendingId ? 'observing' : 'idle')">
    <h2 class="sr-only">Execution</h2>
    <p v-if="preparing">Waiting for this submission…</p>
    <template v-else>
      <section v-if="execution?.status === 'completed' && execution.result" aria-label="Execution result">
        <h3>Succeeded · {{ execution.result.branch === null ? 'end' : `branch ${execution.result.branch}` }}</h3>
        <DataFields :raw="slices.resultOutputRaw" empty-text="The execution returned no output fields." />
      </section>
      <section v-else-if="execution?.status === 'completed'" aria-label="Execution result">
        <h3>Succeeded</h3><p>No result was returned.</p>
      </section>
      <section v-else-if="execution?.status === 'failed'" aria-label="Execution failure">
        <h3>Failed{{ execution.error ? ` · ${execution.error.type}` : '' }}</h3>
        <p role="alert">{{ execution.error?.message ?? 'No failure reason was returned.' }}</p>
      </section>
      <p v-else-if="execution?.status === 'cancelled'">Execution was cancelled.</p>
      <div v-else class="execution-current">
        <section class="execution-steps" aria-label="Current steps">
          <h3>Current steps</h3>
          <FrameTree v-if="steps.length" :steps="steps" :selected-key="selectedKey" @select="(frame, key) => emit('select', frame, key)" />
          <p v-else>{{ pendingId || execution ? 'Waiting for current steps…' : 'No current steps. Run a compiled workflow to begin.' }}</p>
        </section>
        <section aria-label="Selected step data" class="selected-step">
          <h3>{{ selected?.label ?? 'Step data' }}</h3>
          <template v-if="selected">
            <p v-if="selected.frame.waiting.length">Waiting for signal. Send a signal to an exact address:</p>
            <ul v-if="selected.frame.waiting.length" class="signal-addresses"><li v-for="address in selected.frame.waiting" :key="address"><code>{{ address }}</code></li></ul>
            <DataFields :raw="slices.frameVariablesRaw.get(selected.path) ?? null" empty-text="This step has no current data fields." />
          </template>
          <p v-else>No step data available yet.</p>
        </section>
      </div>
      <details v-if="execution || pendingId" class="execution-metadata">
        <summary>Execution details</summary>
        <dl>
          <dt>ID</dt><dd>{{ execution?.execution_id ?? pendingId }}</dd>
          <template v-if="execution">
            <dt>Started</dt><dd>{{ execution.started_at }}</dd>
            <dt>Finished</dt><dd>{{ execution.finished_at ?? 'In progress' }}</dd>
          </template>
        </dl>
      </details>
    </template>
  </section>
</template>
