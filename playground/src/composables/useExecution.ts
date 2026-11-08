/** One observed execution per workbench. The host remains the state owner.
 * Polls never overlap, stale responses cannot overwrite a terminal state, and
 * disposing aborts observation requests only (it never cancels server work).
 * Hey API's SDK signatures do not narrow data for parseAs:'text'; conversion at
 * this boundary is explicit and every business payload stays in JsonText.
 */
import { onScopeDispose, ref, shallowRef } from 'vue';
import { createClient } from '../client/client';
import { submitExecution, getExecution, cancelExecution } from '../client/sdk.gen';
import type { ErrorResponse, Execution } from '../client/types.gen';
import type { Diagnostic, SourceDocument } from '../types/compiler';
import { executionSlices, JsonText, serializeSubmitBody, type RawExecutionSlices } from '../utils/losslessJson';

export interface ExecutionProblem {
  status: number | null;
  code: string;
  message: string;
  detailsRaw: string | null;
  diagnostics: Diagnostic[];
}

export function useExecution(baseUrl = globalThis.location?.origin ?? 'http://localhost') {
  const client = createClient({ baseUrl, parseAs: 'text', responseStyle: 'fields' });
  const rawResponses = new WeakMap<Response, string>();
  client.interceptors.response.use(async response => {
    rawResponses.set(response, await response.clone().text());
    return response;
  });
  const execution = shallowRef<Execution | null>(null);
  const slices = shallowRef<RawExecutionSlices>({ frameVariablesRaw: new Map(), resultOutputRaw: null });
  const error = shallowRef<ExecutionProblem | null>(null);
  const submitting = ref(false);
  const cancelRequested = ref(false);
  const controllers = new Set<AbortController>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let generation = 0;
  let disposed = false;
  let polling = false;

  const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
  const abortRequests = () => { controllers.forEach(controller => controller.abort()); controllers.clear(); };
  onScopeDispose(() => { disposed = true; generation++; clearTimer(); abortRequests(); });

  function accept(raw: string, requestGeneration: number): boolean {
    if (disposed || generation !== requestGeneration) return false;
    const tokens = new JsonText(raw);
    // Read only topology/control fields from this metadata view. Variables and
    // output in the view are intentionally never rendered or sent elsewhere.
    const next = JSON.parse(raw) as Execution;
    if (!next.execution_id || !['running', 'completed', 'failed', 'cancelled'].includes(next.status)) {
      throw new TypeError('Invalid execution response');
    }
    if (execution.value?.execution_id === next.execution_id && execution.value.status !== 'running') return false;
    execution.value = next;
    slices.value = executionSlices(tokens);
    error.value = null;
    if (next.status !== 'running') { clearTimer(); abortRequests(); cancelRequested.value = false; }
    return true;
  }

  async function request(
    operation: (signal: AbortSignal) => Promise<{ response?: Response; error?: unknown }>,
    requestGeneration: number,
  ): Promise<boolean> {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const result = await operation(controller.signal);
      if (disposed || generation !== requestGeneration || controller.signal.aborted) return false;
      const raw = result.response && rawResponses.get(result.response);
      if (result.response?.ok && raw !== undefined) return accept(raw, requestGeneration);
      const tokens = raw ? new JsonText(raw) : null;
      const envelope = tokens ? JSON.parse(raw!) as ErrorResponse : null;
      error.value = {
        status: result.response?.status ?? null,
        code: envelope?.error?.code ?? 'network_error',
        message: envelope?.error?.message ?? String(result.error ?? 'Request failed'),
        detailsRaw: tokens?.format(['error', 'details']) ?? null,
        diagnostics: envelope?.error?.details?.diagnostics ?? [],
      };
    } catch (cause) {
      if (!disposed && generation === requestGeneration && !controller.signal.aborted) error.value = {
        status: null, code: 'response_error', message: String(cause), detailsRaw: null, diagnostics: [],
      };
    } finally { controllers.delete(controller); }
    return false;
  }

  function schedule() {
    if (disposed || polling || timer !== undefined || execution.value?.status !== 'running') return;
    timer = setTimeout(() => { timer = undefined; void poll(); }, 300);
  }

  async function poll() {
    const id = execution.value?.execution_id;
    if (!id || disposed || polling || execution.value?.status !== 'running') return;
    polling = true;
    try {
      await request(signal => getExecution({ client, path: { execution_id: id }, signal }), generation);
    } finally { polling = false; schedule(); }
  }

  async function run(entry: string, sources: Record<string, SourceDocument>, inputRawText: string) {
    if (disposed || submitting.value || execution.value?.status === 'running') return false;
    // Snapshot before the first await: editors can continue changing independently.
    const body = { entry, sources: Object.fromEntries(Object.entries(sources).map(([path, source]) => [
      path, { format: source.format, text: source.text },
    ])) };
    let serialized: string;
    try { serialized = serializeSubmitBody(body, inputRawText); }
    catch (cause) {
      error.value = { status: null, code: 'invalid_input_json', message: String(cause), detailsRaw: null, diagnostics: [] };
      return false;
    }
    generation++;
    clearTimer();
    abortRequests();
    execution.value = null;
    slices.value = { frameVariablesRaw: new Map(), resultOutputRaw: null };
    error.value = null;
    cancelRequested.value = false;
    submitting.value = true;
    try {
      const accepted = await request(signal => submitExecution({
        client, body, bodySerializer: () => serialized, signal,
      }), generation);
      schedule();
      return accepted;
    } finally { submitting.value = false; }
  }

  async function cancel() {
    if (disposed || execution.value?.status !== 'running' || cancelRequested.value) return;
    cancelRequested.value = true;
    const requestGeneration = generation;
    const id = execution.value.execution_id;
    const accepted = await request(signal => cancelExecution({
      client, path: { execution_id: id }, signal,
    }), requestGeneration);
    if (generation !== requestGeneration || disposed) return;
    if (!accepted && execution.value?.status === 'running') cancelRequested.value = false;
    schedule();
  }

  return { execution, slices, error, submitting, cancelRequested, run, cancel };
}
