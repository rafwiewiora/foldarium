import {
  scorePlayForFunResults,
  validateArchiveRoundId,
  verifyRevealedPlayForFunRound,
  WeeklyPlayForFunResultsError,
  WeeklyResultsError,
} from '../lib/weekly-play-for-fun-results.js';

export function createWeeklyPlayForFunResultsHandler({
  env = process.env,
  fetchImpl = fetch,
} = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET');
      return send(response, 405, { error: 'Method not allowed' });
    }

    let roundId;
    try {
      roundId = parseRoundId(request.query?.round_id);
    } catch {
      return send(response, 400, { error: 'Invalid request' });
    }

    const config = weeklyPlayForFunResultsConfig(env);
    if (!config.url || !config.serviceRoleKey) {
      return send(response, 503, { error: 'Play-for-fun results unavailable' });
    }

    try {
      const headers = serviceHeaders(config.serviceRoleKey);
      const roundRows = await fetchRows(
        fetchImpl,
        config.url,
        headers,
        '/rest/v1/weekly_quiz_rounds?'
          + 'select=round_id,status,reveal_manifest,reveal_manifest_sha256,item_count,revealed_at&'
          + `round_id=eq.${encodeURIComponent(roundId)}&limit=2`,
      );
      if (roundRows.length !== 1) {
        throw new WeeklyPlayForFunResultsError('round count is invalid');
      }
      const live = verifyRevealedPlayForFunRound(roundRows[0], roundId);

      const [sessions, voteAttempts] = await Promise.all([
        fetchAllRows(
          fetchImpl,
          config.url,
          headers,
          '/rest/v1/weekly_quiz_post_reveal_sessions?'
            + 'select=session_id,round_id,user_id,display_name,initial_app_state,started_at&'
            + `round_id=eq.${encodeURIComponent(roundId)}&`
            + 'order=started_at.asc,session_id.asc',
        ),
        fetchAllRows(
          fetchImpl,
          config.url,
          headers,
          '/rest/v1/weekly_quiz_post_reveal_vote_attempts?'
            + 'select=vote_attempt_id,session_id,round_id,user_id,item_id,choice_id,picked_none,submitted_at&'
            + `round_id=eq.${encodeURIComponent(roundId)}&`
            + 'order=submitted_at.asc,vote_attempt_id.asc',
        ),
      ]);

      const result = scorePlayForFunResults({
        roundId: live.roundId,
        itemCount: live.itemCount,
        revealManifest: live.revealManifest,
        sessions,
        voteAttempts,
      });
      return send(response, 200, result);
    } catch (error) {
      if (error instanceof WeeklyPlayForFunResultsError
        || error instanceof WeeklyResultsError) {
        return send(response, 502, { error: 'Play-for-fun results unavailable' });
      }
      return send(response, 502, { error: 'Play-for-fun results unavailable' });
    }
  };
}

export function weeklyPlayForFunResultsConfig(env) {
  return {
    url: normalizedOrigin(
      env.FOLDARIUM_PRODUCTION_SUPABASE_URL || env.SUPABASE_URL,
    ),
    serviceRoleKey: normalizedServiceRoleKey(
      env.FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY
        || env.SUPABASE_SERVICE_ROLE_KEY,
    ),
  };
}

function parseRoundId(value) {
  if (value == null) throw new WeeklyPlayForFunResultsError('round_id is missing');
  if (Array.isArray(value) || typeof value !== 'string' || !value) {
    throw new WeeklyPlayForFunResultsError('round_id is invalid');
  }
  return validateArchiveRoundId(value, 'round_id');
}

function normalizedOrigin(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'https:' || url.username || url.password) return '';
    return url.origin;
  } catch {
    return '';
  }
}

function normalizedServiceRoleKey(value) {
  if (typeof value !== 'string' || !value) return '';
  if (value.startsWith('sb_secret_') && value.length > 'sb_secret_'.length) return value;
  const parts = value.split('.');
  if (parts.length !== 3) return '';
  try {
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    return payload?.role === 'service_role' ? value : '';
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

async function fetchAllRows(fetchImpl, baseUrl, headers, path) {
  const pageSize = 1000;
  const maxRows = 100000;
  const rows = [];
  for (let offset = 0; offset < maxRows; offset += pageSize) {
    const page = await fetchRows(fetchImpl, baseUrl, {
      ...headers,
      Range: `${offset}-${offset + pageSize - 1}`,
    }, path);
    rows.push(...page);
    if (page.length < pageSize) return rows;
  }
  throw new Error('upstream row limit exceeded');
}

function serviceHeaders(key) {
  const headers = { apikey: key };
  if (!key.startsWith('sb_secret_')) headers.Authorization = `Bearer ${key}`;
  return headers;
}

function send(response, status, value) {
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  return response.status(status).json(value);
}

export default createWeeklyPlayForFunResultsHandler();
