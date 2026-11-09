/** WASM owns compilation and type reasoning. The UI consumes this metadata
 * view for topology and diagnostics only; exact business values stay in the
 * execution JSON slices, independently of compilation.
 */
import { onMounted, ref, shallowRef, watch, type Ref } from 'vue';
import init, { compile_workflow } from '../wasm-pkg/axcutor_wasm';
import type { CompileOutput, CompileRequest, SourceDocument } from '../types/compiler';

let initialization: Promise<unknown> | undefined;

export function compileSource(source: CompileRequest): CompileOutput {
  return JSON.parse(compile_workflow(JSON.stringify(source))) as CompileOutput;
}

export function useCompiler(entry: Ref<string>, packages: Ref<Record<string, SourceDocument>>) {
  const ready = ref(false);
  const error = ref<string | null>(null);
  const compileOutput = shallowRef<CompileOutput | null>(null);

  const compile = () => {
    if (!ready.value) return;
    try {
      compileOutput.value = compileSource({ entry: entry.value, packages: packages.value });
      error.value = null;
    } catch (cause) {
      compileOutput.value = null;
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
  watch([entry, packages], compile, { deep: true });

  return { ready, error, compileOutput };
}
