<script setup lang="ts">
import type { Frame } from '../client/types.gen';
import type { RawExecutionSlices } from '../utils/losslessJson';

defineProps<{ frames: Frame[]; slices: RawExecutionSlices; prefix?: string; selectedKey: string | null }>();
const emit = defineEmits<{ select: [frame: Frame, key: string] }>();
function frameKey(prefix: string | undefined, index: number): string { return prefix ? `${prefix}.${index}` : String(index); }
</script>

<template>
  <ul>
    <li v-for="(frame, index) in frames" :key="`${frame.kind}:${frame.address}:${index}`" :data-frame-kind="frame.kind">
      <details open>
        <summary>
          <button type="button" :aria-pressed="selectedKey === `${frame.kind}:${frame.address}`" @click.stop="emit('select', frame, `${frame.kind}:${frame.address}`)">
            {{ frame.kind }} / {{ frame.address }} / {{ frame.cursor }}
          </button>
        </summary>
        <p>call_depth: {{ frame.call_depth }} · steps: {{ frame.steps }} / {{ frame.max_steps ?? 'unlimited' }}</p>
        <pre><code>{{ slices.frameVariablesRaw.get(frameKey(prefix, index)) ?? '{}' }}</code></pre>
        <FrameTree v-if="frame.children.length" :frames="frame.children" :slices="slices" :prefix="frameKey(prefix, index)" :selected-key="selectedKey" @select="(child, key) => emit('select', child, key)" />
      </details>
    </li>
  </ul>
</template>
