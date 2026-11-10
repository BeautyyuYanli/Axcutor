/** One observed execution per workbench. Temporal owns execution state.
 * Polls never overlap, stale responses cannot overwrite a terminal state, and
 * disposing aborts observation requests only (it never cancels server work).
 * A submission 503 with an ID keeps observation/cancellation available without
 * inventing an Execution view or submitting the workflow again.
 * Hey API's SDK signatures do not narrow data for parseAs:'text'; conversion at
 * this boundary is explicit and every business payload stays in JsonText.
 */
import { onScopeDispose, ref, shallowRef } from 'vue';
import { createClient } from '../client/client';
import { runWorkspace, getExecution, cancelExecution } from '../client/sdk.gen';
import type { ErrorResponse, Execution, SubmittedSource, Workspace } from '../client/types.gen';
import type { Diagnostic } from '../types/compiler';
import { executionSlices, JsonText, type RawExecutionSlices } from '../utils/losslessJson';

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
  const observedId = ref<string | null>(null);
  const slices = shallowRef<RawExecutionSlices>({ frameVariablesRaw: new Map(), resultOutputRaw: null });
  const error = shallowRef<ExecutionProblem | null>(null);
  const source = shallowRef<SubmittedSource | null>(null);
  const workspace = shallowRef<Workspace | null>(null);
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
    observedId.value = next.status === 'running' ? next.execution_id : null;
    slices.value = executionSlices(tokens);
    error.value = null;
    if (next.status !== 'running') { clearTimer(); abortRequests(); cancelRequested.value = false; }
    return true;
  }

  async function request(
    operation: (signal: AbortSignal) => Promise<{ response?: Response; error?: unknown }>,
    requestGeneration: number,
    submission = false,
  ): Promise<boolean> {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const result = await operation(controller.signal);
      if (disposed || generation !== requestGeneration || controller.signal.aborted) return false;
      const raw = result.response && rawResponses.get(result.response);
      if (result.response?.ok && raw !== undefined) {
        if (submission) {
          const tokens = new JsonText(raw);
          source.value = JSON.parse(tokens.raw(['source'])!) as SubmittedSource;
          workspace.value = JSON.parse(tokens.raw(['workspace'])!) as Workspace;
          return accept(tokens.raw(['execution'])!, requestGeneration);
        }
        return accept(raw, requestGeneration);
      }
      const tokens = raw ? new JsonText(raw) : null;
      const envelope = tokens ? JSON.parse(raw!) as ErrorResponse : null;
      if (submission && envelope?.error?.details?.workspace) {
        workspace.value = envelope.error.details.workspace as Workspace;
      }
      const id = envelope?.error?.details?.execution_id;
      if (submission && result.response?.status === 503 && envelope?.error?.code === 'runtime_unavailable'
        && typeof id === 'string' && id.length > 0) {
        execution.value = null;
        source.value = null;
        slices.value = { frameVariablesRaw: new Map(), resultOutputRaw: null };
        observedId.value = id;
      }
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
    if (disposed || polling || timer !== undefined || !observedId.value) return;
    timer = setTimeout(() => { timer = undefined; void poll(); }, 300);
  }

  async function poll() {
    const id = observedId.value;
    if (!id || disposed || polling) return;
    polling = true;
    try {
      await request(signal => getExecution({ client, path: { execution_id: id }, signal }), generation);
    } finally { polling = false; schedule(); }
  }

  async function run(entry: string, inputRaw = '{}', revision: string) {
    if (disposed || submitting.value || observedId.value) return false;
    // Validate the envelope boundary even for callers outside the editor. Never
    // turn business numbers or integer-like keys into JavaScript values.
    try {
      if (new JsonText(inputRaw).root.kind !== 'object') throw new TypeError('Input must be a JSON object');
    } catch (cause) {
      error.value = { status: null, code: 'invalid_input', message: String(cause), detailsRaw: null, diagnostics: [] };
      return false;
    }
    const body = { entry, revision };
    generation++;
    clearTimer();
    abortRequests();
    error.value = null;
    workspace.value = null;
    cancelRequested.value = false;
    submitting.value = true;
    try {
      const accepted = await request(signal => runWorkspace({
        client, body, signal,
        bodySerializer: () => `{"entry":${JSON.stringify(entry)},"input":${inputRaw},"revision":${JSON.stringify(revision)}}`,
      }), generation, true);
      schedule();
      return accepted;
    } finally { submitting.value = false; }
  }

  async function cancel() {
    const id = observedId.value;
    if (disposed || !id || cancelRequested.value) return;
    cancelRequested.value = true;
    const requestGeneration = generation;
    const accepted = await request(signal => cancelExecution({
      client, path: { execution_id: id }, signal,
    }), requestGeneration);
    if (generation !== requestGeneration || disposed) return;
    if (!accepted && observedId.value) cancelRequested.value = false;
    schedule();
  }

  function restore(raw: string | null, id: string, submittedSource: SubmittedSource) {
    if (disposed || submitting.value || execution.value || observedId.value) return;
    source.value = submittedSource;
    if (raw && raw !== 'null') accept(raw, generation);
    else observedId.value = id;
    schedule();
  }

  return { execution, observedId, slices, error, source, workspace, submitting, cancelRequested, run, cancel, restore };
}
