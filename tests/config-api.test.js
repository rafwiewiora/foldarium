import test from 'node:test';
import assert from 'node:assert/strict';
import { createConfigHandler, resolveBrowserConfig } from '../api/config.js';

function productionEnv(overrides = {}) {
  return {
    VERCEL_ENV: 'production',
    VERCEL_GIT_COMMIT_SHA: '1234567abcdef',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co/',
    FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_production',
    ...overrides,
  };
}

function previewEnv(overrides = {}) {
  return {
    VERCEL_ENV: 'preview',
    VERCEL_GIT_COMMIT_SHA: 'abcdef1234567',
    ...overrides,
  };
}

function invoke(handler, method = 'GET') {
  const headers = {};
  let statusCode;
  let body;
  const response = {
    setHeader(name, value) {
      headers[name] = value;
    },
    status(value) {
      statusCode = value;
      return this;
    },
    json(value) {
      body = value;
      return this;
    },
  };
  handler({ method }, response);
  return { statusCode, headers, body };
}

test('returns the production browser config and derives its public structure URL', () => {
  assert.deepEqual(resolveBrowserConfig(productionEnv()), {
    url: 'https://production.supabase.co',
    publishableKey: 'sb_publishable_production',
    structureBaseUrl: 'https://production.supabase.co/storage/v1/object/public/structures',
    enabled: true,
    writable: true,
    deploymentEnvironment: 'production',
    commitSha: '1234567abcdef',
  });
});

test('Preview never falls back to production or server-side Supabase credentials', () => {
  const config = resolveBrowserConfig(previewEnv({
    ...productionEnv(),
    VERCEL_ENV: 'preview',
    VERCEL_GIT_COMMIT_SHA: 'abcdef1234567',
    SUPABASE_URL: 'https://server-only.supabase.co',
    SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_never_expose',
    REPLAY_PASSWORD: 'never expose',
  }));

  assert.deepEqual(config, {
    url: '',
    publishableKey: '',
    structureBaseUrl: '',
    enabled: false,
    writable: false,
    deploymentEnvironment: 'preview',
    commitSha: 'abcdef1234567',
  });
  assert.doesNotMatch(JSON.stringify(config), /server-only|sb_secret|never expose|production/);
});

test('Preview credentials default to read-only and require an explicit write opt-in', () => {
  const staging = {
    FOLDARIUM_PREVIEW_SUPABASE_URL: 'https://staging.supabase.co',
    FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_staging',
  };

  assert.deepEqual(resolveBrowserConfig(previewEnv(staging)), {
    url: 'https://staging.supabase.co',
    publishableKey: 'sb_publishable_staging',
    structureBaseUrl: 'https://staging.supabase.co/storage/v1/object/public/structures',
    enabled: true,
    writable: false,
    deploymentEnvironment: 'preview',
    commitSha: 'abcdef1234567',
  });
  assert.deepEqual(resolveBrowserConfig(previewEnv({
    ...staging,
    FOLDARIUM_PREVIEW_WRITES_ENABLED: '1',
    FOLDARIUM_PREVIEW_STRUCTURE_BASE_URL: 'https://assets.example.test/structures/',
  })), {
    url: 'https://staging.supabase.co',
    publishableKey: 'sb_publishable_staging',
    structureBaseUrl: 'https://assets.example.test/structures',
    enabled: true,
    writable: true,
    deploymentEnvironment: 'preview',
    commitSha: 'abcdef1234567',
  });
});

test('supports a legacy browser anon key but rejects unsafe URLs', () => {
  const anonPayload = Buffer.from(JSON.stringify({ role: 'anon' })).toString('base64url');
  const anonKey = `eyJheader.${anonPayload}.signature`;
  const config = resolveBrowserConfig(productionEnv({
    FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: '',
    FOLDARIUM_PRODUCTION_SUPABASE_ANON_KEY: anonKey,
  }));
  assert.equal(config.publishableKey, anonKey);

  for (const url of ['http://production.supabase.co', 'not a url', 'https://user:pass@example.test']) {
    assert.equal(resolveBrowserConfig(productionEnv({
      FOLDARIUM_PRODUCTION_SUPABASE_URL: url,
    })).enabled, false);
  }
});

test('rejects secret and legacy service-role keys even in a browser-key variable', () => {
  const servicePayload = Buffer.from(JSON.stringify({ role: 'service_role' })).toString('base64url');
  for (const key of ['sb_secret_never_expose', `eyJheader.${servicePayload}.signature`]) {
    const config = resolveBrowserConfig(productionEnv({
      FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: key,
    }));
    assert.equal(config.enabled, false);
    assert.equal(config.publishableKey, '');
    assert.doesNotMatch(JSON.stringify(config), /sb_secret|service_role/);
  }
});

test('serves only GET config responses without caching', () => {
  const handler = createConfigHandler({ env: productionEnv() });
  const get = invoke(handler);
  assert.equal(get.statusCode, 200);
  assert.equal(get.headers['Cache-Control'], 'no-store');
  assert.equal(get.headers['Content-Type'], 'application/json; charset=utf-8');
  assert.equal(get.body.publishableKey, 'sb_publishable_production');

  const post = invoke(handler, 'POST');
  assert.equal(post.statusCode, 405);
  assert.equal(post.headers.Allow, 'GET');
  assert.deepEqual(post.body, { error: 'Method not allowed' });
});
