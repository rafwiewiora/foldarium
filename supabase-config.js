(function loadFoldariumRuntimeConfig() {
  const disabled = Object.freeze({
    url: '',
    publishableKey: '',
    structureBaseUrl: '',
    enabled: false,
    writable: false,
    deploymentEnvironment: 'unknown',
    commitSha: '',
    performanceBetaEnabled: false,
  });

  window.FOLDARIUM_SUPABASE = disabled;
  let loadError = null;

  try {
    // Quiz startup consumes this global as soon as this script's load event fires.
    // A synchronous same-origin request keeps that existing contract race-free.
    const request = new XMLHttpRequest();
    const search = window.location?.search || '';
    const diagnosticsProduction = /(?:^|[?&])perf(?:=|&|$)/.test(search)
      && !/(?:^|[?&])record_performance=1(?:&|$)/.test(search);
    request.open(
      'GET',
      diagnosticsProduction ? '/api/config?performance_source=production' : '/api/config',
      false,
    );
    request.send(null);
    if (request.status !== 200) throw new Error('runtime config unavailable');

    const config = JSON.parse(request.responseText);
    if (!isRuntimeConfig(config)) throw new Error('invalid runtime config');

    window.FOLDARIUM_SUPABASE = Object.freeze({
      url: config.enabled ? config.url : '',
      publishableKey: config.enabled ? config.publishableKey : '',
      structureBaseUrl: config.enabled ? config.structureBaseUrl : '',
      enabled: config.enabled,
      writable: config.writable,
      deploymentEnvironment: config.deploymentEnvironment,
      commitSha: config.commitSha,
      performanceBetaEnabled: config.performanceBetaEnabled,
    });
  } catch (error) {
    loadError = error;
    console.warn('Foldarium remote services are disabled:', error.message);
  }

  window.FOLDARIUM_CONFIG_READY = loadError
    ? Promise.reject(loadError)
    : Promise.resolve(window.FOLDARIUM_SUPABASE);
  // Consumers may choose not to await remote persistence during local-only use.
  void window.FOLDARIUM_CONFIG_READY.catch(() => {});

  function isRuntimeConfig(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    if (typeof value.enabled !== 'boolean' || typeof value.writable !== 'boolean') return false;
    if (typeof value.deploymentEnvironment !== 'string' || typeof value.commitSha !== 'string'
      || typeof value.performanceBetaEnabled !== 'boolean') return false;
    if (typeof value.url !== 'string'
      || typeof value.publishableKey !== 'string'
      || typeof value.structureBaseUrl !== 'string') return false;
    if (!value.enabled) return !value.writable;
    return isHttpsUrl(value.url) && isHttpsUrl(value.structureBaseUrl) && value.publishableKey.length > 0;
  }

  function isHttpsUrl(value) {
    try {
      return new URL(value).protocol === 'https:';
    } catch {
      return false;
    }
  }
}());
