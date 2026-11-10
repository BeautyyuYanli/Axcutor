import { effectScope } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useExecution } from '../src/composables/useExecution';

const view = (status = 'running', output = 'null') => `{"execution_id":"demo","status":"${status}","started_at":"2026-01-01T00:00:00Z","finished_at":${status === 'running' ? 'null' : '"2026-01-01T00:00:01Z"'},"frames":[],"result":${output},"error":null}`;
const response = (raw: string, status = 200) => new Response(raw, { status, headers: { 'content-type': 'application/json' } });
const submitted = (raw: string) => `{"execution":${raw},"source":{"entry":"demo.main","mounts":{}},"workspace":{"revision":"generated","files":[],"busy":true,"execution_id":"demo"}}`;
const uncertainSubmission = JSON.stringify({
  error: {
    code: 'runtime_unavailable',
    message: 'submission outcome unknown; observe or cancel this ID, do not resubmit',
    details: {
      execution_id: 'demo',
      workspace: { revision: 'generated', files: [], busy: true, execution_id: 'demo' },
    },
  },
});
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const scopes: ReturnType<typeof effectScope>[] = [];
function controller() {
  const scope = effectScope();
  scopes.push(scope);
  return scope.run(() => useExecution('http://test'))!;
}
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('generated Hey API execution boundary', () => {
  it('waits for a slow poll before scheduling the next 300ms query and stops at terminal', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    const gets: Request[] = [];
    const replies: ((result: Response) => void)[] = [];
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === 'POST') return response(submitted(view()), 202);
      gets.push(request);
      return new Promise<Response>((resolve, reject) => {
        replies.push(resolve);
        request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
      });
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main.yaml', null, 'revision');
    await vi.advanceTimersByTimeAsync(299);
    expect(gets).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(gets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1200);
    expect(gets).toHaveLength(1);
    expect(gets[0].signal.aborted).toBe(false);

    const running = view().replace('"frames":[]', '"frames":[{"kind":"function","address":"main","cursor":"work","waiting":["main/work"],"call_depth":1,"steps":1,"max_steps":1000,"variables":{},"children":[]}]');
    replies[0](response(running));
    await vi.advanceTimersByTimeAsync(0);
    expect(state.execution.value?.frames[0]?.cursor).toBe('work');
    expect(state.execution.value?.frames[0]?.waiting).toEqual(['main/work']);
    await vi.advanceTimersByTimeAsync(299);
    expect(gets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(gets).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(900);
    expect(gets).toHaveLength(2);

    replies[1](response(view('completed', '{"branch":null,"output":{}}')));
    await vi.advanceTimersByTimeAsync(0);
    expect(state.execution.value?.status).toBe('completed');
    await vi.advanceTimersByTimeAsync(1200);
    expect(gets).toHaveLength(2);
    scopes[0].stop();
    await vi.advanceTimersByTimeAsync(1200);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(state.error.value).toBeNull();
  });

  it('submits file paths, polls every 300ms, retains output tokens and stops on terminal', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(submitted(view()), 202)).mockResolvedValueOnce(response(view('completed', '{"branch":null,"output":{"2":1.0,"1":999999999999999999999999999999}}')));
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    expect(await state.run('main.yaml', 'input.json', 'revision')).toBe(true);
    expect(JSON.parse(await (fetch.mock.calls[0][0] as Request).text())).toEqual({ entry: 'main.yaml', input: 'input.json', revision: 'revision' });
    expect(fetch.mock.calls[0][0].url).toBe('http://test/v1/workspace/executions');
    expect(state.source.value?.entry).toBe('demo.main');
    await vi.waitFor(() => expect(state.execution.value?.status).toBe('completed'), { timeout: 2000 });
    expect(state.slices.value.resultOutputRaw).toContain('1.0');
    expect(state.slices.value.resultOutputRaw).toContain('999999999999999999999999999999');
    await sleep(350);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][0].url).toBe('http://test/v1/executions/demo');
  });

  it('continues polling after 202 cancellation until cleanup reaches its terminal state', async () => {
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.url.endsWith(':cancel')) return response(view(), 202);
      if (request.method === 'POST') return response(submitted(view()), 202);
      return response(view('cancelled'));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main.yaml', null, 'revision');
    await state.cancel();
    expect(state.cancelRequested.value).toBe(true);
    expect(state.execution.value?.status).toBe('running');
    await state.cancel();
    expect(fetch.mock.calls.filter(([request]) => request.url.endsWith(':cancel'))).toHaveLength(1);
    await vi.waitFor(() => expect(state.execution.value?.status).toBe('cancelled'), { timeout: 2000 });
    expect(state.cancelRequested.value).toBe(false);
    await sleep(350);
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('observes an uncertain submission by ID once and stops after a real terminal response', async () => {
    vi.useFakeTimers();
    let queries = 0;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === 'POST') return response(uncertainSubmission, 503);
      queries++;
      return response(queries === 1 ? view() : view('completed', '{"branch":null,"output":{"n":1.00000000000000000000001}}'));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    expect(await state.run('main.yaml', null, 'revision')).toBe(false);
    expect(state.observedId.value).toBe('demo');
    expect(state.execution.value).toBeNull();
    expect(state.source.value).toBeNull();
    expect(state.workspace.value?.busy).toBe(true);
    expect(await state.run('main.yaml', null, 'revision')).toBe(false);
    await vi.advanceTimersByTimeAsync(300);
    expect(state.execution.value?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(300);
    expect(state.execution.value?.status).toBe('completed');
    expect(state.observedId.value).toBeNull();
    expect(state.slices.value.resultOutputRaw).toContain('1.00000000000000000000001');
    await vi.advanceTimersByTimeAsync(1200);
    expect(fetch.mock.calls.map(([request]) => [request.method, request.url])).toEqual([
      ['POST', 'http://test/v1/workspace/executions'],
      ['GET', 'http://test/v1/executions/demo'],
      ['GET', 'http://test/v1/executions/demo'],
    ]);
  });

  it('can cancel by the uncertain ID even when its first query is still unavailable', async () => {
    vi.useFakeTimers();
    let queries = 0;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.url.endsWith(':cancel')) return response(view(), 202);
      if (request.method === 'POST') return response(uncertainSubmission, 503);
      queries++;
      return queries === 1
        ? response('{"error":{"code":"runtime_unavailable","message":"query unavailable","details":null}}', 503)
        : response(view('cancelled'));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main.yaml', null, 'revision');
    await vi.advanceTimersByTimeAsync(300);
    expect(state.execution.value).toBeNull();
    expect(state.observedId.value).toBe('demo');
    expect(state.error.value?.status).toBe(503);
    await state.cancel();
    expect(state.cancelRequested.value).toBe(true);
    expect(state.observedId.value).toBe('demo');
    expect(state.workspace.value?.busy).toBe(true);
    await state.cancel();
    await vi.advanceTimersByTimeAsync(300);
    expect(state.execution.value?.status).toBe('cancelled');
    expect(state.observedId.value).toBeNull();
    expect(state.cancelRequested.value).toBe(false);
    await vi.advanceTimersByTimeAsync(1200);
    expect(fetch.mock.calls.map(([request]) => [request.method, request.url])).toEqual([
      ['POST', 'http://test/v1/workspace/executions'],
      ['GET', 'http://test/v1/executions/demo'],
      ['POST', 'http://test/v1/executions/demo:cancel'],
      ['GET', 'http://test/v1/executions/demo'],
    ]);
  });

  it.each([
    { name: 'input error', status: 422, code: 'input_validation_failed', raw: '{"error":{"code":"input_validation_failed","message":"integer required","details":null}}' },
    { name: 'unavailable response without ID', status: 503, code: 'runtime_unavailable', raw: '{"error":{"code":"runtime_unavailable","message":"unavailable","details":null}}' },
    { name: 'empty response', status: 503, code: 'network_error', raw: '' },
  ])('reports $name without an ID and sends no queries or cancellation', async ({ raw, status, code }) => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(response(raw, status));
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    expect(await state.run('main.yaml', 'input.json', 'revision')).toBe(false);
    expect(state.error.value).toMatchObject({ status, code });
    expect(state.execution.value).toBeNull();
    expect(state.observedId.value).toBeNull();
    await state.cancel();
    await vi.advanceTimersByTimeAsync(1200);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('aborts observation on disposal without sending a cancellation', async () => {
    let signal: AbortSignal | null = null;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === 'POST') return response(submitted(view()), 202);
      signal = request.signal;
      return new Promise<Response>((_, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main.yaml', null, 'revision');
    await vi.waitFor(() => expect(signal).not.toBeNull());
    scopes[0].stop();
    expect(signal!.aborted).toBe(true);
    expect(fetch.mock.calls.some(([request]) => request.url.endsWith(':cancel'))).toBe(false);
  });

  it('aborts an in-flight poll when cancellation returns terminal and ignores its stale error', async () => {
    let pollSignal: AbortSignal | null = null;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.url.endsWith(':cancel')) return response(view('cancelled'));
      if (request.method === 'POST') return response(submitted(view()), 202);
      pollSignal = request.signal;
      return new Promise<Response>((_, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main.yaml', null, 'revision');
    await vi.waitFor(() => expect(pollSignal).not.toBeNull());
    await state.cancel();
    expect(pollSignal!.aborted).toBe(true);
    await sleep(50);
    expect(state.execution.value?.status).toBe('cancelled');
    expect(state.error.value).toBeNull();
  });
});
