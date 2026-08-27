import {
  ALLOWED_ROUND_ID,
  ContractError,
  buildClientBundle,
  parseIntegrityDescriptor,
  parseSupabaseObjectUri,
  verifyArtifactEnvelope,
  verifyIntegrityDescriptor,
  verifyLiveRoundState,
} from '../lib/private-evaluation-contract.js';
import {
  buildWeeklyQuestionResults,
  enrichVotesWithSelectionKinds,
  LEGACY_V4_ROUND_ID,
  WeeklyResultsError,
  scoreWeeklyResults,
} from '../lib/weekly-results.js';

export function createPrivateEvaluationHandler({
  env = process.env,
  fetchImpl = fetch,
} = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');
    if (env.FOLDARIUM_ENV !== 'preview') return send(response, 404, { error: 'Not found' });
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST');
      return send(response, 405, { error: 'Method not allowed' });
    }

    const config = privateEvaluationConfig(env);
    if (!config.url || !config.serviceRoleKey || !config.descriptorRaw) {
      return send(response, 500, { error: 'Private evaluation service is not configured' });
    }

    try {
      const descriptorRecord = parseIntegrityDescriptor(config.descriptorRaw);
      const headers = serviceHeaders(config.serviceRoleKey);
      const liveRows = await fetchRows(fetchImpl, config.url, headers,
        '/rest/v1/weekly_quiz_rounds?'
        + 'select=round_id,campaign_id,environment,status,opens_at,closes_at,'
        + 'blind_manifest_sha256,reveal_manifest,reveal_manifest_sha256,metadata,item_count,revealed_at&'
        + `round_id=eq.${encodeURIComponent(ALLOWED_ROUND_ID)}&limit=2`);
      if (liveRows.length !== 1) throw new ContractError('live round count is invalid');

      const live = verifyLiveRoundState(liveRows[0]);
      const descriptor = verifyIntegrityDescriptor(descriptorRecord, live);
      const { bucket, objectPath } = parseSupabaseObjectUri(
        descriptor.artifactObjectUri,
        descriptor.artifactSha256,
      );
      const artifactResponse = await fetchImpl(
        `${config.url}/storage/v1/object/authenticated/${encodeURIComponent(bucket)}/${objectPath}`,
        { headers },
      );
      if (!artifactResponse.ok) throw new ContractError('artifact download failed');
      const artifactBytes = Buffer.from(await artifactResponse.arrayBuffer());
      const verified = verifyArtifactEnvelope(artifactBytes, descriptor, live);
      const bundle = buildClientBundle({
        evaluationId: verified.evaluationId,
        campaignId: live.campaignId,
        opensAt: live.opensAt,
        closesAt: live.closesAt,
        blindManifest: verified.blindManifest,
        blindManifestSha256: live.blindManifestSha256,
        revealManifest: verified.revealManifest,
        revealManifestSha256: verified.revealManifestSha256,
        answerOverlays: verified.answerOverlays,
        itemCount: descriptor.itemCount,
        choiceCount: descriptor.choiceCount,
      });
      const [votes, voteAttempts, currentSessions, legacySessions] = await Promise.all([
        fetchAllRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_votes?'
          + 'select=round_id,user_id,item_id,choice_id,picked_none&'
          + `round_id=eq.${encodeURIComponent(ALLOWED_ROUND_ID)}`),
        fetchAllRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_vote_attempts?'
          + 'select=round_id,user_id,item_id,choice_id,picked_none,app_state,submitted_at&'
          + `round_id=eq.${encodeURIComponent(ALLOWED_ROUND_ID)}`),
        fetchAllRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_sessions?'
          + 'select=round_id,user_id,display_name,initial_app_state&'
          + `round_id=eq.${encodeURIComponent(ALLOWED_ROUND_ID)}`),
        fetchAllRows(fetchImpl, config.url, headers,
          '/rest/v1/weekly_quiz_sessions?'
          + 'select=round_id,user_id,display_name&'
          + `round_id=eq.${encodeURIComponent(LEGACY_V4_ROUND_ID)}`),
      ]);
      const votesWithSelectionKinds = enrichVotesWithSelectionKinds(votes, voteAttempts);
      bundle.weekly_leaderboard = scoreWeeklyResults({
        roundId: ALLOWED_ROUND_ID,
        itemCount: descriptor.itemCount,
        blindManifest: verified.blindManifest,
        revealManifest: verified.revealManifest,
        votes: votesWithSelectionKinds,
        currentSessions,
        legacySessions,
      });
      bundle.weekly_question_results = buildWeeklyQuestionResults({
        roundId: ALLOWED_ROUND_ID,
        itemCount: descriptor.itemCount,
        blindManifest: verified.blindManifest,
        revealManifest: verified.revealManifest,
        votes: votesWithSelectionKinds,
        currentSessions,
        legacySessions,
      });
      return send(response, 200, bundle);
    } catch (error) {
      if (error instanceof ContractError || error instanceof WeeklyResultsError) {
        return send(response, 502, { error: 'Private evaluation verification failed' });
      }
      return send(response, 502, { error: 'Private evaluation unavailable' });
    }
  };
}

export function privateEvaluationConfig(env) {
  if (env.FOLDARIUM_ENV !== 'preview') {
    return { url: '', serviceRoleKey: '', descriptorRaw: '' };
  }
  return {
    url: normalizedOrigin(
      env.FOLDARIUM_PRODUCTION_SUPABASE_URL || env.SUPABASE_URL,
    ),
    serviceRoleKey: env.FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY
      || env.SUPABASE_SERVICE_ROLE_KEY
      || '',
    descriptorRaw: env.FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR || '',
  };
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

export default createPrivateEvaluationHandler();
