import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const loaderSource = await readFile(new URL('../supabase-config.js', import.meta.url), 'utf8');

async function runLoader({ status = 200, response = validConfig(), requestError = null } = {}) {
  const requests = [];
  const warnings = [];
  class XMLHttpRequest {
    open(method, url, async) {
      requests.push({ method, url, async });
    }
    send() {
      if (requestError) throw requestError;
      this.status = status;
      this.responseText = JSON.stringify(response);
    }
  }
  const window = {};
  vm.runInNewContext(loaderSource, {
    window,
    XMLHttpRequest,
    URL,
    Promise,
    console: { warn: (...values) => warnings.push(values.join(' ')) },
  });
  let readyConfig;
  let readyError;
  try {
    readyConfig = await window.FOLDARIUM_CONFIG_READY;
  } catch (error) {
    readyError = error;
  }
  return { config: window.FOLDARIUM_SUPABASE, readyConfig, readyError, requests, warnings };
}

function validConfig(overrides = {}) {
  return {
    url: 'https://staging.supabase.co',
    publishableKey: 'sb_publishable_staging',
    structureBaseUrl: 'https://staging.supabase.co/storage/v1/object/public/structures',
    enabled: true,
    writable: true,
    deploymentEnvironment: 'preview',
    commitSha: 'abcdef1234567',
    ...overrides,
  };
}

test('loads validated runtime config before the script completes', async () => {
  const result = await runLoader();
  assert.deepEqual(result.requests, [{ method: 'GET', url: '/api/config', async: false }]);
  assert.equal(result.config.url, 'https://staging.supabase.co');
  assert.equal(result.config.publishableKey, 'sb_publishable_staging');
  assert.equal(result.config.enabled, true);
  assert.equal(result.config.writable, true);
  assert.equal(Object.isFrozen(result.config), true);
  assert.equal(result.readyConfig, result.config);
  assert.equal(result.readyError, undefined);
  assert.deepEqual(result.warnings, []);
});

test('fails closed when runtime config cannot be loaded', async () => {
  for (const options of [
    { status: 503 },
    { requestError: new Error('offline') },
    { response: validConfig({ publishableKey: '' }) },
  ]) {
    const { config, readyConfig, readyError, warnings } = await runLoader(options);
    assert.equal(config.url, '');
    assert.equal(config.publishableKey, '');
    assert.equal(config.enabled, false);
    assert.equal(config.writable, false);
    assert.equal(readyConfig, undefined);
    assert.equal(typeof readyError?.message, 'string');
    assert.equal(warnings.length, 1);
  }
});

test('a read-only response retains public read credentials without enabling writes', async () => {
  const { config } = await runLoader({ response: validConfig({ writable: false }) });
  assert.equal(config.url, 'https://staging.supabase.co');
  assert.equal(config.publishableKey, 'sb_publishable_staging');
  assert.equal(config.enabled, true);
  assert.equal(config.writable, false);
});

test('a disabled response strips browser credentials', async () => {
  const { config } = await runLoader({
    response: validConfig({ enabled: false, writable: false }),
  });
  assert.equal(config.url, '');
  assert.equal(config.publishableKey, '');
  assert.equal(config.enabled, false);
});
