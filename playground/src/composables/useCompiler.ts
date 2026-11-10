/** WASM owns compilation and type reasoning. The metadata view serves topology
 * and diagnostics; original schema JSON serves Input defaults and validation
 * so enum literals never round-trip through JavaScript numbers.
 */
import { onMounted, ref, shallowRef, watch, type Ref } from 'vue';
import init, { compile_workflow } from '../wasm-pkg/axcutor_wasm';
import type { CompileOutput, CompileRequest, MountUnit } from '../types/compiler';
import { JsonText } from '../utils/losslessJson';

let initialization: Promise<unknown> | undefined;

/** Project submitted snapshots onto the closed compiler request; workbench
 * metadata such as entry_path must stay outside the WASM boundary.
 */
export function compileSource({ entry, mounts }: CompileRequest): CompileOutput {
  return JSON.parse(compile_workflow(JSON.stringify({ entry, mounts }))) as CompileOutput;
}

export function useCompiler(entry: Ref<string>, mounts: Ref<Record<string, MountUnit>>) {
  const ready = ref(false);
  const error = ref<string | null>(null);
  const compileOutput = shallowRef<CompileOutput | null>(null);
  // Schema defaults and validation must keep enum numbers and field order.
  const compileJson = shallowRef<JsonText | null>(null);

  const compile = () => {
    if (!ready.value) return;
    try {
      const raw = compile_workflow(JSON.stringify({ entry: entry.value, mounts: mounts.value }));
      compileJson.value = new JsonText(raw);
      compileOutput.value = JSON.parse(raw) as CompileOutput;
      error.value = null;
    } catch (cause) {
      compileOutput.value = null;
      compileJson.value = null;
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
  watch([entry, mounts], compile, { deep: true });

  return { ready, error, compileOutput, compileJson };
}
