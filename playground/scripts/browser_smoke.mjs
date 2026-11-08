/** Real browser + real WASM + generated SDK + HTTP + Temporal/Worker integration.
 * Generated screenshots/logs belong in .browser-test; no API is mocked here.
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
const host = spawn('uv', ['run', '--project', root, '--locked', 'python', fileURLToPath(new URL('./browser_host.py', import.meta.url))], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let hostLog = '';
host.stderr.on('data', chunk => { hostLog = (hostLog + chunk).slice(-15000); });
const ready = new Promise((resolve, reject) => {
  let output = '';
  host.stdout.on('data', chunk => {
    output += chunk;
    for (const line of output.split('\n')) {
      try { const value = JSON.parse(line); if (value.apiUrl) resolve(value.apiUrl); } catch { /* Non-readiness log. */ }
    }
  });
  host.once('exit', code => reject(new Error(`Browser backend exited (${code}): ${hostLog}`)));
});
let vite;
let browser;
let page;
const errors = [];
try {
  const apiUrl = await Promise.race([ready, new Promise((_, reject) => { const timeout = setTimeout(() => reject(new Error('Backend startup timed out')), 60000); timeout.unref(); })]);
  vite = await createServer({ root: directory, server: { host: '127.0.0.1', port: 0, proxy: { '/v1': { target: apiUrl, changeOrigin: true } } } });
  await vite.listen();
  const chrome = process.env.AXCUTOR_CHROME ?? (existsSync('/opt/google/chrome/chrome') ? '/opt/google/chrome/chrome' : undefined);
  browser = await chromium.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox'] });
  page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
  page.on('pageerror', error => errors.push(error.message));
  const baseUrl = vite.resolvedUrls.local[0];
  await page.goto(baseUrl);
  const run = page.getByRole('button', { name: 'Run', exact: true });
  const input = page.getByLabel('Input JSON', { exact: true });
  const status = page.getByRole('region', { name: 'Execution inspector' });
  const graph = page.getByRole('region', { name: 'Control flow graph' });
  await expect(run).toBeEnabled();
  const mainSourceText = await page.locator('#source-text').inputValue();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(4);
  await graph.locator('.vue-flow__node[data-id="double"]').getByRole('button', { name: 'double', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Static node probe' })).toContainText('item');
  await page.getByLabel('Expected type', { exact: true }).selectOption('integer');
  await expect(page.getByRole('table', { name: 'Available variables and field projections' })).toContainText('item');
  await graph.getByRole('button', { name: 'Open workflow' }).click();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(3);
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('double.yaml');

  // File creation/deletion and diagnostics navigate the actual editable closure.
  await page.getByLabel('New file path', { exact: true }).fill('nested/new.json');
  await page.getByLabel('New file format', { exact: true }).selectOption('json');
  await page.getByRole('button', { name: 'Add file', exact: true }).click();
  await page.getByLabel('Entry', { exact: true }).selectOption('nested/new.json');
  await page.locator('#source-text').fill('{"input":{},"output":{},"run":{"executor":"builtin","content":"return Output()"}}');
  await expect(run).toBeEnabled();
  await expect(graph.locator('.vue-flow__node')).toHaveCount(3);
  await page.locator('#source-text').fill('{"input":{},"output":{},"graph":{}}');
  await expect(run).toBeDisabled();
  const files = page.getByRole('navigation', { name: 'Source files', exact: true });
  const mainFile = files.getByRole('button', { name: /^main\.yaml \(/ });
  const newFile = files.getByRole('button', { name: /^nested\/new\.json \(/ });
  await mainFile.click();
  await expect(mainFile).toHaveAttribute('aria-pressed', 'true');
  await expect(newFile).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#source-text')).toHaveValue(mainSourceText);
  await page.getByRole('region', { name: 'Diagnostics', exact: true }).getByRole('button', { name: /^nested\/new\.json\b/ }).first().click();
  await expect(newFile).toHaveAttribute('aria-pressed', 'true');
  await expect(mainFile).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('#source-text')).toHaveValue('{"input":{},"output":{},"graph":{}}');
  await page.getByRole('button', { name: 'Delete file', exact: true }).click();
  await page.getByRole('button', { name: 'Load default example', exact: true }).click();
  await input.fill('{"items":[3,1,2],"delay_seconds":1}');

  // Execute the repository preset, inspect live iteration/function frames.
  await run.click();
  await expect(status).toHaveAttribute('data-status', 'running');
  await expect(graph.locator('[data-node-kind="workflow"][data-active="true"]')).toHaveCount(1);
  await expect(graph).toContainText('running: [0], [1]');
  await graph.locator('.vue-flow__node[data-id="double"]').getByRole('button', { name: 'double', exact: true }).click();
  await expect(status.getByRole('button', { name: 'function / main.yaml / double', exact: true })).toHaveAttribute('aria-pressed', 'true');
  // An editor error must not change the submitted frame's graph interpretation.
  await page.locator('#source-text').fill('{}');
  const childFrame = status.getByRole('button', { name: /function \/ main\.yaml\/double\[0\] \/ run/ });
  await expect(childFrame).toBeVisible();
  await childFrame.click();
  await expect(page.getByLabel('Workflow', { exact: true })).toHaveValue('double.yaml');
  await expect(graph.locator('[data-node-kind="run"][data-active="true"]')).toHaveCount(1);
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 15000 });
  await expect(status.getByRole('region', { name: 'Execution result' })).toContainText('"total": 12');
  await expect(status).toContainText('6,');

  // Arbitrary-precision values and integer-like object keys survive the full path.
  const schema = { type: 'object', properties: { integer: { type: 'integer' }, decimal: { type: 'number' }, ordered: { type: 'object', additionalProperties: { type: 'integer' } } }, required: ['integer', 'decimal', 'ordered'] };
  const document = { input: { value: schema }, output: { result: schema }, graph: { nodes: { copy: { use: 'builtin:assign', with: { input: { source: 'value' }, output: { value: 'result' } } } }, edges: ['start -> copy', 'copy -> end'] } };
  await page.getByRole('button', { name: 'Load default example', exact: true }).click();
  await page.getByLabel('Format', { exact: true }).selectOption('json');
  await page.locator('#source-text').fill(JSON.stringify(document));
  await input.fill('{"value":{"integer":99999999999999999999999999999999999999999999999999,"decimal":1.00000000000000000000001,"ordered":{"2":2,"1":1,"z":3}}}');
  await expect(run).toBeEnabled();
  await run.click();
  await expect(status).toHaveAttribute('data-status', 'completed', { timeout: 15000 });
  const result = await status.getByRole('region', { name: 'Execution result' }).innerText();
  expect(result).toContain('99999999999999999999999999999999999999999999999999');
  expect(result).toContain('1.00000000000000000000001');
  expect(result.indexOf('"2"')).toBeLessThan(result.indexOf('"1"'));

  // Server input validation distinguishes 1.0 from integer; duplicate keys are local errors.
  const integerRoot = { input: { value: { type: 'integer' } }, output: {}, run: { executor: 'builtin', content: 'return Output()' } };
  await page.locator('#source-text').fill(JSON.stringify(integerRoot));
  await input.fill('{"value":1.0}');
  await run.click();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText('input_validation_failed');
  await input.fill('{"value":1,"value":2}');
  await run.click();
  await expect(page.getByRole('region', { name: 'Diagnostics', exact: true })).toContainText('invalid_input_json');

  // Failed executions and cooperative cancellation use terminal resource views.
  await page.locator('#source-text').fill(JSON.stringify({ ...integerRoot, run: { executor: 'builtin', content: 'raise ValueError("demo failure")' } }));
  await input.fill('{"value":1}');
  await run.click();
  await expect(status).toHaveAttribute('data-status', 'failed', { timeout: 15000 });
  await expect(status.getByRole('region', { name: 'Execution failure' })).toContainText('demo failure');
  await page.getByRole('button', { name: 'Load default example', exact: true }).click();
  await run.click();
  await expect(status).toHaveAttribute('data-status', 'running');
  await expect(status.locator('[data-frame-kind="iteration"]')).toHaveCount(2);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(status).toHaveAttribute('data-status', 'cancelled', { timeout: 15000 });
  expect(errors).toEqual([]);
  await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
  await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/workbench.png', import.meta.url)), fullPage: true });
  process.stdout.write('PASS: WASM compile/probes, CFG/navigation, live frames, HTTP execution, lossless JSON, errors, cooperative cancellation\n');
} catch (error) {
  if (page) {
    await mkdir(new URL('../.browser-test/', import.meta.url), { recursive: true });
    await page.screenshot({ path: fileURLToPath(new URL('../.browser-test/failure.png', import.meta.url)), fullPage: true }).catch(() => {});
  }
  process.stderr.write(`${hostLog}\n`);
  throw error;
} finally {
  await browser?.close();
  await vite?.close();
  if (host.exitCode === null) {
    host.kill('SIGTERM');
    await Promise.race([once(host, 'exit'), new Promise(resolve => { const timer = setTimeout(() => { host.kill('SIGKILL'); resolve(); }, 10000); timer.unref(); })]);
  }
}
