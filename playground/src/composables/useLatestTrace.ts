/** Observe one process-local trace independently of Execution and cleanup.
 * Every response is pinned to an ID/generation; trace outages never invent an
 * execution failure. Exact business payloads are read only from JsonText.
 */
import { onScopeDispose, ref, shallowRef } from 'vue';
import { createClient } from '../client/client';
import { getLatestTrace } from '../client/sdk.gen';
import type { LatestTrace } from '../client/types.gen';
import { JsonText } from '../utils/losslessJson';

export interface TraceView {
  metadata: LatestTrace;
  json: JsonText;
}

export function useLatestTrace(baseUrl = globalThis.location?.origin ?? 'http://localhost') {
  const client = createClient({ baseUrl, parseAs: 'text', responseStyle: 'fields' });
  const view = shallowRef<TraceView | null>(null);
  const problem = ref<string | null>(null);
  let id: string | null = null;
  let generation = 0;
  let disposed = false;
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  onScopeDispose(() => { disposed = true; generation++; controller?.abort(); clearTimeout(timer); });

  async function fetchTrace(expected: string | null, version: number): Promise<TraceView | null> {
    const request = new AbortController();
    controller = request;
    try {
      const result = await getLatestTrace({ client, query: expected ? { execution_id: expected } : {}, signal: request.signal });
      if (disposed || version !== generation || request.signal.aborted) return null;
      const raw: unknown = result.data;
      if (!result.response?.ok || typeof raw !== 'string') throw new Error('Trace unavailable. Observation will retry.');
      if (raw === 'null') { view.value = null; problem.value = null; return null; }
      const json = new JsonText(raw);
      const metadata = JSON.parse(raw) as LatestTrace;
      if (!metadata.execution_id || (expected && metadata.execution_id !== expected)) throw new Error('Trace ID changed.');
      id = metadata.execution_id;
      view.value = { metadata, json };
      problem.value = metadata.trace_error;
      return view.value;
    } catch (cause) {
      if (!disposed && version === generation && !request.signal.aborted) problem.value = String(cause);
      return null;
    } finally {
      if (!disposed && version === generation && id && !view.value?.metadata.complete) {
        timer = setTimeout(() => { timer = undefined; void fetchTrace(id, generation); }, 300);
      }
    }
  }

  function track(next: string) {
    if (id === next) return;
    generation++;
    controller?.abort();
    clearTimeout(timer);
    id = next;
    view.value = null;
    problem.value = null;
    void fetchTrace(next, generation);
  }

  function retry() {
    generation++;
    controller?.abort();
    clearTimeout(timer);
    void fetchTrace(id, generation);
  }

  async function restore() { return await fetchTrace(null, generation); }
  return { view, problem, track, retry, restore };
}
