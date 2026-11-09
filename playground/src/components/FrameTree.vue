<script setup lang="ts">
import type { Frame } from '../client/types.gen';
import type { ExecutionStep } from '../utils/executionView';

defineProps<{ steps: ExecutionStep[]; selectedKey: string | null }>();
const emit = defineEmits<{ select: [frame: Frame, key: string] }>();
</script>

<template>
  <ul class="frame-tree">
    <li v-for="step in steps" :key="step.key" :data-frame-kind="step.frame.kind">
      <button type="button" :aria-pressed="selectedKey === step.key" @click="emit('select', step.frame, step.key)">
        {{ step.label }}{{ step.frame.waiting.length ? ' · Waiting for signal' : '' }}
      </button>
      <FrameTree v-if="step.children.length" :steps="step.children" :selected-key="selectedKey" @select="(child, key) => emit('select', child, key)" />
    </li>
  </ul>
</template>
