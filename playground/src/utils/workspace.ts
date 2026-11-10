/** Mirror file discovery for browser previews only; the host's load_mounts
 * remains authoritative for Run. Python is never evaluated in the browser.
 * Paths map compiler diagnostics back to the actual editable file.
 */
import type { WorkspaceFile } from '../client/types.gen';
import type { CompileRequest, MountUnit } from '../types/compiler';

const keywords = new Set('False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield'.split(' '));
const segment = (part: string) => /^[a-zA-Z_][a-zA-Z0-9_]*$/.test(part) && !keywords.has(part) && !['__axcutor__', '__ax_resources__'].includes(part);
const packageName = (parts: string[]) => !parts[0]?.startsWith('_') && parts.every(segment) && !['yaml', 'yml', 'json'].includes(parts.at(-1) ?? '');

export function workspaceSource(files: WorkspaceFile[], entryPath: string): CompileRequest & { paths: Record<string, string> } {
  const byPath = new Map(files.map(file => [file.path, file]));
  const mounts: Record<string, MountUnit> = {};
  const paths: Record<string, string> = {};
  const roots = new Set(files.flatMap(file => {
    const parts = file.path.split('/');
    return parts.length === 3 && parts[0] === 'workflows' && parts[2] === '__init__.py' && packageName([parts[1]])
      ? [parts[1]] : [];
  }));
  for (const root of roots) mounts[root] = { packages: {}, resources: {}, allowed_executors: null };
  for (const file of files) {
    const parts = file.path.split('/');
    const resource = parts.at(-1) === '__ax_resources__.yaml';
    if (parts[0] !== 'workflows' || !roots.has(parts[1]) || (!resource && parts.at(-1) !== '__axcutor__.yaml')) continue;
    const names = parts.slice(1, -1);
    if (!packageName(names)) throw new Error(`Invalid declaration package: ${file.path}`);
    for (let i = 2; i < parts.length; i++) {
      const directory = parts.slice(0, i).join('/');
      if (!byPath.has(`${directory}/__init__.py`) || byPath.has(`${directory}.py`)) {
        throw new Error(`Workflow requires an unambiguous Python package: ${directory}`);
      }
    }
    const name = names.join('.');
    mounts[parts[1]][resource ? 'resources' : 'packages']![name] = { format: 'yaml', text: file.text };
    // Preserve both sources when workflow and resource headers are colocated.
    paths[resource ? `${name}:resources` : name] = file.path;
  }
  const entryFile = byPath.get(entryPath);
  if (!entryFile) throw new Error('Select a workflow entry file');
  let entry = Object.keys(paths).find(name => paths[name] === entryPath);
  if (entryPath.endsWith('/__axcutor__.yaml')) {
    if (!entry || entry.split('.').some(part => part.startsWith('_'))) throw new Error('Entry requires a public Python package');
  } else {
    if (!/\.(yaml|yml|json)$/.test(entryPath)) throw new Error('Entry must be YAML or JSON');
    entry = '__main__';
    mounts[entry] = { packages: { [entry]: { format: entryPath.endsWith('.json') ? 'json' : 'yaml', text: entryFile.text } } };
    paths[entry] = entryPath;
  }
  return { entry: entry!, mounts, paths };
}

export function validSourcePath(path: string): boolean {
  return !!path && !path.includes('\\') && !path.includes('\0')
    && path.split('/').every(part => !!part && part !== '.' && part !== '..' && part !== '__pycache__')
    && /\.(py|yaml|yml|json)$/.test(path);
}

export const generatedPath = (path: string) => path.split('/').at(-1)?.endsWith('_contract.py') ?? false;
