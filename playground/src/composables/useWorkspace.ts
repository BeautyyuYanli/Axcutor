/** Shared persistent files with revision-checked whole-workspace saves.
 * File editing is locked during requests and execution. Save and Run responses
 * replace the file view and revision; failed saves preserve local edits.
 */
import { computed, getCurrentScope, onScopeDispose, ref } from 'vue';
import { createClient } from '../client/client';
import { getWorkspace, saveWorkspace } from '../client/sdk.gen';
import type { Workspace, WorkspaceFile } from '../client/types.gen';
import { generatedPath, validSourcePath } from '../utils/workspace';

const fingerprint = (files: WorkspaceFile[]) => JSON.stringify(files.map(({ path, text }) => [path, text]).sort(([a], [b]) => a.localeCompare(b)));

export function useWorkspace(baseUrl = globalThis.location?.origin ?? 'http://localhost') {
  const client = createClient({ baseUrl });
  const files = ref<WorkspaceFile[]>([]);
  const revision = ref('');
  const baseline = ref('');
  const busy = ref(false);
  const serverBusy = ref(false);
  const problem = ref<string | null>(null);
  const dirty = computed(() => fingerprint(files.value) !== baseline.value);
  const loaded = ref(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  if (getCurrentScope()) onScopeDispose(() => { disposed = true; clearTimeout(timer); });

  function schedule() {
    if (!disposed && loaded.value && timer === undefined) {
      timer = setTimeout(() => { timer = undefined; void observe(); }, serverBusy.value ? 300 : 1000);
    }
  }

  // Status polling never adopts another page's files/revision over local edits.
  async function observe() {
    try {
      const result = await getWorkspace({ client });
      if (!disposed && result.data) serverBusy.value = result.data.busy;
    } catch {
      // A transient read failure cannot prove that cleanup has finished.
    } finally { schedule(); }
  }

  function accept(view: Workspace) {
    files.value = view.files;
    revision.value = view.revision;
    baseline.value = fingerprint(view.files);
    loaded.value = true;
    serverBusy.value = view.busy;
    clearTimeout(timer);
    timer = undefined;
    schedule();
  }

  async function load() {
    if (busy.value) return;
    busy.value = true;
    try {
      const result = await getWorkspace({ client });
      if (!result.data) throw new Error(result.error?.error?.message ?? 'Workspace request failed');
      accept(result.data);
      problem.value = null;
    } catch (cause) { problem.value = String(cause); }
    finally { busy.value = false; }
  }

  async function save(): Promise<string | null> {
    if (busy.value || serverBusy.value || !loaded.value) return null;
    busy.value = true;
    const snapshot = files.value.map(({ path, text }) => ({ path, text }));
    try {
      const result = await saveWorkspace({ client, body: { revision: revision.value, files: snapshot } });
      if (!result.data) {
        if (result.error?.error?.details?.execution_id) {
          serverBusy.value = true;
          schedule();
        }
        problem.value = result.response?.status === 409 && result.error?.error?.code === 'workspace_conflict' && !result.error.error.details?.execution_id
          ? 'Workspace conflict: another page saved changes. Your edits are kept. Copy them before reloading.'
          : result.error?.error?.message ?? 'Workspace save failed';
        return null;
      }
      accept(result.data);
      problem.value = null;
      return revision.value;
    } catch (cause) { problem.value = String(cause); return null; }
    finally { busy.value = false; }
  }

  function add(path: string): boolean {
    if (!validSourcePath(path) || generatedPath(path)) {
      problem.value = 'Use a relative .py, .yaml, .yml or .json path; contracts are generated.';
      return false;
    }
    if (files.value.some(file => file.path === path || file.path.startsWith(path + '/') || path.startsWith(file.path + '/'))) {
      problem.value = 'File already exists or conflicts with a directory.';
      return false;
    }
    files.value.push({ path, text: '', generated: false });
    problem.value = null;
    return true;
  }

  function remove(path: string) { files.value = files.value.filter(file => file.path !== path || file.generated); }
  return { files, revision, busy, serverBusy, dirty, loaded, problem, load, save, accept, observe, add, remove };
}
