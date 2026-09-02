import test from 'node:test';
import assert from 'node:assert/strict';
import { createConfigHandler, resolveBrowserConfig } from '../api/config.js';

function productionEnv(overrides = {}) {
  return {
    FOLDARIUM_ENV: 'production',
    FOLDARIUM_COMMIT_SHA: '1234567abcdef',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co/',
    FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_production',
    ...overrides,
  };
}

function previewEnv(overrides = {}) {
  return {
    FOLDARIUM_ENV: 'preview',
    FOLDARIUM_COMMIT_SHA: 'abcdef1234567',
    ...overrides,
  };
}

function invoke(handler, method = 'GET', url = '/api/config') {
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
  handler({ method, url }, response);
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
    performanceBetaEnabled: false,
  });
});

test('Preview never falls back to production or server-side Supabase credentials', () => {
  const config = resolveBrowserConfig(previewEnv({
    ...productionEnv(),
    FOLDARIUM_ENV: 'preview',
    FOLDARIUM_COMMIT_SHA: 'abcdef1234567',
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
    performanceBetaEnabled: false,
  });
  assert.doesNotMatch(JSON.stringify(config), /server-only|sb_secret|never expose|production/);
});

test('does not infer deployment metadata from unrelated variables', () => {
  const config = resolveBrowserConfig({
    CLOUD_ENV: 'production',
    CLOUD_COMMIT_SHA: '1234567abcdef',
    FOLDARIUM_DEVELOPMENT_SUPABASE_URL: 'https://development.supabase.co',
    FOLDARIUM_DEVELOPMENT_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_development',
  });

  assert.equal(config.deploymentEnvironment, 'development');
  assert.equal(config.commitSha, '');
  assert.equal(config.url, 'https://development.supabase.co');
});

test('allows plain HTTP only for loopback development services', () => {
  const config = resolveBrowserConfig({
    FOLDARIUM_ENV: 'development',
    FOLDARIUM_DEVELOPMENT_SUPABASE_URL: 'http://127.0.0.1:54321',
    FOLDARIUM_DEVELOPMENT_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_local',
  });
  assert.equal(config.enabled, true);
  assert.equal(config.url, 'http://127.0.0.1:54321');
  assert.equal(
    config.structureBaseUrl,
    'http://127.0.0.1:54321/storage/v1/object/public/structures',
  );
  assert.equal(resolveBrowserConfig({
    FOLDARIUM_ENV: 'development',
    FOLDARIUM_DEVELOPMENT_SUPABASE_URL: 'http://example.test',
    FOLDARIUM_DEVELOPMENT_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_local',
  }).enabled, false);
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
    performanceBetaEnabled: false,
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
    performanceBetaEnabled: false,
  });
});

test('performance Preview can read production round data without enabling writes', () => {
  const env = previewEnv({
    FOLDARIUM_PREVIEW_SUPABASE_URL: 'https://staging.supabase.co',
    FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_staging',
    FOLDARIUM_PREVIEW_WRITES_ENABLED: '1',
  });
  const response = invoke(
    createConfigHandler({ env }),
    'GET',
    '/api/config?performance_source=production',
  );

  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.body, {
    url: 'https://staging.supabase.co',
    publishableKey: 'sb_publishable_staging',
    structureBaseUrl: 'https://staging.supabase.co/storage/v1/object/public/structures',
    enabled: true,
    writable: false,
    deploymentEnvironment: 'production',
    commitSha: 'abcdef1234567',
    performanceBetaEnabled: false,
  });
});

test('a deployment-controlled data environment can isolate a public beta project', () => {
  assert.deepEqual(resolveBrowserConfig(productionEnv({
    FOLDARIUM_WEEKLY_DATA_ENVIRONMENT: 'preview',
  })), {
    url: 'https://production.supabase.co',
    publishableKey: 'sb_publishable_production',
    structureBaseUrl: 'https://production.supabase.co/storage/v1/object/public/structures',
    enabled: true,
    writable: true,
    deploymentEnvironment: 'preview',
    commitSha: '1234567abcdef',
    performanceBetaEnabled: false,
  });
  assert.equal(resolveBrowserConfig(productionEnv({
    FOLDARIUM_WEEKLY_DATA_ENVIRONMENT: 'invalid',
  })).deploymentEnvironment, 'production');
});

test('a deployment can opt into queryless performance beta diagnostics', () => {
  const config = resolveBrowserConfig(productionEnv({
    FOLDARIUM_PERFORMANCE_BETA: '1',
  }));
  assert.equal(config.performanceBetaEnabled, true);
  assert.equal(config.enabled, true);
  assert.equal(config.writable, true);
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
