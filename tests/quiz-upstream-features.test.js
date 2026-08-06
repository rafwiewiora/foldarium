import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const readApp = () => readFile(new URL('../app.js', import.meta.url), 'utf8');
const readHtml = () => readFile(new URL('../index.html', import.meta.url), 'utf8');

test('ports Grid UI and balanced session source contracts', async () => {
  const [app, html] = await Promise.all([readApp(), readHtml()]);

  assert.match(app, /const HARD_MIX = \{ 'game-able': 0\.40, 'all-wrong': 0\.45, 'all-correct': 0\.15 \};/);
  assert.match(app, /function drawSession\(\)/);
  assert.match(app, /function gridEntriesFor\(method\)/);
  assert.match(app, /const LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'\.split\(''\);/);
  assert.match(html, /data-m="grid"/);
});

test('retains local persistence, trace, and Storage integration points', async () => {
  const app = await readApp();

  assert.match(app, /researchBackend\(\)\?\.recordAnswer/);
  assert.match(app, /viewerTraceRecorder\?\.stop\(\)/);
  assert.match(app, /window\.foldariumAssetUrl/);
});

test('allocates exact hard-session quotas by deterministic largest remainder', async () => {
  const app = await readApp();
  const start = app.indexOf('function hardSessionQuotas');
  assert.notEqual(start, -1, 'hardSessionQuotas production helper must exist');
  const end = app.indexOf('\n}', start) + 2;
  const hardSessionQuotas = vm.runInNewContext(`(${app.slice(start, end)})`);

  const quotas = hardSessionQuotas(30, {
    'game-able': 0.40,
    'all-wrong': 0.45,
    'all-correct': 0.15,
  });

  assert.deepEqual({ ...quotas }, {
    'game-able': 12,
    'all-wrong': 14,
    'all-correct': 4,
  });
  assert.equal(Object.values(quotas).reduce((total, count) => total + count, 0), 30);
});
