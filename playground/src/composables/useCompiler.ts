/** WASM owns compilation and all type reasoning. Keep its original analysis
 * JSON for subsequent queries: enum literals and object order must not pass
 * through JavaScript's numeric/object serialization.
 */
import { computed, onMounted, ref, shallowRef, watch, type Ref } from 'vue';
import init, { compile_workflow, get_available_vars_at_node } from '../wasm-pkg/axcutor_wasm';
import type { CompileOutput, SourceDocument, VariableCandidate } from '../types/compiler';
import { JsonText } from '../utils/losslessJson';

let initialization: Promise<unknown> | undefined;

export function useCompiler(entry: Ref<string>, sources: Ref<Record<string, SourceDocument>>) {
  const ready = ref(false);
  const error = ref<string | null>(null);
  const compileOutput = shallowRef<CompileOutput | null>(null);
  const rawOutput = shallowRef<JsonText | null>(null);
  const analyses = computed(() => compileOutput.value?.analysis ?? []);

  const compile = () => {
    if (!ready.value) return;
    try {
      const raw = compile_workflow(JSON.stringify({ entry: entry.value, sources: sources.value }));
      rawOutput.value = new JsonText(raw);
      compileOutput.value = JSON.parse(raw) as CompileOutput;
      error.value = null;
    } catch (cause) {
      compileOutput.value = null;
      rawOutput.value = null;
      error.value = String(cause);
    }
  };

  onMounted(async () => {
    try {
      initialization ??= init();
      await initialization;
      ready.value = true;
      compile();
    } catch (cause) { error.value = `WASM initialization failed: ${String(cause)}`; }
  });
  watch([entry, sources], compile, { deep: true });

  function availableVariables(analysisJson: string, nodeId: string, expected: string): {
    candidate: VariableCandidate; typespecRaw: string;
  }[] {
    if (!ready.value) return [];
    const result = get_available_vars_at_node(analysisJson, nodeId, expected ? JSON.stringify({ type: expected }) : undefined);
    const tokens = new JsonText(result);
    const candidates = JSON.parse(result) as VariableCandidate[];
    return candidates.map((candidate, i) => ({
      candidate, typespecRaw: tokens.format([i, 'typespec']) ?? '',
    }));
  }

  return { ready, error, compileOutput, rawOutput, analyses, availableVariables };
}
