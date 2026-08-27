import test from 'node:test';
import assert from 'node:assert/strict';
import { createReplayHandler } from '../api/replay.js';

const configuredEnv = {
  REPLAY_PASSWORD: 'correct horse',
  SUPABASE_URL: 'https://example.supabase.co',
  SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_test',
};

function invoke(handler, body, method = 'POST') {
  const headers = {};
  let statusCode;
  let responseBody;
  const response = {
    setHeader(name, value) {
      headers[name] = value;
    },
    status(value) {
      statusCode = value;
      return this;
    },
    json(value) {
      responseBody = value;
      return this;
    },
  };

  return handler({ method, body }, response).then(() => ({
    statusCode,
    headers,
    body: JSON.stringify(responseBody),
  }));
}

function failIfCalled() {
  return () => { throw new Error('Supabase must not be called'); };
}

function recordingFetch(body, options = {}) {
  async function fetchImpl(url, requestOptions) {
    fetchImpl.calls.push({ url, headers: requestOptions.headers });
    fetchImpl.url = url;
    fetchImpl.headers = requestOptions.headers;
    return {
      ok: options.ok ?? true,
      json: async () => body,
    };
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

function handler({ env = configuredEnv, fetchImpl = failIfCalled() } = {}) {
  return createReplayHandler({ env, fetchImpl });
}

test('rejects an invalid replay password without calling Supabase', async () => {
  const response = await invoke(handler(), {
    password: 'wrong',
    action: 'sessions',
  });

  assert.equal(response.statusCode, 401);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body, '{"error":"Invalid password"}');
});

test('lists recent classic and weekly safe sessions without raw user identities', async () => {
  const fetchImpl = recordingFetch([{ id: 'session-1' }]);
  const response = await invoke(handler({ fetchImpl }), {
    password: 'correct horse',
    action: 'sessions',
  });

  assert.equal(response.statusCode, 200);
  assert.equal(fetchImpl.calls.length, 2);
  assert.match(fetchImpl.calls[0].url, /replay_quiz_sessions_safe/);
  assert.match(fetchImpl.calls[1].url, /replay_weekly_sessions_safe/);
  assert.doesNotMatch(fetchImpl.calls.map(call => call.url).join(' '), /user_id|display_name,/);
  assert.equal(fetchImpl.headers.apikey, 'sb_secret_test');
  assert.equal(fetchImpl.headers.Authorization, undefined);
  assert.doesNotMatch(response.body, /sb_secret_test|correct horse/);
});

test('uses a bearer header with legacy JWT service-role keys', async () => {
  const fetchImpl = recordingFetch([]);
  await invoke(handler({
    fetchImpl,
    env: {
      ...configuredEnv,
      SUPABASE_SERVICE_ROLE_KEY: 'eyJlegacy-service-role',
    },
  }), {
    password: 'correct horse',
    action: 'sessions',
  });

  assert.equal(fetchImpl.headers.apikey, 'eyJlegacy-service-role');
  assert.equal(fetchImpl.headers.Authorization, 'Bearer eyJlegacy-service-role');
});

test('validates the session UUID before requesting answers', async () => {
  const response = await invoke(handler(), {
    password: 'correct horse',
    action: 'answers',
    session_id: 'not-a-uuid',
  });

  assert.equal(response.statusCode, 400);
  assert.equal(response.body, '{"error":"Invalid action"}');
});

test('returns traced answers ordered by question index', async () => {
  const fetchImpl = recordingFetch([]);
  const response = await invoke(handler({ fetchImpl }), {
    password: 'correct horse',
    action: 'answers',
    session_id: '00000000-0000-4000-8000-000000000001',
  });

  assert.equal(response.statusCode, 200);
  assert.match(fetchImpl.url, /replay_quiz_answers_safe/);
  assert.match(fetchImpl.url, /viewer_trace=not\.is\.null/);
  assert.match(fetchImpl.url, /order=question_index\.asc/);
});

test('returns ordered weekly vote attempts and bounded suggestion records from safe views', async () => {
  const fetchImpl = recordingFetch([]);
  const weekly = await invoke(handler({ fetchImpl }), {
    password: 'correct horse',
    action: 'weekly-attempts',
    session_id: '00000000-0000-4000-8000-000000000001',
  });
  assert.equal(weekly.statusCode, 200);
  assert.equal(fetchImpl.calls.length, 2);
  assert.match(fetchImpl.calls[0].url, /replay_weekly_vote_attempts_safe/);
  assert.match(fetchImpl.calls[0].url, /vote_comment/);
  assert.match(fetchImpl.calls[0].url, /order=question_index\.asc,submitted_at\.asc/);
  assert.match(fetchImpl.calls[1].url, /replay_weekly_trace_batches_safe/);
  assert.deepEqual(JSON.parse(weekly.body), { attempts: [], batches: [] });

  const suggestions = await invoke(handler({ fetchImpl }), {
    password: 'correct horse',
    action: 'suggestions',
  });
  assert.equal(suggestions.statusCode, 200);
  assert.match(fetchImpl.url, /replay_user_suggestions_safe/);
  assert.match(fetchImpl.url, /limit=100/);
});

test('returns append-only weekly thinking batches in acknowledged order', async () => {
  const fetchImpl = recordingFetch([]);
  const response = await invoke(handler({ fetchImpl }), {
    password: 'correct horse',
    action: 'weekly-trace-batches',
    session_id: '00000000-0000-4000-8000-000000000001',
  });

  assert.equal(response.statusCode, 200);
  assert.match(fetchImpl.url, /replay_weekly_trace_batches_safe/);
  assert.match(fetchImpl.url, /order=submitted_at\.asc,trace_batch_id\.asc/);
  assert.match(fetchImpl.url, /limit=1000/);
  assert.doesNotMatch(fetchImpl.url, /user_id|display_name,/);
});

test('Preview replay service never falls back to production credentials', async () => {
  const fetchImpl = failIfCalled();
  const response = await invoke(handler({
    fetchImpl,
    env: { ...configuredEnv, FOLDARIUM_ENV: 'preview' },
  }), {
    password: 'correct horse',
    action: 'sessions',
  });

  assert.equal(response.statusCode, 500);
  assert.equal(response.body, '{"error":"Replay service is not configured"}');
});

test('rejects non-POST requests without calling Supabase', async () => {
  const response = await invoke(handler(), {
    password: 'correct horse',
    action: 'sessions',
  }, 'GET');

  assert.equal(response.statusCode, 405);
  assert.equal(response.headers['Cache-Control'], 'no-store');
});

test('rejects malformed or incomplete request bodies', async () => {
  for (const body of ['{', null, {}, { password: 42, action: 'sessions' }]) {
    const response = await invoke(handler(), body);
    assert.equal(response.statusCode, 400);
    assert.equal(response.body, '{"error":"Invalid request"}');
  }
});

test('rejects unsupported actions without calling Supabase', async () => {
  const response = await invoke(handler(), {
    password: 'correct horse',
    action: 'delete-all',
  });

  assert.equal(response.statusCode, 400);
  assert.equal(response.body, '{"error":"Invalid action"}');
});

test('returns a generic configuration error when credentials are absent', async () => {
  const response = await invoke(handler({
    env: { REPLAY_PASSWORD: 'correct horse' },
  }), {
    password: 'correct horse',
    action: 'sessions',
  });

  assert.equal(response.statusCode, 500);
  assert.equal(response.body, '{"error":"Replay service is not configured"}');
});

test('sanitizes failed upstream responses and exceptions', async () => {
  for (const fetchImpl of [
    recordingFetch({ message: 'service-key leaked' }, { ok: false }),
    () => { throw new Error('service-key leaked'); },
  ]) {
    const response = await invoke(handler({ fetchImpl }), {
      password: 'correct horse',
      action: 'sessions',
    });
    assert.equal(response.statusCode, 502);
    assert.equal(response.body, '{"error":"Replay data unavailable"}');
    assert.doesNotMatch(response.body, /service-key|correct horse/);
  }
});
