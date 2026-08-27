import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  createFoldariumServer,
  resolveServerConfig,
} from '../server.mjs';

async function startServer(t, options) {
  const server = createFoldariumServer({
    logger: { error() {} },
    ...options,
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => new Promise((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
  }));
  const { port } = server.address();
  return `http://127.0.0.1:${port}`;
}

async function fixtureRoot(t) {
  const parent = await mkdtemp(join(tmpdir(), 'foldarium-server-'));
  const root = join(parent, 'public');
  await mkdir(root);
  await Promise.all([
    writeFile(join(root, 'index.html'), '<h1>weekly</h1>'),
    writeFile(join(root, 'weekly-retrospectives.html'), '<h1>archive</h1>'),
    writeFile(join(root, 'app.js'), 'export const ready = true;\n'),
    writeFile(join(parent, 'secret.txt'), 'not public'),
  ]);
  t.after(() => rm(parent, { recursive: true, force: true }));
  return root;
}

test('serves static files and maps weekly entry routes', async t => {
  const rootDirectory = await fixtureRoot(t);
  const origin = await startServer(t, { rootDirectory, apiHandlers: {} });

  const weekly = await fetch(`${origin}/weekly?round=current`);
  assert.equal(weekly.status, 200);
  assert.equal(await weekly.text(), '<h1>weekly</h1>');
  assert.match(weekly.headers.get('content-type'), /^text\/html/);

  const retrospective = await fetch(`${origin}/weekly/retrospectives/weekly-2026-08-20`);
  assert.equal(retrospective.status, 200);
  assert.equal(await retrospective.text(), '<h1>archive</h1>');

  const head = await fetch(`${origin}/app.js`, { method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get('content-length'), '27');
  assert.equal(await head.text(), '');
});

test('does not serve traversal paths or unknown API routes as static files', async t => {
  const rootDirectory = await fixtureRoot(t);
  const origin = await startServer(t, { rootDirectory, apiHandlers: {} });

  const traversal = await fetch(`${origin}/%2e%2e%2Fsecret.txt`);
  assert.equal(traversal.status, 404);
  assert.doesNotMatch(await traversal.text(), /not public/);

  const api = await fetch(`${origin}/api/missing`);
  assert.equal(api.status, 404);
  assert.deepEqual(await api.json(), { error: 'Not found' });
});

test('adapts API query, JSON body, and response helpers', async t => {
  const rootDirectory = await fixtureRoot(t);
  const apiHandlers = {
    echo(request, response) {
      response.status(201).json({
        body: request.body,
        query: request.query,
        url: request.url,
      });
    },
    'weekly-selector'(request, response) {
      response.status(200).json({ url: request.url });
    },
  };
  const origin = await startServer(t, { rootDirectory, apiHandlers });

  const response = await fetch(`${origin}/api/echo?tag=one&tag=two`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ok: true }),
  });
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), {
    body: { ok: true },
    query: { tag: ['one', 'two'] },
    url: '/api/echo?tag=one&tag=two',
  });

  const nested = await fetch(`${origin}/api/weekly-selector/rounds/current`);
  assert.equal(nested.status, 200);
  assert.deepEqual(await nested.json(), {
    url: '/api/weekly-selector/rounds/current',
  });
});

test('dispatches the existing API handler map', async t => {
  const rootDirectory = await fixtureRoot(t);
  const origin = await startServer(t, { rootDirectory });

  const response = await fetch(`${origin}/api/config`);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(typeof (await response.json()).enabled, 'boolean');
});

test('rejects malformed JSON and request bodies over the configured limit', async t => {
  const rootDirectory = await fixtureRoot(t);
  const apiHandlers = {
    echo(request, response) {
      response.status(200).json(request.body);
    },
  };
  const origin = await startServer(t, {
    rootDirectory,
    apiHandlers,
    bodyLimitBytes: 8,
  });

  const malformed = await fetch(`${origin}/api/echo`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{bad}',
  });
  assert.equal(malformed.status, 400);
  assert.deepEqual(await malformed.json(), { error: 'Invalid request body' });

  const oversized = await fetch(`${origin}/api/echo`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: '123456789',
  });
  assert.equal(oversized.status, 413);
  assert.deepEqual(await oversized.json(), { error: 'Request body is too large' });
});

test('reads host and port from the process-style environment', () => {
  assert.deepEqual(resolveServerConfig({}), {
    host: '127.0.0.1',
    port: 4319,
  });
  assert.deepEqual(resolveServerConfig({ HOST: '0.0.0.0', PORT: '4321' }), {
    host: '0.0.0.0',
    port: 4321,
  });
  assert.throws(() => resolveServerConfig({ PORT: '4321x' }), /PORT/);
});
