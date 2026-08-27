import {
  WeeklySelectorResultsError,
  normalizeLatestSubmissionRows,
  normalizePostCloseBenchmarkRows,
  scoreWeeklySelectorResults,
  verifyKitCatalogRow,
  verifyRevealedSelectorRound,
} from '../lib/weekly-selector-results.js';

export function createWeeklySelectorResultsHandler({
  env = process.env,
  fetchImpl = fetch,
} = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET');
      return send(response, 405, { error: 'Method not allowed' });
    }

    const config = weeklySelectorResultsConfig(env);
    if (!config.url || !config.publishableKey) {
      return send(response, 500, { error: 'Weekly selector results service is not configured' });
    }

    const requestedRoundId = parseRoundId(request);

    try {
      const headers = { apikey: config.publishableKey };
      const liveRows = requestedRoundId
        ? await fetchRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_rounds?'
          + 'select=round_id,environment,status,item_count,blind_manifest,blind_manifest_sha256,'
          + 'reveal_manifest,reveal_manifest_sha256,revealed_at&'
          + `round_id=eq.${encodeURIComponent(requestedRoundId)}&`
          + 'status=eq.revealed&limit=2')
        : await fetchRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_rounds?'
          + 'select=round_id,environment,status,item_count,blind_manifest,blind_manifest_sha256,'
          + 'reveal_manifest,reveal_manifest_sha256,revealed_at&'
          + `environment=eq.${encodeURIComponent(config.environment)}&`
          + 'status=eq.revealed&'
          + 'order=revealed_at.desc&limit=2');

      if (liveRows.length !== 1) throw new WeeklySelectorResultsError('revealed round count is invalid');
      const live = verifyRevealedSelectorRound(liveRows[0]);
      if (live.environment !== config.environment) {
        throw new WeeklySelectorResultsError('revealed round environment is invalid');
      }

      const catalogRows = await postRows(
        fetchImpl,
        config.url,
        headers,
        '/rest/v1/rpc/get_weekly_selector_kit_descriptor',
        {
          p_round_id: live.roundId,
          p_environment: config.environment,
        },
      );
      if (catalogRows.length !== 1) throw new WeeklySelectorResultsError('kit catalog row is missing');
      verifyKitCatalogRow(catalogRows[0], live);

      const submissionRows = await postRows(
        fetchImpl,
        config.url,
        headers,
        '/rest/v1/rpc/get_weekly_selector_latest_submissions_v2',
        {
          p_round_id: live.roundId,
          p_environment: config.environment,
        },
      );
      const benchmarkRows = await postRows(
        fetchImpl,
        config.url,
        headers,
        '/rest/v1/rpc/get_weekly_selector_benchmarks_v1',
        {
          p_round_id: live.roundId,
          p_environment: config.environment,
        },
      );

      const submissions = normalizeLatestSubmissionRows(
        submissionRows,
        live.roundId,
        config.environment,
      );
      const benchmarks = normalizePostCloseBenchmarkRows(
        benchmarkRows,
        live.roundId,
        config.environment,
      );
      const leaderboard = scoreWeeklySelectorResults({
        roundId: live.roundId,
        itemCount: live.itemCount,
        blindManifest: live.blindManifest,
        revealManifest: live.revealManifest,
        submissions: [...submissions, ...benchmarks],
      });
      return send(response, 200, leaderboard);
    } catch (error) {
      if (error instanceof WeeklySelectorResultsError) {
        return send(response, 502, { error: 'Weekly selector results unavailable' });
      }
      return send(response, 502, { error: 'Weekly selector results unavailable' });
    }
  };
}

export function weeklySelectorResultsConfig(env) {
  if (env.FOLDARIUM_ENV === 'preview') {
    return {
      url: normalizedOrigin(env.FOLDARIUM_PREVIEW_SUPABASE_URL),
      publishableKey: env.FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY
        || env.FOLDARIUM_PREVIEW_SUPABASE_ANON_KEY || '',
      environment: 'preview',
    };
  }
  if (env.FOLDARIUM_ENV === 'production') {
    return {
      url: normalizedOrigin(env.FOLDARIUM_PRODUCTION_SUPABASE_URL),
      publishableKey: env.FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY
        || env.FOLDARIUM_PRODUCTION_SUPABASE_ANON_KEY || '',
      environment: 'production',
    };
  }
  return {
    url: normalizedOrigin(env.SUPABASE_URL),
    publishableKey: env.SUPABASE_ANON_KEY || '',
    environment: 'development',
  };
}

function parseRoundId(request) {
  const query = request.query || {};
  const value = query.round_id ?? query.roundId;
  return typeof value === 'string' && value.trim() ? value.trim() : '';
}

function normalizedOrigin(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value.trim());
    const loopbackHttp = url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !loopbackHttp) || url.username || url.password) return '';
    return url.origin;
  } catch {
    return '';
  }
}

async function fetchRows(fetchImpl, baseUrl, headers, path) {
  const upstream = await fetchImpl(`${baseUrl}${path}`, { headers });
  if (!upstream.ok) throw new Error('upstream unavailable');
  const rows = await upstream.json();
  if (!Array.isArray(rows)) throw new Error('invalid upstream rows');
  return rows;
}

async function postRows(fetchImpl, baseUrl, headers, path, body) {
  const upstream = await fetchImpl(`${baseUrl}${path}`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!upstream.ok) throw new Error('upstream unavailable');
  const rows = await upstream.json();
  if (!Array.isArray(rows)) throw new Error('invalid upstream rows');
  return rows;
}

function send(response, status, value) {
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  return response.status(status).json(value);
}

export default createWeeklySelectorResultsHandler();
