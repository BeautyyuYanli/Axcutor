import { effectScope } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useExecution } from '../src/composables/useExecution';

const view = (status = 'running', output = 'null') => `{"execution_id":"demo","status":"${status}","started_at":"2026-01-01T00:00:00Z","finished_at":${status === 'running' ? 'null' : '"2026-01-01T00:00:01Z"'},"frames":[],"result":${output},"error":null}`;
const response = (raw: string, status = 200) => new Response(raw, { status, headers: { 'content-type': 'application/json' } });
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
      if (request.method === 'POST') return response(view(), 202);
      gets.push(request);
      return new Promise<Response>((resolve, reject) => {
        replies.push(resolve);
        request.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
      });
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main', {}, '{}');
    await vi.advanceTimersByTimeAsync(299);
    expect(gets).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(gets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1200);
    expect(gets).toHaveLength(1);
    expect(gets[0].signal.aborted).toBe(false);

    const running = view().replace('"frames":[]', '"frames":[{"kind":"function","address":"main","cursor":"work","call_depth":1,"steps":1,"max_steps":1000,"variables":{},"children":[]}]');
    replies[0](response(running));
    await vi.advanceTimersByTimeAsync(0);
    expect(state.execution.value?.frames[0]?.cursor).toBe('work');
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

  it('submits raw JSON, polls every 300ms, retains output tokens and stops on terminal', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(view(), 202)).mockResolvedValueOnce(response(view('completed', '{"branch":null,"output":{"2":1.0,"1":999999999999999999999999999999}}')));
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    expect(await state.run('main', {}, '{"2":1.0,"1":999999999999999999999999999999}')).toBe(true);
    expect(await (fetch.mock.calls[0][0] as Request).text()).toContain('"input":{"2":1.0,"1":999999999999999999999999999999}');
    expect(fetch.mock.calls[0][0].url).toBe('http://test/v1/executions');
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
      if (request.method === 'POST') return response(view(), 202);
      return response(view('cancelled'));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main', {}, '{}');
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

  it('reports strict local input and HTTP error envelopes without starting a poll', async () => {
    const fetch = vi.fn().mockResolvedValue(response('{"error":{"code":"input_validation_failed","message":"integer required","details":null}}', 422));
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    expect(await state.run('main', {}, '{"n":1,"n":2}')).toBe(false);
    expect(state.error.value?.code).toBe('invalid_input_json');
    expect(fetch).not.toHaveBeenCalled();
    expect(await state.run('main', {}, '{"n":1.0}')).toBe(false);
    expect(state.error.value).toMatchObject({ status: 422, code: 'input_validation_failed' });
    await sleep(350);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('aborts observation on disposal without sending a cancellation', async () => {
    let signal: AbortSignal | null = null;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === 'POST') return response(view(), 202);
      signal = request.signal;
      return new Promise<Response>((_, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main', {}, '{}');
    await vi.waitFor(() => expect(signal).not.toBeNull());
    scopes[0].stop();
    expect(signal!.aborted).toBe(true);
    expect(fetch.mock.calls.some(([request]) => request.url.endsWith(':cancel'))).toBe(false);
  });

  it('aborts an in-flight poll when cancellation returns terminal and ignores its stale error', async () => {
    let pollSignal: AbortSignal | null = null;
    const fetch = vi.fn().mockImplementation(async (request: Request) => {
      if (request.url.endsWith(':cancel')) return response(view('cancelled'));
      if (request.method === 'POST') return response(view(), 202);
      pollSignal = request.signal;
      return new Promise<Response>((_, reject) => request.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.run('main', {}, '{}');
    await vi.waitFor(() => expect(pollSignal).not.toBeNull());
    await state.cancel();
    expect(pollSignal!.aborted).toBe(true);
    await sleep(50);
    expect(state.execution.value?.status).toBe('cancelled');
    expect(state.error.value).toBeNull();
  });
});
