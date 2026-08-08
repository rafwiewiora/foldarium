import { createHash, timingSafeEqual } from 'node:crypto';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function createReplayHandler({ env = process.env, fetchImpl = fetch } = {}) {
  return async function handler(request, response) {
    response.setHeader('Cache-Control', 'no-store');

    if (request.method !== 'POST') return send(response, 405, { error: 'Method not allowed' });

    const body = typeof request.body === 'string' ? safeJson(request.body) : request.body;
    if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.password !== 'string') {
      return send(response, 400, { error: 'Invalid request' });
    }

    const { password: REPLAY_PASSWORD, url: SUPABASE_URL, serviceRoleKey: SUPABASE_SERVICE_ROLE_KEY } = replayConfig(env);
    if (!REPLAY_PASSWORD || !SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
      return send(response, 500, { error: 'Replay service is not configured' });
    }
    if (!secureEqual(body.password, REPLAY_PASSWORD)) {
      return send(response, 401, { error: 'Invalid password' });
    }

    try {
      const fetchRows = async path => {
        const upstream = await fetchImpl(`${SUPABASE_URL}${path}`, {
          headers: serviceHeaders(SUPABASE_SERVICE_ROLE_KEY),
        });
        if (!upstream.ok) throw new Error('upstream unavailable');
        const rows = await upstream.json();
        if (!Array.isArray(rows)) throw new Error('invalid upstream rows');
        return rows;
      };
      if (body.action === 'sessions') {
        const [classic, weekly] = await Promise.all([
          fetchRows('/rest/v1/replay_quiz_sessions_safe?select=session_id,session_kind,'
            + 'participant_hash,display_name_hash,source,difficulty,round_id,started_at,'
            + 'completed_at,has_recorded_name&order=started_at.desc&limit=100'),
          fetchRows('/rest/v1/replay_weekly_sessions_safe?select=session_id,session_kind,'
            + 'participant_hash,display_name_hash,source,difficulty,round_id,started_at,'
            + 'completed_at,has_recorded_name&order=started_at.desc&limit=100'),
        ]);
        const sessions = [...classic, ...weekly]
          .sort((left, right) => String(right.started_at).localeCompare(String(left.started_at)))
          .slice(0, 100);
        return send(response, 200, sessions);
      }
      if (body.action === 'answers' && validSessionId(body.session_id)) {
        const id = encodeURIComponent(body.session_id);
        return send(response, 200, await fetchRows(
          '/rest/v1/replay_quiz_answers_safe?select=id,session_id,question_index,item_id,'
          + 'picked_none,picked_sample,picked_correct,answered_at,viewer_trace,app_trace,'
          + `app_state,active_pane_id&session_id=eq.${id}&viewer_trace=not.is.null`
          + '&order=question_index.asc',
        ));
      }
      if (body.action === 'weekly-attempts' && validSessionId(body.session_id)) {
        const id = encodeURIComponent(body.session_id);
        return send(response, 200, await fetchRows(
          '/rest/v1/replay_weekly_vote_attempts_safe?select=vote_attempt_id,session_id,'
          + 'round_id,participant_hash,display_name_hash,item_id,question_index,choice_id,'
          + `picked_none,viewer_trace,app_state,active_pane_id,submitted_at&session_id=eq.${id}`
          + '&viewer_trace=not.is.null&order=question_index.asc,submitted_at.asc',
        ));
      }
      if (body.action === 'suggestions') {
        return send(response, 200, await fetchRows(
          '/rest/v1/replay_user_suggestions_safe?select=suggestion_id,participant_hash,'
          + 'display_name_hash,quiz_session_id,weekly_session_id,context,item_id,page_path,'
          + 'suggestion_text,app_state,viewer_snapshot,viewer_trace_tail,submitted_at'
          + '&order=submitted_at.desc&limit=100',
        ));
      }
      return send(response, 400, { error: 'Invalid action' });
    } catch {
      return send(response, 502, { error: 'Replay data unavailable' });
    }
  };
}

function validSessionId(value) {
  return typeof value === 'string' && UUID.test(value);
}

function replayConfig(env) {
  if (env.VERCEL_ENV === 'preview') {
    return {
      password: env.FOLDARIUM_PREVIEW_REPLAY_PASSWORD,
      url: env.FOLDARIUM_PREVIEW_SUPABASE_URL,
      serviceRoleKey: env.FOLDARIUM_PREVIEW_SUPABASE_SERVICE_ROLE_KEY,
    };
  }
  if (env.VERCEL_ENV === 'production') {
    return {
      password: env.FOLDARIUM_PRODUCTION_REPLAY_PASSWORD || env.REPLAY_PASSWORD,
      url: env.FOLDARIUM_PRODUCTION_SUPABASE_URL || env.SUPABASE_URL,
      serviceRoleKey: env.FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY
        || env.SUPABASE_SERVICE_ROLE_KEY,
    };
  }
  return {
    password: env.FOLDARIUM_DEVELOPMENT_REPLAY_PASSWORD || env.REPLAY_PASSWORD,
    url: env.FOLDARIUM_DEVELOPMENT_SUPABASE_URL || env.SUPABASE_URL,
    serviceRoleKey: env.FOLDARIUM_DEVELOPMENT_SUPABASE_SERVICE_ROLE_KEY
      || env.SUPABASE_SERVICE_ROLE_KEY,
  };
}

function serviceHeaders(key) {
  const headers = { apikey: key };
  if (!key.startsWith('sb_secret_')) headers.Authorization = `Bearer ${key}`;
  return headers;
}

function secureEqual(left, right) {
  const digest = value => createHash('sha256').update(value).digest();
  return timingSafeEqual(digest(left), digest(right));
}

function safeJson(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function send(response, status, value) {
  return response.status(status).json(value);
}

export default createReplayHandler();
