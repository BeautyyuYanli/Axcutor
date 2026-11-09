<script setup lang="ts">
import { computed, ref } from 'vue';
import type { TraceSpan } from '../client/types.gen';
import { traceRows, spanDuration, durationText } from '../utils/traceView';

const props = defineProps<{
  spans: TraceSpan[]; observedAt: string | null; selectedId: string | null;
  problem: string | null; complete: boolean;
}>();
const emit = defineEmits<{ select: [id: string]; retry: [] }>();
const zoom = ref(1);
const rows = computed(() => traceRows(props.spans));
const start = computed(() => Math.min(...props.spans.map(span => Date.parse(span.started_at))));
const end = computed(() => Math.max(start.value, ...props.spans.map(span => Date.parse(span.finished_at ?? props.observedAt ?? span.started_at))));
// A degenerate time range has a visible point at t=0, never a fabricated span.
const range = computed(() => end.value - start.value);
const position = (span: TraceSpan) => ({
  left: `${range.value ? 100 * (Date.parse(span.started_at) - start.value) / range.value : 0}%`,
  width: `${range.value ? 100 * (spanDuration(span, props.observedAt) ?? 0) / range.value : 0}%`,
});
function spanState(span: TraceSpan): string {
  if (span.status !== 'running') return span.status;
  if (props.complete) return 'No end event';
  return span.events.some(event => event.kind === 'signal_wait')
    && !span.events.some(event => event.kind === 'signal_delivered') ? 'Waiting for signal' : 'running';
}
</script>

<template>
  <section class="trace-canvas" aria-label="Execution trace">
    <div class="trace-toolbar">
      <p v-if="spans.length" class="trace-overview">{{ spans.length }} calls · {{ durationText(range) }}</p>
      <div class="graph-controls" role="group" aria-label="Trace controls">
        <button type="button" aria-label="Zoom trace out" :disabled="zoom <= 1" @click="zoom = Math.max(1, zoom / 1.5)">−</button>
        <button type="button" aria-label="Zoom trace in" :disabled="zoom >= 8" @click="zoom = Math.min(8, zoom * 1.5)">+</button>
        <button type="button" @click="zoom = 1">Fit trace</button>
      </div>
    </div>
    <p v-if="problem" class="trace-notice" role="status">{{ problem }} <button type="button" @click="emit('retry')">Retry trace</button></p>
    <p v-else-if="!spans.length" class="trace-notice">No calls recorded.</p>
    <div v-if="spans.length" class="trace-scroll" tabindex="0" aria-label="Trace timeline">
      <div class="trace-timeline" :style="{ width: `${zoom * 100}%` }">
        <div class="trace-ruler"><span>Call · status · duration</span><div class="trace-scale"><span v-for="tick in range ? [0, 25, 50, 75, 100] : [0]" :key="tick" :style="{ left: `${tick}%` }">{{ durationText(range * tick / 100) }}</span></div></div>
        <button v-for="{ span, depth } in rows" :key="span.id" class="trace-row" type="button" :aria-pressed="selectedId === span.id" :data-span-id="span.id" :data-span-status="span.status" :title="span.address" @click="emit('select', span.id)">
          <span class="trace-label" :style="{ paddingLeft: `${.5 + depth * .8}rem` }">
            <strong>{{ span.address.split('/').at(-1) }}{{ span.kind === 'attempt' ? ` · attempt ${span.attempt}` : ` · ${span.kind}` }}</strong>
            <small>{{ spanState(span) }} · {{ durationText(spanDuration(span, observedAt)) }}{{ span.finished_at ? '' : ' observed' }}</small>
          </span>
          <span class="trace-track"><span class="trace-bar" :data-status="span.status" :data-zero="spanDuration(span, observedAt) === 0" :style="position(span)"></span></span>
        </button>
      </div>
    </div>
  </section>
</template>
