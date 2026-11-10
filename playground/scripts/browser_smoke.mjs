/** Real browser, WASM, generated SDK, file API and resident module Worker.
 * AXCUTOR_BROWSER_URL exercises an already running Compose production site;
 * otherwise the launcher owns temporary Temporal/HTTP/Vite processes.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';
import { chromium, expect } from '@playwright/test';
import { createServer } from 'vite';

const root = fileURLToPath(new URL('../../', import.meta.url));
const directory = fileURLToPath(new URL('../', import.meta.url));
const external = process.env.AXCUTOR_BROWSER_URL;
const host = external ? null : spawn('uv', ['run', '--project', root, '--locked', 'python', fileURLToPath(new URL('./browser_host.py', import.meta.url))], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let hostLog = '';
host?.stderr.on('data', chunk => { hostLog = (hostLog + chunk).slice(-15000); });
const ready = host && new Promise((resolve, reject) => {
  let output = '';
  host.stdout.on('data', chunk => {
    output += chunk;
    for (const line of output.split('\n')) {
      try { const value = JSON.parse(line); if (value.apiUrl) resolve(value.apiUrl); } catch { /* Non-readiness log. */ }
    }
  });
  host.once('exit', code => reject(new Error('Browser backend exited (' + code + '): ' + hostLog)));
});
let vite;
let browser;
let page;
const errors = [];
try {
  let baseUrl = external;
  if (!baseUrl) {
    const apiUrl = await Promise.race([ready, new Promise((_, reject) => { const timeout = setTimeout(() => reject(new Error('Backend startup timed out')), 60000); timeout.unref(); })]);
    vite = await createServer({ root: directory, server: { host: '127.0.0.1', port: 0, proxy: { '/v1': { target: apiUrl, changeOrigin: true } } } });
    await vite.listen();
    baseUrl = vite.resolvedUrls.local[0];
  }
  const chrome = process.env.AXCUTOR_CHROME ?? (existsSync('/opt/google/chrome/chrome') ? '/opt/google/chrome/chrome' : undefined);
  browser = await chromium.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox'] });
  // A fresh context keeps this failed initialization out of the normal path's
  // module cache. Hold the request to check loading before making it fail.
  const startupContext = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  let releaseWasm;
  const heldWasm = new Promise(resolve => { releaseWasm = resolve; });
  try {
    await startupContext.route('**/*.wasm', async route => { await heldWasm; await route.abort(); });
    const startupPage = await startupContext.newPage();
    const wasmRequest = startupPage.waitForRequest('**/*.wasm');
    await startupPage.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    await wasmRequest;
    await expect(startupPage.locator('[data-workspace-state]')).toHaveText('Saved');
    const panel = startupPage.getByRole('region', { name: 'Execution and diagnostics', exact: true });
    const diagnostics = startupPage.getByRole('region', { name: 'Diagnostics', exact: true });
    const compilerStatus = startupPage.locator('.workbench-status').getByText(/^Compiler:/);
    await expect(compilerStatus).toHaveText('Compiler: loading');
    await expect(panel).toHaveAttribute('data-expanded', 'false');
    await expect(diagnostics).toBeHidden();
    await expect(startupPage.getByRole('button', { name: 'Run', exact: true })).toBeDisabled();
    releaseWasm();
    await expect(startupPage.locator('#diagnostics-panel [role="alert"]')).toContainText('WASM initialization failed');
    await expect(diagnostics.getByRole('alert')).toBeVisible();
    await expect(panel).toHaveAttribute('data-expanded', 'true');
    await expect(startupPage.getByRole('button', { name: /^Diagnostics \(/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(compilerStatus).toHaveText('Compiler: failed');
    await expect(startupPage.getByRole('button', { name: 'Run', exact: true })).toBeDisabled();
    await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
    await startupPage.screenshot({ path: fileURLToPath(new URL('../.browser-test/wasm-initialization-failed.png', import.meta.url)), fullPage: true });
  } finally {
    releaseWasm();
    await startupContext.close();
  }
  page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseUrl);
  const run = page.getByRole('button', { name: /^(Run|Submitting…)$/ });
  const status = page.getByRole('region', { name: 'Execution inspector' });
  const graph = page.getByRole('region', { name: 'Control flow graph' });
  const editor = page.locator('#source-text');
  const input = page.getByLabel('Input', { exact: true });
  const executionButton = page.getByRole('button', { name: /^Execution(?: · .*)?$/ });
  const diagnosticsButton = page.getByRole('button', { name: /^Diagnostics \(/ });
  const openExecution = async () => {
    if (await executionButton.getAttribute('aria-expanded') !== 'true') await executionButton.click();
  };
  const filesToggle = page.getByRole('button', { name: 'Workspace files', exact: true });
  const editorToggle = page.getByRole('button', { name: 'Editor', exact: true });
  const stepData = status.getByRole('region', { name: 'Selected step data', exact: true });
  const traceTab = page.getByRole('button', { name: 'Trace', exact: true });
  const traceCanvas = page.getByRole('region', { name: 'Execution trace', exact: true });
  const traceDetails = page.getByRole('region', { name: 'Trace call details', exact: true });
  const screenshot = async (name, target = page) => {
    await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
    await target.screenshot({ path: fileURLToPath(new URL(`../.browser-test/${name}.png`, import.meta.url)), fullPage: true });
  };
  const openWithTraceOutage = async () => {
    const target = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    target.on('pageerror', error => errors.push(error.message));
    let reads = 0;
    await target.route('**/v1/workspace/trace*', async route => {
      if (++reads > 1) return route.continue();
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"runtime_unavailable","message":"initial trace read unavailable","details":null}}' });
    });
    await target.goto(baseUrl);
    await target.getByRole('button', { name: 'Trace', exact: true }).click();
    await expect(target.getByRole('button', { name: 'Retry trace', exact: true })).toBeVisible();
    await expect(target.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
    await expect(target.locator('.main-canvas')).toHaveAttribute('data-canvas-source', 'editor');
    return target;
  };
  const checkLayouts = async (name, expanded = false) => {
    await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
    for (const [width, height] of [[1600, 1000], [1280, 800], [900, 900], [390, 844]]) {
      await page.setViewportSize({ width, height });
      await graph.getByRole('button', { name: 'Fit graph', exact: true }).click();
      const bounds = await page.evaluate(() => {
        const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
        return {
          pageWidth: document.documentElement.scrollWidth, pageHeight: document.documentElement.scrollHeight,
          source: rect('.source-editor'), editor: rect('#source-text'), canvas: rect('.canvas'),
          files: rect('.file-sidebar'), cfg: rect('.main-canvas'), panel: rect('.bottom-panel'),
        };
      });
      expect(bounds.pageWidth).toBe(width);
      if (width >= 1280) {
        expect(bounds.pageHeight).toBe(height);
        expect(bounds.editor.width).toBeGreaterThan(width === 1600 ? 600 : 450);
        expect(bounds.editor.height).toBeGreaterThan(expanded ? 160 : height * 0.5);
        expect(bounds.canvas.height).toBeGreaterThan(140);
        expect(bounds.source.y).toBe(bounds.files.y);
        expect(bounds.source.y).toBe(bounds.cfg.y);
        expect(Math.abs(bounds.editor.width - bounds.canvas.width)).toBeLessThan(2);
        expect(bounds.panel.height).toBeLessThan(expanded ? height * 0.35 : 115);
      } else if (width === 900) {
        expect(bounds.source.x).toBe(bounds.cfg.x);
        expect(bounds.cfg.y).toBeGreaterThanOrEqual(bounds.source.bottom);
      } else {
        expect(bounds.source.y).toBeGreaterThanOrEqual(bounds.files.bottom);
        expect(bounds.cfg.y).toBeGreaterThanOrEqual(bounds.source.bottom);
        expect(bounds.editor.height).toBeGreaterThan(160);
      }
      await page.screenshot({ path: fileURLToPath(new URL(`../.browser-test/layout-${name}-${width}.png`, import.meta.url)), fullPage: true });
      if (expanded && width === 390) {
        const output = status.getByRole('region', { name: 'Execution result' }).locator('pre');
        await output.scrollIntoViewIfNeeded();
        await expect(output).toBeInViewport();
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
        await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/layout-long-output-390.png', import.meta.url)), fullPage: true });
      }
    }
    await page.setViewportSize({ width: 1600, height: 1000 });
    await graph.getByRole('button', { name: 'Fit graph', exact: true }).click();
  };
  const expectEditing = async (target, enabled) => {
    const text = target.locator('#source-text');
    const runInput = target.getByLabel('Input', { exact: true });
    if (enabled) { await expect(text).toBeEditable(); await expect(runInput).toBeEditable(); }
    else { await expect(text).not.toBeEditable(); await expect(runInput).not.toBeEditable(); }
    for (const name of ['Create file', 'Delete file', 'Save files', /^(Run|Submitting…)$/]) {
      const control = target.getByRole('button', { name, exact: true });
      if (enabled) await expect(control).toBeEnabled();
      else await expect(control).toBeDisabled();
    }
    const path = target.getByLabel('New file path', { exact: true });
    if (enabled) await expect(path).toBeEnabled();
    else await expect(path).toBeDisabled();
  };
  const startRun = async () => {
    const reply = page.waitForResponse(response => response.url().endsWith('/v1/workspace/executions') && response.request().method() === 'POST');
    await run.click();
    const response = await reply;
    expect(response.status()).toBe(202);
    const id = (await response.json()).execution.execution_id;
    await expect(status.locator('dd').first()).toHaveText(id);
    return id;
  };
  const file = path => page.getByRole('navigation', { name: 'Workspace files', exact: true }).getByRole('button', { name: path, exact: true });
  const select = async path => { await file(path).click(); };
  const save = async () => {
    await page.getByRole('button', { name: 'Save files', exact: true }).click();
    await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  };
  const create = async (path, text) => {
    if (await editorToggle.getAttribute('aria-expanded') === 'true') await editorToggle.click();
    await page.getByLabel('New file path', { exact: true }).fill(path);
    await page.getByRole('button', { name: 'Create file', exact: true }).click();
    await expect(editorToggle).toHaveAttribute('aria-expanded', 'true');
    await editor.fill(text);
  };
  const mainPath = 'workflows/demo/main/__axcutor__.yaml';
  const doublePath = 'workflows/demo/double/__init__.py';
  const summaryPath = 'workflows/demo/main/summarize.py';
  await expect(run).toBeEnabled();
  await expect(page.getByLabel('Entry', { exact: true })).toHaveValue(mainPath);
  await expect(input).toHaveValue('{"items":[3,1,2],"delay_seconds":5}');
  await expect(page.getByLabel('Input file', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Entry', { exact: true }).locator('option')).toHaveCount(3);
  await expect(file('inputs/demo.json')).toHaveCount(0);
  // Drafts belong to physical entries, including entries with different schemas.
  const demoDraft = '{"items":[8],"delay_seconds":0}';
  await input.fill(demoDraft);
  await page.getByLabel('Entry', { exact: true }).selectOption('workflows/demo/double/__axcutor__.yaml');
  await expect(input).toHaveValue('{"value":0,"delay_seconds":0}');
  await input.fill('{"value":7,"delay_seconds":0}');
  await page.getByLabel('Entry', { exact: true }).selectOption(mainPath);
  await expect(input).toHaveValue(demoDraft);
  await page.getByRole('button', { name: 'Expand Input', exact: true }).click();
  const multiline = '{\n  "items": [3, 1, 2],\n  "delay_seconds": 5\n}';
  await input.fill(multiline);
  await page.getByRole('button', { name: 'Collapse Input', exact: true }).click();
  await expect(input).toHaveValue('{"items":[3,1,2],"delay_seconds":5}');
  await page.getByRole('button', { name: 'Expand Input', exact: true }).click();
  await expect(input).toHaveValue(multiline);
  await page.getByRole('button', { name: 'Collapse Input', exact: true }).click();
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  await expect(page.getByLabel('Entry', { exact: true }).locator('option:checked')).toHaveText('demo.main');
  await expect(page.getByLabel('Entry', { exact: true })).toHaveAttribute('title', mainPath);
  await expect(page.getByLabel('Entry', { exact: true }).locator('option[value="workflows/demo/double/__axcutor__.yaml"]')).toHaveText('demo.double');
  await expect(page.locator('.app-toolbar')).toHaveCount(0);
  await expect(page.getByLabel('Canvas source', { exact: true })).toHaveCount(0);
  await expect(page.locator('.run-config').getByRole('button', { name: 'Run', exact: true })).toBeVisible();
  await expect(page.locator('.run-config').getByRole('button', { name: 'Cancel', exact: true })).toBeVisible();
  await expect(page.locator('.file-sidebar').getByRole('button', { name: 'Workspace files', exact: true })).toBeVisible();
  await expect(page.locator('.file-sidebar').getByRole('button', { name: 'Delete file', exact: true })).toBeVisible();
  await expect(page.locator('.source-editor').getByRole('button', { name: 'Editor', exact: true })).toBeVisible();
  await expect(page.locator('.source-editor').getByRole('button', { name: 'Save files', exact: true })).toBeVisible();
  await expect(page.locator('.source-editor').getByRole('button', { name: 'Discard edits', exact: true })).toBeVisible();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(5);
  await expect(page.getByText('Compiler: ready', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Files editable', { exact: true })).toHaveCount(0);
  await expect(graph.getByRole('navigation', { name: 'Scope navigation' })).toHaveCount(0);
  const horizontal = await graph.locator('.vue-flow__node').evaluateAll(nodes => Object.fromEntries(nodes.map(node => {
    const rect = node.getBoundingClientRect();
    return [node.dataset.id, { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, height: rect.height }];
  })));
  expect(horizontal.start.x).toBeLessThan(horizontal.double.x);
  expect(horizontal.double.x).toBeLessThan(horizontal.summarize.x);
  expect(horizontal.summarize.x).toBeCloseTo(horizontal.measure_spread.x, 1);
  expect(horizontal.summarize.x).toBeLessThan(horizontal.end.x);
  expect(Math.abs(horizontal.start.y - horizontal.end.y)).toBeLessThan(2);
  expect(horizontal.start.height).toBeLessThan(horizontal.double.height);
  const mainSourceText = await editor.inputValue();
  await expect(executionButton).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByRole('button', { name: /^(Expand|Collapse) panel$/ })).toHaveCount(0);
  await expect(page.locator('.execution-summary')).toHaveCount(0);
  await checkLayouts('workspace');
  // Hiding panes keeps the same editor, unsaved buffer, caret and scroll position.
  // Collapsed panes retain their own reopen controls in a compact header/rail.
  const unsavedText = mainSourceText + '\n# ' + 'long-line-'.repeat(80) + '\n' + '# unsaved position\n'.repeat(100);
  await editor.fill(unsavedText);
  const editorElement = await editor.elementHandle();
  for (const [width, height] of [[1600, 1000], [1280, 800], [900, 900], [390, 844]]) {
    await page.setViewportSize({ width, height });
    const position = await editor.evaluate(element => {
      element.setSelectionRange(17, 23);
      element.scrollTop = 140;
      element.scrollLeft = 120;
      return { start: element.selectionStart, end: element.selectionEnd, top: element.scrollTop, left: element.scrollLeft };
    });
    const original = await graph.boundingBox();
    await filesToggle.click();
    await expect(page.locator('#workspace-files-content')).toBeHidden();
    await expect(editor).toBeVisible();
    if (width >= 1280) expect((await graph.boundingBox()).width).toBeGreaterThan(original.width);
    await filesToggle.click();
    await editorToggle.click();
    await expect(editor).toBeHidden();
    await expect(page.locator('.file-sidebar')).toBeVisible();
    if (width >= 1280) expect((await graph.boundingBox()).width).toBeGreaterThan(original.width);
    else expect((await graph.boundingBox()).y).toBeLessThan(original.y);
    await filesToggle.click();
    const collapsed = await page.evaluate(() => {
      const workbench = document.querySelector('.workbench').getBoundingClientRect();
      const cfg = document.querySelector('.main-canvas').getBoundingClientRect();
      const files = document.querySelector('.file-sidebar').getBoundingClientRect();
      const editor = document.querySelector('.source-editor').getBoundingClientRect();
      return { right: Math.abs(workbench.right - cfg.right), bottom: Math.abs(workbench.bottom - cfg.bottom), files: files.toJSON(), editor: editor.toJSON() };
    });
    expect(collapsed.right).toBeLessThan(2);
    expect(collapsed.bottom).toBeLessThan(2);
    expect(width > 640 ? collapsed.files.width : collapsed.files.height).toBeLessThan(50);
    expect(width > 1100 ? collapsed.editor.width : collapsed.editor.height).toBeLessThan(50);
    await expect(filesToggle).toBeVisible();
    await expect(editorToggle).toBeVisible();
    await expect(filesToggle).toHaveAttribute('title', 'Expand files');
    await expect(filesToggle.locator('svg')).toBeVisible();
    await expect(editorToggle).toHaveText('</>');
    await expect(page.locator('.main-canvas')).toHaveAttribute('data-canvas-source', 'editor');
    // No manual Fit: the layout's actual size change must fit the graph.
    await expect.poll(() => graph.locator('.vue-flow__node').evaluateAll(nodes => {
      const pane = document.querySelector('.canvas').getBoundingClientRect();
      return nodes.every(node => {
        const rect = node.getBoundingClientRect();
        return rect.left >= pane.left - 2 && rect.right <= pane.right + 2 && rect.top >= pane.top - 2 && rect.bottom <= pane.bottom + 2;
      });
    })).toBe(true);
    await screenshot(`layout-canvas-only-${width}`);
    await filesToggle.click();
    await editorToggle.click();
    expect(await editorElement.evaluate(element => element === document.querySelector('#source-text'))).toBe(true);
    await expect(editor).toHaveValue(unsavedText);
    expect(await editor.evaluate(element => ({ start: element.selectionStart, end: element.selectionEnd, top: element.scrollTop, left: element.scrollLeft }))).toEqual(position);
    await expect(page.locator('[data-workspace-state]')).toHaveText('Unsaved changes');
  }
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.locator('.source-editor').getByRole('button', { name: 'Discard edits', exact: true }).click();
  await expect(editor).toHaveValue(mainSourceText);
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  const graphZoom = () => graph.locator('.vue-flow__transformationpane').evaluate(element => new DOMMatrixReadOnly(getComputedStyle(element).transform).a);
  const initialZoom = await graphZoom();
  await graph.getByRole('button', { name: 'Zoom in', exact: true }).click();
  await expect.poll(graphZoom).toBeGreaterThan(initialZoom);
  await graph.getByRole('button', { name: 'Zoom out', exact: true }).click();
  await expect.poll(graphZoom).toBeCloseTo(initialZoom);
  await graph.getByRole('button', { name: 'Fit graph', exact: true }).click();
  await graph.locator('.vue-flow__node[data-id="double"]').getByRole('button', { name: 'double', exact: true }).click();
  await expect(graph.locator('[data-selected="true"]')).toContainText('double');
  for (const text of ['Entry input contract', 'Static probe', 'TypeSpec', 'Expected type', 'definite_in', 'injected', 'Derived ports', 'kill_vars', 'default (normal)']) {
    await expect(page.getByText(text, { exact: false })).toHaveCount(0);
  }
  await graph.getByRole('button', { name: 'Open workflow' }).click();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(3);
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('demo.double');

  // Generated files are visible, and real file edits persist through reload.
  await editorToggle.click();
  await select('workflows/demo/double/_contract.py');
  await expect(editorToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(editor).toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: 'Delete file', exact: true })).toBeDisabled();
  await input.fill('{"items":[3,1,2],"delay_seconds":5}');
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  // Modifier parameters stay in source. Both root and graph nodes display only
  // behavior labels, including when legal integers exceed JavaScript precision.
  const modifiedRun = '"run":{"executor":"builtin","content":"return Output(value=1)"},"timeout_ms":9007199254740993,"retry":{"max_attempts":3,"backoff_ms":9007199254740993}';
  await create('scratch/new.json', '{"input":{},"output":{"value":{"type":"integer"}},' + modifiedRun + '}');
  await save();
  await page.reload();
  await expect(file('scratch/new.json')).toBeVisible();
  await file('scratch/new.json').click();
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/new.json');
  await expect(run).toBeEnabled();
  const rootRun = graph.locator('.vue-flow__node[data-id="run"]');
  await expect(rootRun).toContainText('Retries enabled');
  await expect(rootRun).toContainText('Timeout configured');
  await expect(rootRun).not.toContainText('9007199254740992');
  await expect(rootRun).not.toContainText('9007199254740993');
  await editor.fill('{"input":{"items":{"type":"array","items":{"type":"integer"}}},"output":{"value":{"type":"array","items":{"type":"integer"}}},"graph":{"nodes":{"work":{"output":{"value":{"type":"integer"}},' + modifiedRun + ',"for_each":{"source":"items","item":"item","concurrency":2}}},"edges":["start -> work","work -> end"]}}');
  await expect(run).toBeEnabled();
  const modifiedNode = graph.locator('.vue-flow__node[data-id="work"]');
  await expect(modifiedNode).toContainText('Parallel items');
  await expect(modifiedNode).toContainText('Retries enabled');
  await expect(modifiedNode).toContainText('Timeout configured');
  await expect(modifiedNode).not.toContainText('9007199254740992');
  await expect(modifiedNode).not.toContainText('9007199254740993');
  await screenshot('graph-behavior-labels');
  await editor.fill('{"input":{},"output":{},"graph":{}}');
  await expect(run).toBeDisabled();
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toBeVisible();
  await select(mainPath);
  await editorToggle.click();
  await page.getByRole('region', { name: 'Diagnostics', exact: true }).getByRole('button', { name: /^__main__\b/ }).first().click();
  await expect(editorToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(file('scratch/new.json')).toHaveAttribute('aria-pressed', 'true');
  await page.getByLabel('Entry', { exact: true }).selectOption(mainPath);
  await select('scratch/new.json');
  await page.getByRole('button', { name: 'Delete file', exact: true }).click();
  await save();

  // A clean stale page submits its existing revision without saving. A dirty
  // stale page stops at Save. Both preserve their independent input/source edits.
  const second = await browser.newPage();
  await second.goto(baseUrl);
  await expect(second.getByRole('button', { name: 'Run', exact: true })).toBeEnabled();
  await create('scratch/kept.py', '# saved by the first page\n');
  await save();
  let staleRuns = 0;
  let staleSaves = 0;
  second.on('request', request => {
    if (request.url().endsWith('/v1/workspace/executions')) staleRuns++;
    if (request.url().endsWith('/v1/workspace') && request.method() === 'PUT') staleSaves++;
  });
  const secondInput = second.getByLabel('Input', { exact: true });
  await secondInput.fill('{"items":[99],"delay_seconds":0}');
  await second.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(second.getByRole('alert')).toContainText('workspace_conflict');
  await expect(secondInput).toHaveValue('{"items":[99],"delay_seconds":0}');
  await expect(second.locator('[data-workspace-state]')).toHaveText('Saved');
  expect(staleRuns).toBe(1);
  expect(staleSaves).toBe(0);
  const secondText = mainSourceText + '\n# Unsaved in the stale page.\n';
  await second.locator('#source-text').fill(secondText);
  await second.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(second.getByRole('alert').filter({ hasText: 'Workspace conflict' })).toBeVisible();
  await expect(second.locator('#source-text')).toHaveValue(secondText);
  expect(staleRuns).toBe(1);
  expect(staleSaves).toBe(1);

  // Submission, execution and cleanup lock editing; browsing remains available.
  await select(doublePath);
  const doubleText = await editor.inputValue();
  await select(summaryPath);
  const summaryText = await editor.inputValue();
  const doubleWorkflow = 'workflows/demo/double/__axcutor__.yaml';
  await select(doubleWorkflow);
  const doubleWorkflowText = await editor.inputValue();
  await select(mainPath);
  await editor.fill(mainSourceText + '\n# Saved before run.\n');
  let resumeSave;
  const heldSave = new Promise(resolve => { resumeSave = resolve; });
  const holdSave = async route => {
    if (route.request().method() === 'PUT') await heldSave;
    await route.continue();
  };
  await page.route('**/v1/workspace', holdSave);
  const submitting = startRun();
  try {
    await expectEditing(page, false);
    await expect(executionButton).toHaveText('Execution · Submitting');
    await expect(status.getByRole('region', { name: 'Execution result' })).toHaveCount(0);
  }
  finally { resumeSave(); }
  await submitting;
  await page.unroute('**/v1/workspace', holdSave);
  await expect(status).toHaveAttribute('data-status', 'running');
  await expectEditing(page, false);
  await expectEditing(second, false);
  await expect(second.locator('#source-text')).toHaveValue(secondText);
  await second.close();
  await expect(graph.locator('[data-node-kind="workflow"][data-active="true"]')).toHaveCount(1);
  await expect(graph).toContainText('Running items: [0], [1]');
  await expect(executionButton).toHaveText('Execution · Running');
  await expect(stepData.locator('.data-fields')).toHaveCount(1);
  await expect(status.locator('.frame-tree pre')).toHaveCount(0);
  await expect(status.locator('button[aria-pressed="true"]')).toHaveCount(1);
  await screenshot('execution-current-steps-1600');
  await traceTab.click();
  const parallelCall = traceCanvas.getByRole('button').filter({ hasText: 'double[0] · run' });
  await expect(parallelCall).toBeVisible();
  await parallelCall.click();
  await expect(traceDetails.getByRole('region', { name: 'Call Input', exact: true })).toContainText('3');
  await expect(traceCanvas.locator('[data-span-status="running"]')).not.toHaveCount(0);
  const runningSpans = await traceCanvas.locator('.trace-row').count();
  expect(runningSpans).toBeGreaterThan(3);
  await screenshot('trace-parallel-running-1600');
  await expect(graph).toBeVisible();
  expect((await traceCanvas.boundingBox()).y).toBeGreaterThanOrEqual((await graph.boundingBox()).y + (await graph.boundingBox()).height);
  await expect(page.locator('.bottom-panel').getByRole('region', { name: 'Execution trace', exact: true })).toBeVisible();
  await traceDetails.getByRole('button', { name: 'Locate in Flow', exact: true }).click();
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('demo.double');
  await expect(graph.locator('[data-selected="true"]')).toContainText('run');
  // Switching and collapsing panes preserves observation without polling taking focus.
  await executionButton.focus();
  await page.keyboard.press('Tab');
  await expect(traceTab).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(diagnosticsButton).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await expect(diagnosticsButton).toHaveCSS('outline-style', 'solid');
  await expect(status).toBeHidden();
  await page.waitForResponse(response => /\/v1\/executions\/[^/]+$/.test(response.url()) && response.request().method() === 'GET');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await diagnosticsButton.click();
  await page.waitForResponse(response => /\/v1\/executions\/[^/]+$/.test(response.url()) && response.request().method() === 'GET');
  await expect(diagnosticsButton).toHaveAttribute('aria-expanded', 'false');
  await diagnosticsButton.click();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toBeVisible();
  await openExecution();
  await expect(status).toBeVisible();
  await select(doublePath);
  await expect(editor).toHaveValue(doubleText);
  await expectEditing(page, false);
  const workspaceUrl = new URL('/v1/workspace', baseUrl).href;
  const runUrl = new URL('/v1/workspace/executions', baseUrl).href;
  const busyView = await (await page.request.get(workspaceUrl)).json();
  expect(busyView.busy).toBe(true);
  expect((await page.request.put(workspaceUrl, { data: {
    revision: busyView.revision, files: busyView.files.map(({ path, text }) => ({ path, text })),
  } })).status()).toBe(409);
  expect((await page.request.post(runUrl, { data: {
    entry: mainPath, revision: busyView.revision,
  } })).status()).toBe(409);
  const childFrame = status.getByRole('button', { name: 'demo.double › run · item [0]', exact: true });
  await expect(childFrame).toBeVisible();
  await childFrame.click();
  await expect(page.locator('.main-canvas')).toHaveAttribute('data-canvas-source', 'execution');
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('demo.double');
  await expect(graph.locator('[data-node-kind="run"][data-active="true"]')).toHaveCount(1);
  await expect(graph.locator('[data-active="true"][data-selected="true"] .node-title')).toHaveAttribute('aria-pressed', 'true');
  await expect(graph.locator('[data-active="true"][data-selected="true"]').getByRole('status', { name: 'Running', exact: true })).toBeVisible();
  await expect(stepData.getByRole('heading')).toHaveText('demo.double › run · item [0]');
  await expect(stepData.getByRole('row', { name: /^value / })).toContainText('3');
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 20000 });
  await expect(status.getByRole('region', { name: 'Execution result' }).getByRole('row', { name: /^total / })).toContainText('12');
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('6,');
  await select(doublePath);
  await expectEditing(page, true);
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  // Editing only input must never issue PUT or change the source revision.
  const cleanRevision = (await (await page.request.get(workspaceUrl)).json()).revision;
  let inputOnlySaves = 0;
  const countSaves = request => {
    if (request.url().endsWith('/v1/workspace') && request.method() === 'PUT') inputOnlySaves++;
  };
  page.on('request', countSaves);
  await input.fill('{"items":[4],"delay_seconds":0}');
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  await startRun();
  await expect(status).toHaveAttribute('data-status', 'completed');
  await expectEditing(page, true);
  expect(inputOnlySaves).toBe(0);
  expect((await (await page.request.get(workspaceUrl)).json()).revision).toBe(cleanRevision);
  page.off('request', countSaves);
  await input.fill('{"items":[3,1,2],"delay_seconds":5}');
  await editor.fill(doubleText.replace('input.value * 2', 'int(input.value) * 3'));
  await select(summaryPath);
  await editor.fill(summaryText.replace('sum(input.doubled)', 'sum(input.doubled) + 100'));
  await select(doubleWorkflow);
  await editor.fill(doubleWorkflowText.replace('value: {type: integer}', 'value: {type: number}'));
  await startRun();
  await expectEditing(page, false);
  await select('workflows/demo/double/_contract.py');
  const generatedView = await (await page.request.get(workspaceUrl)).json();
  await expect(editor).toHaveValue(generatedView.files.find(f => f.path === 'workflows/demo/double/_contract.py').text);
  const mainFrame = status.getByRole('region', { name: 'Current steps', exact: true }).locator(':scope > .frame-tree > li > button').first();
  await mainFrame.click();
  await expect(mainFrame).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.main-canvas')).toHaveAttribute('data-canvas-source', 'execution');
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 20000 });
  await expect(status.getByRole('region', { name: 'Execution result' }).getByRole('row', { name: /^total / })).toContainText('118');
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('9,');
  await select(doublePath);
  await expectEditing(page, true);
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');

  // An ID-only response after a real start must not reuse the previous bundle.
  const canvas = page.locator('.main-canvas');
  await select(mainPath);
  await editor.fill(mainSourceText
    .replace('    double:', '    changed_double:')
    .replace('start -> double', 'start -> changed_double')
    .replace('double -> [summarize, measure_spread] -> end', 'changed_double -> [summarize, measure_spread] -> end'));
  await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toBeVisible();
  await graph.locator('.vue-flow__node[data-id="summarize"]').getByRole('button', { name: 'summarize', exact: true }).click();
  let actualSubmission;
  let upstreamStatus;
  let submissions = 0;
  let releaseSubmission;
  const heldSubmission = new Promise(resolve => { releaseSubmission = resolve; });
  let releaseQueries;
  const heldQueries = new Promise(resolve => { releaseQueries = resolve; });
  let releaseTrace;
  const heldTrace = new Promise(resolve => { releaseTrace = resolve; });
  const holdTrace = async route => { await heldTrace; await route.continue(); };
  let queryFailed = false;
  const queries = [];
  const record = request => {
    if (request.method() === 'POST' && request.url() === runUrl) submissions++;
    if (request.method() === 'GET') queries.push(request.url());
  };
  const loseSource = async route => {
    const accepted = await route.fetch();
    upstreamStatus = accepted.status();
    actualSubmission = await accepted.json();
    await heldSubmission;
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: {
        code: 'runtime_unavailable',
        message: 'submission outcome unknown; observe or cancel this ID, do not resubmit',
        details: {
          execution_id: actualSubmission.execution.execution_id,
          workspace: actualSubmission.workspace,
        },
      } }),
    });
  };
  const unavailableQuery = async route => {
    if (route.request().method() !== 'GET') return route.continue();
    await heldQueries;
    if (queryFailed) return route.continue();
    queryFailed = true;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'runtime_unavailable', message: 'query unavailable', details: null } }) });
  };
  page.on('request', record);
  await page.route('**/v1/workspace/executions', loseSource);
  await page.route('**/v1/executions/*', unavailableQuery);
  await page.route('**/v1/workspace/trace*', holdTrace);
  try {
    const reply = page.waitForResponse(response => response.url() === runUrl && response.request().method() === 'POST');
    await run.click();
    await expect(executionButton).toHaveText('Execution · Submitting');
    await expect(status.getByRole('region', { name: 'Execution result' })).toHaveCount(0);
    releaseSubmission();
    expect((await reply).status()).toBe(503);
    expect(upstreamStatus).toBe(202);
    const id = actualSubmission.execution.execution_id;
    await expect(executionButton).toHaveAttribute('aria-pressed', 'true');
    await expect(status.locator('dd').first()).toHaveText(id);
    await expect(executionButton).toHaveText('Execution · Observing');
    await expect(status).toHaveAttribute('data-status', 'observing');
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeEnabled();
    await expect(page.locator('.execution-summary')).toContainText('observation will retry');
    await screenshot('submission-observing');
    releaseQueries();
    await expect.poll(() => queryFailed).toBe(true);
    await expect(status).toHaveAttribute('data-status', 'observing');
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeEnabled();
    const unknownChild = status.getByRole('button', { name: 'demo.main › changed_double[0] › run', exact: true });
    await expect(unknownChild).toBeVisible();
    expect(queries).toContain(new URL('/v1/executions/' + id, baseUrl).href);
    await expect(canvas).toHaveAttribute('data-canvas-source', 'editor');
    await expect(status.locator('button[aria-pressed="true"]')).toHaveCount(1);
    await expect(graph.locator('[data-selected="true"]')).toHaveCount(0);
    await mainFrame.click();
    await unknownChild.click();
    await expect(canvas).toHaveAttribute('data-canvas-source', 'editor');
    await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toBeVisible();
    await expect(graph.locator('.vue-flow__node[data-id="double"]')).toHaveCount(0);
    await expect(unknownChild).toHaveAttribute('aria-pressed', 'true');
    await expect(stepData.getByRole('heading')).toHaveText('demo.main › changed_double[0] › run');
    await expectEditing(page, false);
    releaseTrace();
    await expect(canvas).toHaveAttribute('data-canvas-source', 'execution');
    await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toBeVisible();
    await expect(graph.locator('.vue-flow__node[data-id="double"]')).toHaveCount(0);
    let releaseCancellation;
    const heldCancellation = new Promise(resolve => { releaseCancellation = resolve; });
    const holdCancel = async route => { await heldCancellation; await route.continue(); };
    const cancelPattern = '**/v1/executions/' + id + ':cancel';
    await page.route(cancelPattern, holdCancel);
    try {
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      await expect(executionButton).toHaveText('Execution · Cancelling');
      await expect(page.getByRole('button', { name: 'Cancelling…', exact: true })).toBeDisabled();
      await executionButton.click();
      await expect(executionButton).toBeVisible();
      await expect(page.locator('.execution-summary')).toContainText('Cancellation requested');
      await screenshot('execution-cancelling');
      await openExecution();
    } finally {
      releaseCancellation();
      await page.unroute(cancelPattern, holdCancel);
    }
    await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
    await expect(executionButton).toHaveText('Execution · Cancelled');
    await expectEditing(page, true);
    expect(submissions).toBe(1);
  } finally {
    releaseSubmission();
    releaseQueries();
    releaseTrace();
    await page.unroute('**/v1/executions/*', unavailableQuery);
    await page.unroute('**/v1/workspace/trace*', holdTrace);
    await page.unroute('**/v1/workspace/executions', loseSource);
    page.off('request', record);
  }

  // Standalone workflows and inline input preserve arbitrary precision and key order.
  const longField = 'field_' + 'projection_'.repeat(12);
  const longOutput = 'long-output-'.repeat(60);
  const schema = { type: 'object', properties: { integer: { type: 'integer' }, decimal: { type: 'number' }, ordered: { type: 'object', additionalProperties: { type: 'integer' } }, [longField]: { type: 'string' } }, required: ['integer', 'decimal', 'ordered'] };
  const document = { input: { value: schema }, output: { result: schema }, graph: { nodes: { copy: { use: 'builtin:assign', with: { input: { source: 'value' }, output: { value: 'result' } } } }, edges: ['start -> copy', 'copy -> end'] } };
  await create('scratch/main.json', JSON.stringify(document));
  await create('scratch/main-copy.json', JSON.stringify(document));
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/main.json');
  await expect(page.getByLabel('Entry', { exact: true }).locator('option:checked')).toHaveText('__main__');
  const exactValue = '{"integer":99999999999999999999999999999999999999999999999999,"decimal":1.00000000000000000000001,"ordered":{"2":2,"1":1,"z":3},"' + longField + '":"' + longOutput + '"}';
  await input.fill('{"value":' + exactValue + '}');
  await startRun();
  await expect(canvas).toHaveAttribute('data-canvas-source', 'execution');
  await expect(graph.locator('.vue-flow__node[data-id="copy"]')).toBeVisible();
  await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toHaveCount(0);
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 15000 });
  const result = await status.getByRole('region', { name: 'Execution result' }).innerText();
  expect(result).toContain('99999999999999999999999999999999999999999999999999');
  expect(result).toContain('1.00000000000000000000001');
  expect(result.indexOf('"2"')).toBeLessThan(result.indexOf('"1"'));
  expect(result).toContain(longOutput);
  await expectEditing(page, true);
  // The builtin copy completes before any 300ms frame poll. Its real call and
  // exact payloads remain selectable from the final trace and after page reload.
  await traceTab.click();
  const copyCall = traceCanvas.getByRole('button').filter({ hasText: 'copy · builtin' });
  await expect(copyCall).toBeVisible();
  await copyCall.click();
  await expect(traceDetails.getByRole('region', { name: 'Call Input', exact: true })).toContainText('99999999999999999999999999999999999999999999999999');
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText('1.00000000000000000000001');
  await expect(copyCall).toContainText('0 ms');
  await expect(copyCall.locator('.trace-bar[data-zero="true"]')).toBeVisible();
  const timing = traceDetails.locator('.trace-timing');
  await expect(timing).not.toHaveAttribute('open');
  await expect(timing.getByText('Started', { exact: true })).toBeHidden();
  await timing.getByText('Timing', { exact: true }).click();
  await expect(timing.getByText('Started', { exact: true })).toBeVisible();
  await timing.getByText('Timing', { exact: true }).click();
  await expect(traceCanvas.getByRole('heading')).toHaveCount(0);
  const copyId = await copyCall.getAttribute('data-span-id');
  await traceCanvas.getByRole('button', { name: 'Zoom trace in', exact: true }).click();
  await expect(traceCanvas.locator('.trace-timeline')).toHaveAttribute('style', /150%/);
  await traceCanvas.getByRole('button', { name: 'Fit trace', exact: true }).click();
  await screenshot('trace-fast-lossless-1600');
  await traceDetails.getByRole('button', { name: 'Locate in Flow', exact: true }).click();
  await expect(graph.locator('[data-selected="true"]')).toContainText('copy');
  // Only the initial HTTP read fails. Retry must restore the real completed
  // execution, result and submitted snapshot, retaining edits made meanwhile.
  const completedRecovery = await openWithTraceOutage();
  try {
    const recoveryEditor = completedRecovery.locator('#source-text');
    await expect(recoveryEditor).toBeEditable();
    const editedText = await recoveryEditor.inputValue() + '\n# Unsaved during trace recovery.\n';
    await recoveryEditor.fill(editedText);
    await completedRecovery.getByLabel('Entry', { exact: true }).selectOption('scratch/main.json');
    const recoveredInput = completedRecovery.getByLabel('Input', { exact: true });
    const editedInput = '{"value":{"integer":99,"decimal":1.0,"ordered":{}}}';
    await recoveredInput.fill(editedInput);
    await completedRecovery.getByRole('button', { name: 'Retry trace', exact: true }).click();
    await completedRecovery.getByRole('button', { name: /^Execution ·/ }).click();
    const recoveredStatus = completedRecovery.getByRole('region', { name: 'Execution inspector' });
    await expect(recoveredStatus).toHaveAttribute('data-status', 'completed');
    await expect(recoveredStatus.getByRole('region', { name: 'Execution result' })).toContainText('1.00000000000000000000001');
    await expect(recoveredStatus.getByRole('region', { name: 'Execution result' })).toContainText('99999999999999999999999999999999999999999999999999');
    const recoveryCanvas = completedRecovery.locator('.main-canvas');
    await expect(recoveryCanvas).toHaveAttribute('data-canvas-source', 'editor');
    await completedRecovery.getByRole('button', { name: 'Trace', exact: true }).click();
    await completedRecovery.getByRole('region', { name: 'Execution trace', exact: true }).getByRole('button').filter({ hasText: 'copy · builtin' }).click();
    await completedRecovery.getByRole('button', { name: 'Locate in Flow', exact: true }).click();
    await expect(recoveryCanvas).toHaveAttribute('data-canvas-source', 'execution');
    await expect(completedRecovery.locator('.cfg-node[data-selected="true"]')).toContainText('copy');
    await expect(recoveredInput).toHaveValue(editedInput);
    await completedRecovery.getByRole('navigation', { name: 'Workspace files', exact: true }).getByRole('button', { name: mainPath, exact: true }).click();
    await expect(recoveryEditor).toHaveValue(editedText);
    await expect(completedRecovery.locator('[data-workspace-state]')).toHaveText('Unsaved changes');
    await screenshot('trace-retry-completed', completedRecovery);
  } finally { await completedRecovery.close(); }

  // Fixed response data exercises the production component's rendered geometry,
  // independently of scheduling. The recovery tests above/below use real runs.
  const axisPage = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  axisPage.on('pageerror', error => errors.push(error.message));
  try {
    const latest = await (await page.request.get(new URL('/v1/workspace/trace', baseUrl).href)).json();
    const instant = second => `2026-01-01T00:00:${second}Z`;
    const span = (id, start, finish, parent_id = 'window') => ({
      id, parent_id, kind: 'run', address: `__main__/${id}`, frame_kind: 'iteration', cursor: id,
      started_at: instant(start), finished_at: finish === null ? null : instant(finish),
      status: finish === null ? 'running' : 'completed', input: {}, output: finish === null ? null : {},
      branch: null, error: null, events: [],
    });
    const fixture = { ...latest, execution: { ...latest.execution, status: 'running', started_at: instant('00'), finished_at: null, frames: [], result: null, error: null }, complete: false, observed_at: instant('10'), spans: [
      span('window', '00', null, null), span('early', '01', '05'),
      span('overlap', '03', '07'), span('live', '06', null),
    ] };
    await axisPage.route('**/v1/workspace/trace*', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(fixture) }));
    await axisPage.goto(baseUrl);
    await axisPage.getByRole('button', { name: 'Trace', exact: true }).click();
    await expect(axisPage.locator('.trace-row')).toHaveCount(4);
    const bounds = () => axisPage.locator('.trace-row').evaluateAll(rows => Object.fromEntries(rows.map(row => {
      const track = row.querySelector('.trace-track').getBoundingClientRect();
      const bar = row.querySelector('.trace-bar').getBoundingClientRect();
      return [row.dataset.spanId, { axis: track.x, left: (bar.x - track.x) / track.width, width: bar.width / track.width }];
    })));
    const initial = await bounds();
    expect(initial.early.axis).toBe(initial.overlap.axis);
    expect(initial.early.left).toBeCloseTo(0.1, 3);
    expect(initial.early.width).toBeCloseTo(0.4, 3);
    expect(initial.overlap.left).toBeCloseTo(0.3, 3);
    expect(initial.overlap.width).toBeCloseTo(0.4, 3);
    expect(initial.overlap.left).toBeLessThan(initial.early.left + initial.early.width);
    expect(initial.live.left).toBeCloseTo(0.6, 3);
    expect(initial.live.width).toBeCloseTo(0.4, 3);
    const early = axisPage.locator('[data-span-id="early"]');
    await expect(early).toContainText('4.000 s');
    await screenshot('trace-axis-fixed', axisPage);
    fixture.observed_at = instant('20');
    await expect.poll(async () => (await bounds()).live.width).toBeCloseTo(0.7, 3);
    const advanced = await bounds();
    // The shared range doubled. Closed calls still cover 1–5s and 3–7s;
    // only the unfinished call grows from 6–10s to 6–20s.
    expect(advanced.live.left).toBeCloseTo(0.3, 3);
    expect(advanced.early.left).toBeCloseTo(0.05, 3);
    expect(advanced.early.width).toBeCloseTo(0.2, 3);
    expect(advanced.overlap.left).toBeCloseTo(0.15, 3);
    expect(advanced.overlap.width).toBeCloseTo(0.2, 3);
    await expect(early).toContainText('4.000 s');
    await screenshot('trace-axis-advanced', axisPage);
  } finally { await axisPage.close(); }
  await page.reload();
  await openExecution();
  await expect(status).toHaveAttribute('data-status', 'completed');
  await expect(canvas).toHaveAttribute('data-canvas-source', 'execution');
  await expect(graph.locator('.vue-flow__node[data-id="copy"]')).toBeVisible();
  await traceTab.click();
  await copyCall.click();
  await expect(copyCall).toHaveAttribute('data-span-id', copyId);
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText(longOutput);
  await page.setViewportSize({ width: 390, height: 844 });
  await traceCanvas.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await screenshot('trace-fast-lossless-390');
  await page.setViewportSize({ width: 1600, height: 1000 });
  await openExecution();
  // Identical standalone sources still own different drafts. Reload must restore
  // only the submitted physical entry, including exact numbers and key order.
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/main-copy.json');
  await expect(input).toHaveValue('{"value":{"integer":0,"decimal":0.0,"ordered":{}}}');
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/main.json');
  await expect(input).toHaveValue('{"value":' + exactValue + '}');
  await select('scratch/main.json');
  await editor.fill(JSON.stringify(document).replaceAll('copy', 'edited_copy'));
  await expect(graph.locator('.vue-flow__node[data-id="edited_copy"]')).toBeVisible();
  await traceTab.click();
  await copyCall.click();
  await traceDetails.getByRole('button', { name: 'Locate in Flow', exact: true }).click();
  await expect(graph.locator('[data-selected="true"]')).toContainText('copy');
  await expect(graph.locator('.vue-flow__node[data-id="edited_copy"]')).toHaveCount(0);
  await select('scratch/main.json');
  await editor.fill(JSON.stringify(document));
  const longPath = 'scratch/' + 'long_directory_name/'.repeat(6) + 'a_source_file_with_a_long_name.py';
  await create(longPath, '# Long paths and source lines stay inside the editor.\n# ' + longOutput + '\n');
  await expect(file(longPath)).toHaveAttribute('title', longPath);
  await expect(page.locator('.source-path')).toHaveText(longPath);
  await graph.locator('.vue-flow__node[data-id="copy"]').getByRole('button', { name: 'copy', exact: true }).click();
  await openExecution();
  await expect.poll(() => status.getByRole('region', { name: 'Execution result' }).locator('pre').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await checkLayouts('long-content', true);

  // Real signal waits make current data stable across viewport/layout changes.
  // Entering the next step removes the selected child frame and its data.
  const waitingDocument = { input: { value: schema, parent_note: { type: 'string' } }, output: { result: schema }, branches: { delivered: 'Delivered' }, graph: {
    nodes: {
      collect: { input: ['value'], output: { result: schema }, graph: {
        nodes: { receive: { input: ['value'], output: { result: schema }, run: { executor: 'signal' } } },
        edges: ['start -> receive', 'receive -> end'],
      } },
      confirm: { input: ['result'], run: { executor: 'signal' } },
    },
    edges: ['start -> collect', 'collect -> confirm', 'confirm -> end.delivered'],
  } };
  await create('scratch/waiting.json', JSON.stringify(waitingDocument));
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/waiting.json');
  await input.fill('{"value":' + exactValue + ',"parent_note":"Parent context stays selected"}');
  const waitingId = await startRun();
  await expect(executionButton).toHaveText('Execution · Waiting for signal');
  await expect(stepData.getByRole('heading')).toHaveText('__main__ › collect › receive');
  await expect(stepData).toContainText('1.00000000000000000000001');
  await expect(stepData).toContainText('99999999999999999999999999999999999999999999999999');
  const firstStep = status.getByRole('button', { name: '__main__ › collect › receive · Waiting for signal', exact: true });
  await expect(firstStep).toHaveAttribute('aria-pressed', 'true');
  await traceTab.click();
  await expect(traceCanvas.locator(`[data-span-id="${copyId}"]`)).toHaveCount(0);
  const receiveCall = traceCanvas.getByRole('button').filter({ hasText: 'receive · run' });
  await receiveCall.click();
  await expect(receiveCall).toContainText('Waiting for signal');
  const receiveSpanId = await receiveCall.getAttribute('data-span-id');
  const initialElapsed = await receiveCall.locator('small').innerText();
  for (let i = 0; i < 2; i++) await page.waitForResponse(response => response.url().includes('/v1/workspace/trace') && response.ok());
  await expect(receiveCall).toHaveAttribute('aria-pressed', 'true');
  await expect(receiveCall.locator('small')).not.toHaveText(initialElapsed);
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText('No output has been returned');
  await screenshot('trace-signal-wait-1600');
  await openExecution();
  await expect(stepData.getByRole('rowheader', { name: 'parent_note', exact: true })).toHaveCount(0);
  // Select a non-default parent with data absent from its child. Two successful
  // polls ensure the first response has been applied before checking selection.
  const parentStep = status.getByRole('button', { name: '__main__ › collect', exact: true });
  await parentStep.click();
  await expect(parentStep).toHaveAttribute('aria-pressed', 'true');
  const waitingExecutionUrl = new URL('/v1/executions/' + waitingId, baseUrl).href;
  for (let i = 0; i < 2; i++) {
    const poll = await page.waitForResponse(response => response.url() === waitingExecutionUrl && response.request().method() === 'GET' && response.ok());
    await poll.finished();
  }
  await expect(parentStep).toHaveAttribute('aria-pressed', 'true');
  await expect(firstStep).toHaveAttribute('aria-pressed', 'false');
  await expect(stepData.getByRole('heading')).toHaveText('__main__ › collect');
  await expect(stepData.getByRole('row', { name: /^parent_note / })).toContainText('Parent context stays selected');
  await screenshot('execution-parent-selection-preserved');
  await firstStep.click();
  await expect(stepData.getByRole('rowheader', { name: 'parent_note', exact: true })).toHaveCount(0);
  const scopeNavigation = graph.getByRole('navigation', { name: 'Scope navigation', exact: true });
  await expect(scopeNavigation).toBeVisible();
  await scopeNavigation.getByRole('button', { name: '__main__', exact: true }).click();
  await expect(scopeNavigation).toHaveCount(0);
  await expect(graph.locator('.vue-flow__node[data-id="collect"]')).toBeVisible();
  await firstStep.click();
  await expect(scopeNavigation).toBeVisible();
  await expect(graph.locator('[data-selected="true"][data-active="true"]')).toContainText('receive');
  await graph.getByRole('button', { name: 'Zoom in', exact: true }).click();
  const runningZoom = await graphZoom();
  await page.waitForResponse(response => /\/v1\/executions\/[^/]+$/.test(response.url()) && response.request().method() === 'GET');
  expect(await graphZoom()).toBeCloseTo(runningZoom);
  await filesToggle.click();
  await editorToggle.click();
  await expect(canvas).toHaveAttribute('data-canvas-source', 'execution');
  for (const [width, height] of [[1600, 1000], [1280, 800], [900, 900], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await expect(editor).toBeHidden();
    await expect(page.locator('#workspace-files-content')).toBeHidden();
    await stepData.getByRole('heading').scrollIntoViewIfNeeded();
    await screenshot(`execution-waiting-${width}`);
    await page.locator('.panel-content').evaluate(element => {
      const data = element.querySelector('.selected-step .data-fields');
      element.scrollTop += data.getBoundingClientRect().top - element.getBoundingClientRect().top;
    });
    await screenshot(`execution-step-data-${width}`);
  }
  await page.setViewportSize({ width: 1600, height: 1000 });
  await filesToggle.click();
  await editorToggle.click();
  const waitingUrl = new URL('/v1/executions/' + waitingId + ':signal', baseUrl).href;
  const address = await stepData.locator('.signal-addresses code').innerText();
  expect(address).toBe('__main__/collect/receive');
  const firstSignal = await page.request.post(waitingUrl, { headers: { 'content-type': 'application/json' }, data: '{"address":' + JSON.stringify(address) + ',"output":{"result":' + exactValue + '}}' });
  expect([200, 202]).toContain(firstSignal.status());
  await expect(firstStep).toHaveCount(0);
  await expect(status.locator('button[aria-pressed="true"]')).toHaveCount(1);
  await expect(stepData.getByRole('heading')).toHaveText('__main__ › confirm');
  await expect(stepData.locator('.signal-addresses')).toHaveText('__main__/confirm');
  await expect(stepData.getByRole('row', { name: /^result / })).toContainText('1.00000000000000000000001');
  await expect(graph.locator('[data-selected="true"]')).toHaveCount(0);
  await traceTab.click();
  await expect(receiveCall).toHaveAttribute('data-span-id', receiveSpanId);
  await expect(receiveCall).toHaveAttribute('aria-pressed', 'true');
  await expect(receiveCall).toContainText('completed');
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText('1.00000000000000000000001');
  await expect(traceDetails.getByRole('heading').first()).toBeInViewport();
  await screenshot('trace-completed-call-during-run-1600');
  await openExecution();
  // Losing the cleanup observation cannot prove the workspace editable, even
  // after a real terminal execution. The original busy observation is retained.
  let unavailableWorkspaceReads = 0;
  const unavailableWorkspace = async route => {
    if (route.request().method() !== 'GET') return route.continue();
    unavailableWorkspaceReads++;
    await route.abort();
  };
  await page.route('**/v1/workspace', unavailableWorkspace);
  try {
    const finalSignal = await page.request.post(waitingUrl, { data: { address: '__main__/confirm', output: {} } });
    expect([200, 202]).toContain(finalSignal.status());
    await expect(status).toHaveAttribute('data-status', 'completed');
    await expect(executionButton).toHaveText('Execution · Succeeded');
    await expect(status.getByRole('region', { name: 'Execution result' }).getByRole('heading')).toHaveText('Succeeded · branch delivered');
    await expect(status.getByRole('region', { name: 'Current steps', exact: true })).toHaveCount(0);
    await expect(stepData).toHaveCount(0);
    await expect.poll(() => unavailableWorkspaceReads).toBeGreaterThan(0);
    await expectEditing(page, false);
    await expect(page.locator('.execution-summary')).toContainText('workspace is still busy');
    await screenshot('execution-succeeded-workspace-busy');
  } finally { await page.unroute('**/v1/workspace', unavailableWorkspace); }
  await expectEditing(page, true);
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/main.json');

  const integerRoot = { input: { value: { type: 'integer' } }, output: {}, run: { executor: 'builtin', content: 'return Output()' } };
  await select('scratch/main.json');
  await editor.fill(JSON.stringify(integerRoot));
  let preflightSubmissions = 0;
  const countPreflight = request => { if (request.url().endsWith('/v1/workspace/executions')) preflightSubmissions++; };
  page.on('request', countPreflight);
  await input.fill('{"value":1.0}');
  await expect(run).toBeDisabled();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText('Input:');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await input.fill('{"value":1,"value":2}');
  await expect(run).toBeDisabled();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText(/duplicate .*key/);
  expect(preflightSubmissions).toBe(0);
  page.off('request', countPreflight);
  const keptTrace = await (await page.request.get(new URL('/v1/workspace/trace', baseUrl).href)).json();
  expect(keptTrace.execution_id).toBe(waitingId);
  await select('scratch/main.json');
  await editor.fill(JSON.stringify({ ...integerRoot, run: { executor: 'builtin', content: 'raise ValueError("demo failure")' } }));
  await input.fill('{"value":1}');
  await startRun();
  await expect(status).toHaveAttribute('data-status', 'failed', { timeout: 15000 });
  await expect(status.getByRole('region', { name: 'Execution failure' })).toContainText('demo failure');
  await expect(executionButton).toHaveText('Execution · Failed');
  await traceTab.click();
  const failedCall = traceCanvas.getByRole('button').filter({ hasText: '· run' });
  await expect(failedCall).toContainText('failed');
  await failedCall.click();
  await expect(traceDetails).toContainText('demo failure');
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText('No output: this call failed');
  await screenshot('trace-failed-1600');
  await openExecution();
  for (const [width, height] of [[1600, 1000], [1280, 800], [900, 900], [390, 844]]) {
    await page.setViewportSize({ width, height });
    await status.getByRole('region', { name: 'Execution failure' }).scrollIntoViewIfNeeded();
    await screenshot(`execution-failed-${width}`);
  }
  await page.setViewportSize({ width: 1600, height: 1000 });

  await page.getByLabel('Entry', { exact: true }).selectOption(mainPath);
  await input.fill('{"items":[3,1,2],"delay_seconds":5}');
  await startRun();
  await expect(status).toHaveAttribute('data-status', 'running');
  await expect(status.locator('[data-frame-kind="iteration"]')).toHaveCount(2);
  await expectEditing(page, false);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
  await expectEditing(page, true);
  await traceTab.click();
  await expect(traceCanvas.locator('[data-span-status="cancelled"]')).not.toHaveCount(0);
  await traceCanvas.locator('[data-span-status="cancelled"]').last().click();
  await expect(traceDetails.getByRole('region', { name: 'Call Output', exact: true })).toContainText('No output: this call was cancelled');
  await screenshot('trace-cancelled-1600');
  await openExecution();
  await editor.fill((await editor.inputValue()) + '\n# Editable after cancellation.\n');
  await startRun();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
  await expectEditing(page, true);
  // A real waiting run must also regain status, source and Cancel after Retry.
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/waiting.json');
  await input.fill('{"value":' + exactValue + ',"parent_note":"Retry recovery"}');
  const recoveryId = await startRun();
  const runningRecovery = await openWithTraceOutage();
  try {
    await runningRecovery.getByRole('button', { name: 'Retry trace', exact: true }).click();
    await runningRecovery.getByRole('button', { name: /^Execution ·/ }).click();
    const recoveredStatus = runningRecovery.getByRole('region', { name: 'Execution inspector' });
    await expect(recoveredStatus).toHaveAttribute('data-status', 'running');
    await expect(recoveredStatus.locator('dd').first()).toHaveText(recoveryId);
    await expect(runningRecovery.locator('.main-canvas')).toHaveAttribute('data-canvas-source', 'execution');
    await expect(runningRecovery.getByRole('button', { name: 'Cancel', exact: true })).toBeEnabled();
    await screenshot('trace-retry-running', runningRecovery);
    await runningRecovery.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(recoveredStatus).toHaveAttribute('data-status', 'cancelled');
    await expect(status).toHaveAttribute('data-status', 'cancelled');
    await expectEditing(page, true);
  } finally { await runningRecovery.close(); }
  expect(errors).toEqual([]);
  await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
  await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/workbench.png', import.meta.url)), fullPage: true });
  process.stdout.write('PASS: existing workbench lifecycle/layout/controls, latest trace with fast calls, zero duration and lossless Input/Output, trace zoom/Fit/CFG, page recovery and Retry for completed/running executions, shared-axis positions/overlap/live extension, preflight retention, uncertain ID/source recovery\n');
} catch (error) {
  if (page) {
    await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
    await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/failure.png', import.meta.url)), fullPage: true }).catch(() => {});
  }
  process.stderr.write(hostLog + '\n');
  throw error;
} finally {
  await browser?.close();
  await vite?.close();
  if (host && host.exitCode === null) {
    host.kill('SIGTERM');
    await Promise.race([once(host, 'exit'), new Promise(resolve => { const timer = setTimeout(() => { host.kill('SIGKILL'); resolve(); }, 30000); timer.unref(); })]);
  }
}
