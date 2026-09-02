const ENVIRONMENT_CONFIG = Object.freeze({
  production: Object.freeze({
    url: 'FOLDARIUM_PRODUCTION_SUPABASE_URL',
    publishableKey: 'FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY',
    anonKey: 'FOLDARIUM_PRODUCTION_SUPABASE_ANON_KEY',
    structureBaseUrl: 'FOLDARIUM_PRODUCTION_STRUCTURE_BASE_URL',
  }),
  preview: Object.freeze({
    url: 'FOLDARIUM_PREVIEW_SUPABASE_URL',
    publishableKey: 'FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY',
    anonKey: 'FOLDARIUM_PREVIEW_SUPABASE_ANON_KEY',
    structureBaseUrl: 'FOLDARIUM_PREVIEW_STRUCTURE_BASE_URL',
    writesEnabled: 'FOLDARIUM_PREVIEW_WRITES_ENABLED',
  }),
  development: Object.freeze({
    url: 'FOLDARIUM_DEVELOPMENT_SUPABASE_URL',
    publishableKey: 'FOLDARIUM_DEVELOPMENT_SUPABASE_PUBLISHABLE_KEY',
    anonKey: 'FOLDARIUM_DEVELOPMENT_SUPABASE_ANON_KEY',
    structureBaseUrl: 'FOLDARIUM_DEVELOPMENT_STRUCTURE_BASE_URL',
    writesEnabled: 'FOLDARIUM_DEVELOPMENT_WRITES_ENABLED',
  }),
});

export function resolveBrowserConfig(env = {}, { readOnlyProductionData = false } = {}) {
  const credentialEnvironment = normalizeEnvironment(env.FOLDARIUM_ENV);
  const deploymentEnvironment = explicitEnvironment(env.FOLDARIUM_WEEKLY_DATA_ENVIRONMENT)
    || (readOnlyProductionData && credentialEnvironment === 'preview'
      ? 'production'
      : credentialEnvironment);
  const names = ENVIRONMENT_CONFIG[credentialEnvironment];
  const commitSha = publicCommitSha(env.FOLDARIUM_COMMIT_SHA);
  const url = normalizedHttpsUrl(env[names.url]);
  const publishableKey = publicBrowserKey(env[names.publishableKey] || env[names.anonKey]);
  const writesEnabled = !readOnlyProductionData
    && (!names.writesEnabled || env[names.writesEnabled] === '1');
  const performanceBetaEnabled = env.FOLDARIUM_PERFORMANCE_BETA === '1';

  if (!url || !publishableKey) {
    return disabledConfig(deploymentEnvironment, commitSha, performanceBetaEnabled);
  }

  const configuredStructureUrl = env[names.structureBaseUrl];
  const structureBaseUrl = configuredStructureUrl
    ? normalizedHttpsUrl(configuredStructureUrl)
    : `${url}/storage/v1/object/public/structures`;
  if (!structureBaseUrl) {
    return disabledConfig(deploymentEnvironment, commitSha, performanceBetaEnabled);
  }

  return {
    url,
    publishableKey,
    structureBaseUrl,
    enabled: true,
    writable: writesEnabled,
    deploymentEnvironment,
    commitSha,
    performanceBetaEnabled,
  };
}

export function createConfigHandler({ env = process.env } = {}) {
  return function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET');
      return response.status(405).json({ error: 'Method not allowed' });
    }
    const query = new URL(request.url || '/api/config', 'https://foldarium.invalid').searchParams;
    const readOnlyProductionData = normalizeEnvironment(env.FOLDARIUM_ENV) === 'preview'
      && query.get('performance_source') === 'production';
    return response.status(200).json(resolveBrowserConfig(env, { readOnlyProductionData }));
  };
}

function normalizeEnvironment(value) {
  return Object.hasOwn(ENVIRONMENT_CONFIG, value) ? value : 'development';
}

function explicitEnvironment(value) {
  return Object.hasOwn(ENVIRONMENT_CONFIG, value) ? value : null;
}

function normalizedHttpsUrl(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value.trim());
    const loopbackHttp = url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !loopbackHttp) || url.username || url.password) return '';
    return url.toString().replace(/\/$/, '');
  } catch {
    return '';
  }
}

function publicBrowserKey(value) {
  if (typeof value !== 'string') return '';
  const key = value.trim();
  if (/^sb_publishable_[A-Za-z0-9_-]+$/.test(key)) return key;
  const parts = key.split('.');
  if (parts.length !== 3) return '';
  try {
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    return payload?.role === 'anon' ? key : '';
  } catch {
    return '';
  }
}

function publicCommitSha(value) {
  return typeof value === 'string' && /^[0-9a-f]{7,64}$/i.test(value) ? value : '';
}

function disabledConfig(deploymentEnvironment, commitSha, performanceBetaEnabled = false) {
  return {
    url: '',
    publishableKey: '',
    structureBaseUrl: '',
    enabled: false,
    writable: false,
    deploymentEnvironment,
    commitSha,
    performanceBetaEnabled,
  };
}

export default createConfigHandler();
