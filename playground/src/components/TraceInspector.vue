<script setup lang="ts">
import { computed } from 'vue';
import type { TraceView } from '../composables/useLatestTrace';
import { spanDuration, durationText } from '../utils/traceView';
import DataFields from './DataFields.vue';

const props = defineProps<{ view: TraceView; selectedId: string | null }>();
const emit = defineEmits<{ locate: [] }>();
const index = computed(() => props.view.metadata.spans.findIndex(span => span.id === props.selectedId));
const span = computed(() => props.view.metadata.spans[index.value]);
const terminal = computed(() => props.view.metadata.execution && props.view.metadata.execution.status !== 'running');
const waiting = computed(() => span.value?.status === 'running' && !terminal.value
  ? span.value.events.find(event => event.kind === 'signal_wait') : null);
const raw = (key: string) => props.view.json.format(['spans', index.value, key]);
</script>

<template>
  <section class="trace-inspector" aria-label="Trace call details">
    <template v-if="span">
      <div class="trace-toolbar"><h3>{{ span.address }}{{ span.kind === 'attempt' ? ` · attempt ${span.attempt}` : '' }}</h3><button type="button" @click="emit('locate')">Locate in Flow</button></div>
      <dl>
        <dt>Status</dt><dd>{{ span.status === 'running' && terminal ? 'No end event recorded (last status: running)' : span.status }}</dd>
        <dt>Duration</dt><dd>{{ durationText(spanDuration(span, view.metadata.observed_at)) }}{{ span.finished_at ? '' : ' observed' }}</dd>
        <template v-if="span.status === 'completed' && span.branch !== null"><dt>Branch</dt><dd>{{ span.branch }}</dd></template>
      </dl>
      <p v-if="span.error" role="status">{{ span.error.type }}: {{ span.error.message }}</p>
      <p v-if="waiting">Waiting for signal at <code>{{ waiting.address }}</code>.</p>
      <div class="trace-payloads">
        <section aria-label="Call Input"><h3>Input</h3><DataFields :raw="raw('input')" empty-text="This call used no input fields." /></section>
        <section aria-label="Call Output"><h3>Output</h3>
          <DataFields v-if="span.output !== null" :raw="raw('output')" empty-text="This call returned no output fields." />
          <p v-else>{{ span.status === 'failed' ? 'No output: this call failed.' : span.status === 'cancelled' ? 'No output: this call was cancelled.' : terminal ? 'No output was recorded before the execution ended.' : 'No output has been returned.' }}</p>
        </section>
      </div>
      <details class="trace-timing"><summary>Timing</summary><dl>
        <dt>Started</dt><dd>{{ span.started_at }}</dd>
        <dt>Finished</dt><dd>{{ span.finished_at ?? (terminal ? 'No end event recorded' : 'In progress') }}</dd>
      </dl></details>
      <details v-if="span.events.length"><summary>Waiting / retry / error events</summary><pre>{{ raw('events') }}</pre><p v-if="span.events.some(event => event.kind === 'activity_retry_policy')">Temporal manages Activity retries. Individual attempt counts and times are unavailable in this trace.</p></details>
    </template>
    <p v-else>Select a recorded call in Trace.</p>
  </section>
</template>
