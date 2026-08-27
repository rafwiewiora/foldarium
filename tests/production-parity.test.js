import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  checkProductionParity,
  parseOrigin,
  validateProductionConfig,
} from '../scripts/check-production-parity.mjs';

async function withTempDirectory(t) {
  const directory = await mkdtemp(path.join(tmpdir(), 'foldarium-parity-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

function productionConfig() {
  return {
    url: 'https://database.example',
    publishableKey: 'sb_publishable_example',
    structureBaseUrl: 'https://assets.example/structures',
    enabled: true,
    writable: true,
    deploymentEnvironment: 'production',
    commitSha: '',
  };
}

test('accepts only credential-free HTTPS production origins', () => {
  assert.equal(parseOrigin(['--origin', 'https://foldarium.example/path']), 'https://foldarium.example');
  assert.throws(() => parseOrigin(['http://foldarium.example']), /HTTPS/);
  assert.throws(() => parseOrigin(['https://user:pass@foldarium.example']), /without credentials/);
});

test('validates the public production config shape without pinning operator values', () => {
  assert.doesNotThrow(() => validateProductionConfig(productionConfig()));
  assert.throws(
    () => validateProductionConfig({ ...productionConfig(), deploymentEnvironment: 'preview' }),
    /non-production/,
  );
  assert.throws(
    () => validateProductionConfig({ ...productionConfig(), publishableKey: '' }),
    /publishableKey/,
  );
});

test('compares canonical source bytes with deployed responses', async t => {
  const rootDirectory = await withTempDirectory(t);
  await writeFile(path.join(rootDirectory, 'app.js'), 'same bytes\n');
  const fetchImpl = async url => {
    if (url.pathname === '/app.js') return new Response('same bytes\n');
    if (url.pathname === '/api/config') return Response.json(productionConfig());
    return new Response('missing', { status: 404 });
  };
  const result = await checkProductionParity({
    origin: 'https://foldarium.example',
    rootDirectory,
    targets: [['app.js', '/app.js']],
    fetchImpl,
    log() {},
  });
  assert.deepEqual(result, { checked: 1 });

  await writeFile(path.join(rootDirectory, 'app.js'), 'different\n');
  await assert.rejects(
    checkProductionParity({
      origin: 'https://foldarium.example',
      rootDirectory,
      targets: [['app.js', '/app.js']],
      fetchImpl,
      log() {},
    }),
    /Production parity failed/,
  );
});
