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
  const executionButton = page.getByRole('button', { name: /^Execution(?: · .*)?$/ });
  const diagnosticsButton = page.getByRole('button', { name: /^Diagnostics \(/ });
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
          files: rect('.file-sidebar'), cfg: rect('.cfg-panel'), panel: rect('.bottom-panel'),
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
        expect(bounds.panel.height).toBeLessThan(expanded ? height * 0.35 : 60);
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
    if (enabled) await expect(text).toBeEditable();
    else await expect(text).not.toBeEditable();
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
  };
  const file = path => page.getByRole('navigation', { name: 'Workspace files', exact: true }).getByRole('button', { name: path, exact: true });
  const select = async path => { await file(path).click(); };
  const save = async () => {
    await page.getByRole('button', { name: 'Save files', exact: true }).click();
    await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
  };
  const create = async (path, text) => {
    await page.getByLabel('New file path', { exact: true }).fill(path);
    await page.getByRole('button', { name: 'Create file', exact: true }).click();
    await editor.fill(text);
  };
  const mainPath = 'workflows/demo/main/__axcutor__.yaml';
  const doublePath = 'workflows/demo/double/__init__.py';
  const summaryPath = 'workflows/demo/main/summarize.py';
  await expect(run).toBeEnabled();
  await expect(page.getByLabel('Entry', { exact: true })).toHaveValue(mainPath);
  await expect(page.getByLabel('Input file', { exact: true })).toHaveValue('inputs/demo.json');
  await expect(graph.locator('.vue-flow__node')).toHaveCount(4);
  const mainSourceText = await editor.inputValue();
  await expect(page.getByRole('button', { name: 'Expand panel', exact: true })).toHaveAttribute('aria-expanded', 'false');
  await checkLayouts('workspace');
  const graphZoom = () => graph.locator('.vue-flow__transformationpane').evaluate(element => new DOMMatrixReadOnly(getComputedStyle(element).transform).a);
  const initialZoom = await graphZoom();
  await graph.getByRole('button', { name: 'Zoom in', exact: true }).click();
  await expect.poll(graphZoom).toBeGreaterThan(initialZoom);
  await graph.getByRole('button', { name: 'Zoom out', exact: true }).click();
  await expect.poll(graphZoom).toBeCloseTo(initialZoom);
  await graph.getByRole('button', { name: 'Fit graph', exact: true }).click();
  await graph.locator('.vue-flow__node[data-id="double"]').getByRole('button', { name: 'double', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Static node probe' })).toContainText('item');
  await page.getByLabel('Expected type', { exact: true }).selectOption('integer');
  await expect(page.getByRole('table', { name: 'Available variables and field projections' })).toContainText('item');
  await graph.getByRole('button', { name: 'Open workflow' }).click();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(3);
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('demo.double');

  // Generated files are visible, and real file edits persist through reload.
  await select('workflows/demo/double/_contract.py');
  await expect(editor).toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: 'Delete file', exact: true })).toBeDisabled();
  await select('inputs/demo.json');
  await editor.fill('{"items":[3,1,2],"delay_seconds":3}');
  await create('scratch/new.json', '{"input":{},"output":{},"run":{"executor":"builtin","content":"return Output()"}}');
  await save();
  await page.reload();
  await expect(file('scratch/new.json')).toBeVisible();
  await file('scratch/new.json').click();
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/new.json');
  await editor.fill('{"input":{},"output":{},"graph":{}}');
  await expect(run).toBeDisabled();
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toBeVisible();
  await select(mainPath);
  await page.getByRole('region', { name: 'Diagnostics', exact: true }).getByRole('button', { name: /^__main__\b/ }).first().click();
  await expect(file('scratch/new.json')).toHaveAttribute('aria-pressed', 'true');
  await page.getByLabel('Entry', { exact: true }).selectOption(mainPath);
  await select('scratch/new.json');
  await page.getByRole('button', { name: 'Delete file', exact: true }).click();
  await save();

  // Two real pages share one revision; a stale Run preserves edits and submits nothing.
  const second = await browser.newPage();
  await second.goto(baseUrl);
  await expect(second.getByRole('button', { name: 'Run', exact: true })).toBeEnabled();
  await create('scratch/kept.py', '# saved by the first page\n');
  await save();
  await second.getByRole('navigation', { name: 'Workspace files', exact: true }).getByRole('button', { name: 'inputs/demo.json', exact: true }).click();
  await second.locator('#source-text').fill('{"items":[99],"delay_seconds":0}');
  let staleRuns = 0;
  second.on('request', request => { if (request.url().endsWith('/v1/workspace/executions')) staleRuns++; });
  await second.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(second.getByRole('alert')).toContainText('Workspace conflict');
  await expect(second.locator('#source-text')).toHaveValue('{"items":[99],"delay_seconds":0}');
  expect(staleRuns).toBe(0);

  // Submission, execution and cleanup lock editing; browsing remains available.
  await select(doublePath);
  const doubleText = await editor.inputValue();
  await select(summaryPath);
  const summaryText = await editor.inputValue();
  const doubleWorkflow = 'workflows/demo/double/__axcutor__.yaml';
  await select(doubleWorkflow);
  const doubleWorkflowText = await editor.inputValue();
  await select(mainPath);
  let resumeSave;
  const heldSave = new Promise(resolve => { resumeSave = resolve; });
  const holdSave = async route => {
    if (route.request().method() === 'PUT') await heldSave;
    await route.continue();
  };
  await page.route('**/v1/workspace', holdSave);
  const submitting = startRun();
  try { await expectEditing(page, false); }
  finally { resumeSave(); }
  await submitting;
  await page.unroute('**/v1/workspace', holdSave);
  await expect(status).toHaveAttribute('data-status', 'running');
  await expectEditing(page, false);
  await expectEditing(second, false);
  await expect(second.locator('#source-text')).toHaveValue('{"items":[99],"delay_seconds":0}');
  await second.close();
  await expect(graph.locator('[data-node-kind="workflow"][data-active="true"]')).toHaveCount(1);
  await expect(graph).toContainText('running: [0], [1]');
  // Switching and collapsing panes preserves observation without polling taking focus.
  await executionButton.focus();
  await page.keyboard.press('Tab');
  await expect(diagnosticsButton).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await expect(diagnosticsButton).toHaveCSS('outline-style', 'solid');
  await expect(status).toBeHidden();
  await page.waitForResponse(response => /\/v1\/executions\/[^/]+$/.test(response.url()) && response.request().method() === 'GET');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Collapse panel', exact: true }).click();
  await page.waitForResponse(response => /\/v1\/executions\/[^/]+$/.test(response.url()) && response.request().method() === 'GET');
  await expect(page.getByRole('button', { name: 'Expand panel', exact: true })).toHaveAttribute('aria-expanded', 'false');
  await executionButton.click();
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
  const childFrame = status.getByRole('button', { name: /function \/ demo\.main\/double\[0\] \/ run/ });
  await expect(childFrame).toBeVisible();
  await childFrame.click();
  await expect(page.getByLabel('Canvas source', { exact: true })).toHaveValue('execution');
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('demo.double');
  await expect(graph.locator('[data-node-kind="run"][data-active="true"]')).toHaveCount(1);
  await expect(graph.locator('[data-active="true"][data-selected="true"]')).toContainText('Selected · Running');
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 20000 });
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('"total": 12');
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('6,');
  await select(doublePath);
  await expectEditing(page, true);
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');
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
  const mainFrame = status.getByRole('button', { name: /^function \/ demo\.main \/ / });
  await mainFrame.click();
  await expect(mainFrame).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('Canvas source', { exact: true })).toHaveValue('execution');
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 20000 });
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('"total": 118');
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('9,');
  await select(doublePath);
  await expectEditing(page, true);
  await expect(page.locator('[data-workspace-state]')).toHaveText('Saved');

  // An ID-only response after a real start must not reuse the previous bundle.
  const canvasSource = page.getByLabel('Canvas source', { exact: true });
  const snapshotOption = canvasSource.locator('option[value="execution"]');
  await expect(snapshotOption).toHaveJSProperty('disabled', false);
  await select(mainPath);
  await editor.fill(mainSourceText
    .replace('    double:', '    changed_double:')
    .replace('start -> double', 'start -> changed_double')
    .replace('double -> summarize', 'changed_double -> summarize'));
  await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toBeVisible();
  await graph.locator('.vue-flow__node[data-id="summarize"]').getByRole('button', { name: 'summarize', exact: true }).click();
  let actualSubmission;
  let upstreamStatus;
  let submissions = 0;
  const queries = [];
  const record = request => {
    if (request.method() === 'POST' && request.url() === runUrl) submissions++;
    if (request.method() === 'GET') queries.push(request.url());
  };
  const loseSource = async route => {
    const accepted = await route.fetch();
    upstreamStatus = accepted.status();
    actualSubmission = await accepted.json();
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
  page.on('request', record);
  await page.route('**/v1/workspace/executions', loseSource);
  try {
    const reply = page.waitForResponse(response => response.url() === runUrl && response.request().method() === 'POST');
    await run.click();
    expect((await reply).status()).toBe(503);
    expect(upstreamStatus).toBe(202);
    const id = actualSubmission.execution.execution_id;
    await expect(executionButton).toHaveAttribute('aria-pressed', 'true');
    await expect(status.locator('dd').first()).toHaveText(id);
    await expect(status.getByRole('button', { name: /function \/ demo\.main\/changed_double\[0\] \/ run/ })).toBeVisible();
    expect(queries).toContain(new URL('/v1/executions/' + id, baseUrl).href);
    await expect(snapshotOption).toHaveJSProperty('disabled', true);
    await expect(canvasSource).toHaveValue('editor');
    await expect(status.locator('button[aria-pressed="true"]')).toHaveCount(0);
    await expect(graph.getByRole('heading', { name: 'Static probe · Select a node', exact: true })).toBeVisible();
    await mainFrame.click();
    await status.getByRole('button', { name: /function \/ demo\.main\/changed_double\[0\] \/ run/ }).click();
    await expect(canvasSource).toHaveValue('editor');
    await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toBeVisible();
    await expect(graph.locator('.vue-flow__node[data-id="double"]')).toHaveCount(0);
    await expect(status.locator('button[aria-pressed="true"]')).toHaveCount(0);
    await expectEditing(page, false);
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
    await expectEditing(page, true);
    expect(submissions).toBe(1);
  } finally {
    await page.unroute('**/v1/workspace/executions', loseSource);
    page.off('request', record);
  }

  // Standalone JSON and raw input files preserve arbitrary precision and key order.
  const longField = 'field_' + 'projection_'.repeat(12);
  const longOutput = 'long-output-'.repeat(60);
  const schema = { type: 'object', properties: { integer: { type: 'integer' }, decimal: { type: 'number' }, ordered: { type: 'object', additionalProperties: { type: 'integer' } }, [longField]: { type: 'string' } }, required: ['integer', 'decimal', 'ordered'] };
  const document = { input: { value: schema }, output: { result: schema }, graph: { nodes: { copy: { use: 'builtin:assign', with: { input: { source: 'value' }, output: { value: 'result' } } } }, edges: ['start -> copy', 'copy -> end'] } };
  await create('scratch/main.json', JSON.stringify(document));
  await page.getByLabel('Entry', { exact: true }).selectOption('scratch/main.json');
  await create('scratch/input.json', '{"value":{"integer":99999999999999999999999999999999999999999999999999,"decimal":1.00000000000000000000001,"ordered":{"2":2,"1":1,"z":3},"' + longField + '":"' + longOutput + '"}}');
  await page.getByLabel('Input file', { exact: true }).selectOption('scratch/input.json');
  await startRun();
  await expect(snapshotOption).toHaveJSProperty('disabled', false);
  await expect(canvasSource).toHaveValue('execution');
  await expect(graph.locator('.vue-flow__node[data-id="copy"]')).toBeVisible();
  await expect(graph.locator('.vue-flow__node[data-id="changed_double"]')).toHaveCount(0);
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 15000 });
  const result = await status.getByRole('region', { name: 'Execution result' }).innerText();
  expect(result).toContain('99999999999999999999999999999999999999999999999999');
  expect(result).toContain('1.00000000000000000000001');
  expect(result.indexOf('"2"')).toBeLessThan(result.indexOf('"1"'));
  expect(result).toContain(longOutput);
  await expectEditing(page, true);
  const longPath = 'scratch/' + 'long_directory_name/'.repeat(6) + 'a_source_file_with_a_long_name.py';
  await create(longPath, '# Long paths and source lines stay inside the editor.\n# ' + longOutput + '\n');
  await expect(file(longPath)).toHaveAttribute('title', longPath);
  await expect(page.locator('.source-path')).toHaveText(longPath);
  await graph.locator('.vue-flow__node[data-id="copy"]').getByRole('button', { name: 'copy', exact: true }).click();
  await page.getByLabel('Expected type', { exact: true }).selectOption('');
  await page.getByText('Entry input contract (1 fields)', { exact: true }).click();
  await expect(page.locator('.entry-contract')).toHaveAttribute('open', '');
  await expect.poll(() => page.locator('.node-probe .table-scroll').first().evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await expect.poll(() => status.getByRole('region', { name: 'Execution result' }).locator('pre').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await checkLayouts('long-content', true);
  await page.getByText('Entry input contract (1 fields)', { exact: true }).click();

  const integerRoot = { input: { value: { type: 'integer' } }, output: {}, run: { executor: 'builtin', content: 'return Output()' } };
  await select('scratch/main.json');
  await editor.fill(JSON.stringify(integerRoot));
  await select('scratch/input.json');
  await editor.fill('{"value":1.0}');
  await run.click();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText('input_validation_failed');
  await expect(diagnosticsButton).toHaveAttribute('aria-pressed', 'true');
  await editor.fill('{"value":1,"value":2}');
  await run.click();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText('duplicate JSON key');
  await select('scratch/main.json');
  await editor.fill(JSON.stringify({ ...integerRoot, run: { executor: 'builtin', content: 'raise ValueError("demo failure")' } }));
  await select('scratch/input.json');
  await editor.fill('{"value":1}');
  await startRun();
  await expect(status).toHaveAttribute('data-status', 'failed', { timeout: 15000 });
  await expect(status.getByRole('region', { name: 'Execution failure' })).toContainText('demo failure');

  await page.getByLabel('Entry', { exact: true }).selectOption(mainPath);
  await page.getByLabel('Input file', { exact: true }).selectOption('inputs/demo.json');
  await startRun();
  await expect(status).toHaveAttribute('data-status', 'running');
  await expect(status.locator('[data-frame-kind="iteration"]')).toHaveCount(2);
  await expectEditing(page, false);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
  await expectEditing(page, true);
  await editor.fill((await editor.inputValue()) + '\n# Editable after cancellation.\n');
  await startRun();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
  await expectEditing(page, true);
  expect(errors).toEqual([]);
  await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
  await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/workbench.png', import.meta.url)), fullPage: true });
  process.stdout.write('PASS: WASM loading/failure visibility, responsive workspace/editor space, long paths/tables/output, keyboard/panel/zoom controls, polling preserves panel choice, workspace persistence/conflicts, read-only submission/execution/cleanup, module/contract edits, uncertain submission clears old snapshot and frame selection, fresh snapshots, WASM/CFG/probes/frames, lossless JSON\n');
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
