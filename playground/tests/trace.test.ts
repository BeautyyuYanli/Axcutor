import { effectScope } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useLatestTrace } from '../src/composables/useLatestTrace';
import { useExecution } from '../src/composables/useExecution';
import { traceRows, spanDuration, durationText } from '../src/utils/traceView';
import type { TraceSpan } from '../src/client/types.gen';

const scopes: ReturnType<typeof effectScope>[] = [];
const response = (raw: string, status = 200) => new Response(raw, { status, headers: { 'content-type': 'application/json' } });
const execution = (id: string) => `{"execution_id":"${id}","status":"completed","frames":[],"started_at":"2026-01-01T00:00:00Z","finished_at":"2026-01-01T00:00:01Z","result":{"branch":null,"output":{}},"error":null}`;
const exactValue = '{"2":99999999999999999999999999999999999,"1":1.00000000000000000000001}';
const record = (id: string, complete = true) => `{"execution_id":"${id}","execution":${execution(id)},"source":{"entry":"${id}","mounts":{}},`
  + `"spans":[{"id":"${id}:1","parent_id":null,"address":"${id}/copy","kind":"builtin","frame_kind":"iteration","cursor":"copy",`
  + `"status":"completed","started_at":"2026-01-01T00:00:00Z","finished_at":"2026-01-01T00:00:00Z","input":{"source":${exactValue}},"output":{"value":${exactValue}},`
  + `"branch":null,"error":null,"events":[]}],"observed_at":"2026-01-01T00:00:01Z","complete":${complete},"trace_error":null}`;
function inScope<T>(run: () => T): T { const scope = effectScope(); scopes.push(scope); return scope.run(run)!; }
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('latest trace observation', () => {
  it('restores one completed run and keeps exact Input/Output tokens without another request', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(response(record('first')));
    vi.stubGlobal('fetch', fetch);
    const state = inScope(() => useLatestTrace('http://test'));
    const restored = await state.restore();
    expect(restored?.metadata.execution_id).toBe('first');
    const input = restored!.json.format(['spans', 0, 'input'])!;
    const output = restored!.json.format(['spans', 0, 'output'])!;
    expect(input).toContain('99999999999999999999999999999999999');
    expect(output).toContain('1.00000000000000000000001');
    expect(input.indexOf('"2"')).toBeLessThan(input.indexOf('"1"'));
    state.track('first');
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(durationText(spanDuration(restored!.metadata.spans[0], null))).toBe('0 ms');
  });

  it('isolates a new ID from late requests, source and trace, and retries observation failures', async () => {
    vi.useFakeTimers();
    let release: ((value: Response) => void) | undefined;
    let secondReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      if (request.url.includes('first')) return new Promise<Response>(resolve => { release = resolve; });
      secondReads++;
      return secondReads === 1 ? response('{}', 503) : response(record('second'));
    }));
    const state = inScope(() => useLatestTrace('http://test'));
    state.track('first');
    await vi.advanceTimersByTimeAsync(0);
    state.track('second');
    await vi.advanceTimersByTimeAsync(0);
    release!(response(record('first')));
    await vi.advanceTimersByTimeAsync(0);
    expect(state.view.value).toBeNull();
    expect(state.problem.value).toContain('Trace unavailable');
    await vi.advanceTimersByTimeAsync(300);
    expect(state.view.value?.metadata.source.entry).toBe('second');
    expect(state.view.value?.metadata.spans[0].id).toBe('second:1');
    expect(state.problem.value).toBeNull();
  });

  it('preserves the latest Execution/source/output after rejected preflight', async () => {
    vi.useFakeTimers();
    const submitted = `{"execution":${execution('first')},"source":{"entry":"first","mounts":{}},"workspace":{"revision":"r1","files":[],"busy":false,"execution_id":null}}`;
    const fetch = vi.fn().mockResolvedValueOnce(response(submitted, 202)).mockResolvedValueOnce(response('{"error":{"code":"input_validation_failed","message":"invalid input","details":null}}', 422));
    vi.stubGlobal('fetch', fetch);
    const state = inScope(() => useExecution('http://test'));
    await state.run('first.yaml', null, 'r1');
    expect(await state.run('next.yaml', 'bad.json', 'r2')).toBe(false);
    expect(state.execution.value?.execution_id).toBe('first');
    expect(state.source.value?.entry).toBe('first');
    expect(state.slices.value.resultOutputRaw).toBe('{}');
    expect(state.error.value?.code).toBe('input_validation_failed');
  });
});

it('keeps repeated addresses as independent children with their actual durations', () => {
  const root = JSON.parse(record('run')).spans[0] as TraceSpan;
  const spans = [root, { ...root, id: 'a', parent_id: root.id }, { ...root, id: 'b', parent_id: root.id, finished_at: '2026-01-01T00:00:00.007Z' }];
  expect(traceRows(spans).map(row => [row.span.id, row.depth])).toEqual([[root.id, 0], ['a', 1], ['b', 1]]);
  expect(spanDuration(spans[1], null)).toBe(0);
  expect(spanDuration(spans[2], null)).toBe(7);
});
