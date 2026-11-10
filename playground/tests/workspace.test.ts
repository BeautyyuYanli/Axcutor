import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope } from 'vue';
import type { WorkspaceFile } from '../src/client/types.gen';
import { useWorkspace } from '../src/composables/useWorkspace';
import { isWorkflowEntry, workspaceSource } from '../src/utils/workspace';

const file = (path: string, text = ''): WorkspaceFile => ({ path, text, generated: path.endsWith('_contract.py') });
const response = (value: object, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
const scopes: ReturnType<typeof effectScope>[] = [];
function controller() {
  const scope = effectScope();
  scopes.push(scope);
  return scope.run(() => useWorkspace('http://test'))!;
}
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('physical files and browser source preview', () => {
  it('selects package declarations and standalone workflow bodies while excluding input and resource files', () => {
    const files = [
      file('workflows/app/__axcutor__.yaml'),
      file('main.json', '{"input":{},"output":{},"graph":{}}'),
      file('main.yml', 'input: {}\noutput: {}\nrun: {executor: signal}'),
      file('flow.yaml', '{"input": {}, "graph": {}}'),
      file('quoted.yaml', '"run": {executor: signal}'),
      file('input.json', '{"items":[3,1,2],"n":999999999999999999999999999}'),
      file('input.yaml', 'value: {graph: "data", run: "data"}'),
      file('workflows/app/__ax_resources__.yaml', 'resources: {}'),
      file('helper.py', 'graph = {}'),
    ];
    expect(files.filter(isWorkflowEntry).map(file => file.path)).toEqual([
      'workflows/app/__axcutor__.yaml', 'main.json', 'main.yml', 'flow.yaml', 'quoted.yaml',
    ]);
  });

  it('includes resource-only roots and preserves both diagnostic files for colocated headers', () => {
    const files = [
      file('workflows/app/__init__.py'),
      file('workflows/app/__axcutor__.yaml', 'input: {}'),
      file('workflows/app/__ax_resources__.yaml', 'resources: {Tx: null}'),
      file('workflows/infra/__init__.py'),
      file('workflows/infra/__ax_resources__.yaml', 'resources: {Conn: null}'),
    ];
    const source = workspaceSource(files, 'workflows/app/__axcutor__.yaml');
    expect(source.mounts.infra.packages).toEqual({});
    expect(source.mounts.infra.resources?.infra.text).toBe('resources: {Conn: null}');
    expect(source.mounts.app.packages?.app.text).toBe('input: {}');
    expect(source.mounts.app.resources?.app.text).toBe('resources: {Tx: null}');
    expect(source.paths.app).toBe('workflows/app/__axcutor__.yaml');
    expect(source.paths['app:resources']).toBe('workflows/app/__ax_resources__.yaml');
    expect(() => workspaceSource(files.filter(f => f.path !== 'workflows/infra/__init__.py'), 'workflows/app/__axcutor__.yaml')).not.toThrow();
    expect(() => workspaceSource([...files, file('workflows/infra/__ax_resources__/__init__.py'), file('workflows/infra/__ax_resources__/__ax_resources__.yaml')], 'workflows/app/__axcutor__.yaml')).toThrow('Invalid declaration package');
  });

  it('maps package documents and anonymous entries to physical diagnostic paths without importing Python', () => {
    const files = [file('workflows/demo/__init__.py', 'raise RuntimeError()'), file('workflows/demo/main/__init__.py'), file('workflows/demo/main/__axcutor__.yaml', 'input: {}'), file('scratch/main.json', '{}')];
    const named = workspaceSource(files, 'workflows/demo/main/__axcutor__.yaml');
    expect(named.entry).toBe('demo.main');
    expect(named.paths['demo.main']).toBe('workflows/demo/main/__axcutor__.yaml');
    expect(named.mounts.demo.packages!['demo.main']).toEqual({ format: 'yaml', text: 'input: {}' });
    const anonymous = workspaceSource(files, 'scratch/main.json');
    expect(anonymous.entry).toBe('__main__');
    expect(anonymous.paths.__main__).toBe('scratch/main.json');
    expect(anonymous.mounts.__main__.packages!.__main__).toEqual({ format: 'json', text: '{}' });
    expect(() => workspaceSource(files.filter(f => f.path !== 'workflows/demo/main/__init__.py'), 'scratch/main.json')).toThrow('Python package');
    expect(() => workspaceSource([...files, file('workflows/demo.py')], 'scratch/main.json')).toThrow('Python package');
  });

  it('accepts saved files and keeps local edits and revision on conflict', async () => {
    let reply: (result: Response) => void = () => {};
    const fetch = vi.fn().mockResolvedValueOnce(response({ revision: 'r1', files: [file('input.json', '{"n":1.0}')], busy: false, execution_id: null }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { reply = resolve; }))
      .mockResolvedValueOnce(response({ error: { code: 'workspace_conflict', message: 'reload', details: null } }, 409));
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.load();
    expect(state.dirty.value).toBe(false);
    state.files.value[0].text = '{"n":2.0}';
    const pending = state.save();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(state.busy.value).toBe(true);
    reply(response({ revision: 'r2', files: [file('input.json', '{"n":2.0}')], busy: false, execution_id: null }));
    expect(await pending).toBe('r2');
    expect(state.files.value[0].text).toBe('{"n":2.0}');
    expect(state.dirty.value).toBe(false);
    state.files.value[0].text = '{"n":3.0}';
    expect(await state.save()).toBeNull();
    expect(state.revision.value).toBe('r2');
    expect(state.files.value[0].text).toBe('{"n":3.0}');
    expect(state.problem.value).toContain('Your edits are kept');
  });

  it('accepts generated files and revision and waits for server cleanup', async () => {
    vi.useFakeTimers();
    const initial = { revision: 'r1', files: [file('main.py', 'saved'), file('old.py'), file('_contract.py', 'old')], busy: false, execution_id: null };
    const generated = { revision: 'r2', files: [file('main.py', 'saved'), file('old.py'), file('_contract.py', 'new'), file('extra_contract.py', 'extra')], busy: true, execution_id: 'run' };
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response(generated)) // Terminal Temporal root, still cleaning locally.
      .mockResolvedValueOnce(response({ ...generated, busy: false, execution_id: null }))
      .mockImplementationOnce(async (request: Request) => {
        const body = JSON.parse(await request.text());
        expect(body.revision).toBe('r2');
        expect(body.files).toContainEqual({ path: 'main.py', text: 'edited after cleanup' });
        expect(body.files).toContainEqual({ path: '_contract.py', text: 'new' });
        return response({ revision: 'r3', files: body.files.map((f: WorkspaceFile) => file(f.path, f.text)), busy: false, execution_id: null });
      });
    vi.stubGlobal('fetch', fetch);
    const state = controller();
    await state.load();
    state.accept(generated);
    expect(state.revision.value).toBe('r2');
    expect(state.files.value).toEqual(generated.files);
    expect(state.dirty.value).toBe(false);
    expect(state.files.value.find(f => f.path === 'extra_contract.py')?.text).toBe('extra');
    expect(await state.save()).toBeNull();
    await vi.advanceTimersByTimeAsync(300);
    expect(state.serverBusy.value).toBe(true);
    await vi.advanceTimersByTimeAsync(300);
    expect(state.serverBusy.value).toBe(false);
    state.files.value[0].text = 'edited after cleanup';
    expect(await state.save()).toBe('r3');
  });
});
