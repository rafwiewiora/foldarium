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

    const { REPLAY_PASSWORD, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY } = env;
    if (!REPLAY_PASSWORD || !SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
      return send(response, 500, { error: 'Replay service is not configured' });
    }
    if (!secureEqual(body.password, REPLAY_PASSWORD)) {
      return send(response, 401, { error: 'Invalid password' });
    }

    let path;
    if (body.action === 'sessions') {
      path = '/rest/v1/quiz_sessions?select=id,user_id,source,difficulty,started_at,completed_at'
        + '&order=started_at.desc&limit=100';
    } else if (body.action === 'answers' && typeof body.session_id === 'string' && UUID.test(body.session_id)) {
      const id = encodeURIComponent(body.session_id);
      path = '/rest/v1/quiz_answers?select=id,session_id,question_index,item_id,picked_none,'
        + 'picked_sample,picked_correct,answered_at,viewer_trace'
        + `&session_id=eq.${id}&viewer_trace=not.is.null&order=question_index.asc`;
    } else {
      return send(response, 400, { error: 'Invalid action' });
    }

    try {
      const upstream = await fetchImpl(`${SUPABASE_URL}${path}`, {
        headers: serviceHeaders(SUPABASE_SERVICE_ROLE_KEY),
      });
      if (!upstream.ok) return send(response, 502, { error: 'Replay data unavailable' });
      return send(response, 200, await upstream.json());
    } catch {
      return send(response, 502, { error: 'Replay data unavailable' });
    }
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
