import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import {
  ContractError,
  buildClientBundle,
  hashCanonicalString,
  parseIntegrityDescriptor,
  verifyArtifactEnvelope,
  verifyIntegrityDescriptor,
  verifyLiveRoundState,
} from '../lib/private-evaluation-contract.js';
import { createPrivateEvaluationHandler, privateEvaluationConfig } from '../api/private-evaluation.js';
import { buildFixture } from './private-evaluation-fixtures.js';

function previewEnv(overrides = {}) {
  const fixture = buildFixture();
  return {
    FOLDARIUM_ENV: 'preview',
    SUPABASE_URL: 'https://preview-private.supabase.co',
    SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_preview_private',
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR: fixture.descriptorRaw,
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_SUPABASE_URL: 'https://legacy-preview.supabase.co',
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_legacy',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_production',
    ...overrides,
  };
}

function invoke(handler, body, { method = 'POST', env = previewEnv() } = {}) {
  const headers = {};
  let statusCode;
  let responseBody;
  const response = {
    setHeader(name, value) { headers[name] = value; return this; },
    status(value) { statusCode = value; return this; },
    json(value) { responseBody = value; return this; },
  };
  return handler({ method, body }, response).then(() => ({
    statusCode,
    headers,
    body: responseBody,
    serialized: JSON.stringify(responseBody),
  }));
}

function recordingFetch(fixture, leaderboardFixture = null) {
  const leaderboard = leaderboardFixture || {
    votes: [],
    voteAttempts: [],
    currentSessions: [],
    legacySessions: [],
  };
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({ url, headers: options.headers || {} });
    if (url.includes('/weekly_quiz_rounds')) {
      return { ok: true, json: async () => [fixture.liveRound] };
    }
    if (url.includes('/storage/v1/object/authenticated/')) {
      return { ok: true, arrayBuffer: async () => fixture.artifactBytes };
    }
    if (url.includes('/weekly_quiz_votes')) {
      return { ok: true, json: async () => leaderboard.votes };
    }
    if (url.includes('/weekly_quiz_vote_attempts')) {
      return { ok: true, json: async () => leaderboard.voteAttempts || [] };
    }
    if (url.includes('/weekly_quiz_sessions')) {
      const rows = url.includes('weekly-2026-08-08-beta-v4')
        ? leaderboard.legacySessions
        : leaderboard.currentSessions;
      return { ok: true, json: async () => rows };
    }
    throw new Error(`unexpected fetch: ${url}`);
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

test('contract verification accepts a bound v5 fixture', () => {
  const fixture = buildFixture();
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  const verified = verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live);
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
  assert.equal(bundle.round_id, fixture.descriptor.round_id);
  assert.equal(bundle.reveal_manifest.items.length, 1);
  assert.equal(bundle.blind_manifest.items.length, 1);
  assert.deepEqual(bundle.blind_manifest, fixture.blindManifest);
  assert.doesNotMatch(JSON.stringify(bundle), /artifact_object_uri|sb_secret|supabase:\/\//);
});

test('contract verification rejects tampered artifact bytes', () => {
  const fixture = buildFixture({ tamper: 'artifact-bytes' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    ContractError,
  );
});

test('contract verification accepts Python float lexical canonical strings', () => {
  const fixture = buildFixture({ pythonFloatLexical: true });
  assert.match(fixture.artifactObject.blind_manifest_canonical_json, /"schema_version":1\.0/);
  assert.notEqual(
    hashCanonicalString(JSON.stringify(fixture.blindManifest)),
    fixture.liveRound.blind_manifest_sha256,
  );

  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  const verified = verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live);
  assert.equal(verified.evaluationId, fixture.descriptor.evaluation_id);
});

test('contract verification rejects tampered canonical blind strings', () => {
  const fixture = buildFixture({ tamper: 'canonical-blind' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /blind manifest digest is inconsistent/,
  );
});

test('contract verification rejects tampered canonical reveal strings', () => {
  const fixture = buildFixture({ tamper: 'canonical-reveal' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /reveal manifest digest is inconsistent/,
  );
});

test('contract verification rejects reference digest tampering', () => {
  const fixture = buildFixture({ tamper: 'reference-digest' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /reference set digest is inconsistent/,
  );
});

test('contract verification rejects prediction digest tampering', () => {
  const fixture = buildFixture({ tamper: 'prediction-digest' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /prediction set digest is inconsistent/,
  );
});

test('contract verification rejects evaluator version mismatch', () => {
  const fixture = buildFixture({ tamper: 'evaluator-mismatch' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /evaluator_versions are not bound to artifact policy/,
  );
});

test('contract verification rejects blind and reveal identity mismatch', () => {
  const fixture = buildFixture({ tamper: 'identity-mismatch' });
  const live = verifyLiveRoundState(fixture.liveRound);
  const descriptor = verifyIntegrityDescriptor(fixture.descriptor, live);
  assert.throws(
    () => verifyArtifactEnvelope(fixture.artifactBytes, descriptor, live),
    /choice identities differ/,
  );
});

test('verifyLiveRoundState does not require live blind JSON', () => {
  const fixture = buildFixture();
  assert.equal(fixture.liveRound.blind_manifest, undefined);
  assert.doesNotThrow(() => verifyLiveRoundState(fixture.liveRound));
});

test('contract verification rejects a revealed live round', () => {
  const fixture = buildFixture({ tamper: 'live-revealed' });
  assert.throws(() => verifyLiveRoundState(fixture.liveRound), /already revealed/);
});

test('parseIntegrityDescriptor rejects malformed env JSON', () => {
  assert.throws(() => parseIntegrityDescriptor(''), /descriptor env is missing/);
  assert.throws(() => parseIntegrityDescriptor('{'), /descriptor env is not valid JSON/);
  assert.throws(() => parseIntegrityDescriptor('[]'), /descriptor env is invalid/);
});

test('preview config relies on deployment protection without a second password', () => {
  const preferred = privateEvaluationConfig(previewEnv());
  assert.equal(preferred.url, 'https://production.supabase.co');
  assert.equal(preferred.serviceRoleKey, 'sb_secret_production');
  assert.match(preferred.descriptorRaw, /"evaluation_id"/);

  const fallback = privateEvaluationConfig(previewEnv({
    FOLDARIUM_PRODUCTION_SUPABASE_URL: '',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: '',
  }));
  assert.equal(fallback.url, 'https://preview-private.supabase.co');
  assert.equal(fallback.serviceRoleKey, 'sb_secret_preview_private');

  const missing = privateEvaluationConfig(previewEnv({
    SUPABASE_URL: '',
    SUPABASE_SERVICE_ROLE_KEY: '',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: '',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: '',
    FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR: '',
  }));
  assert.deepEqual(missing, { url: '', serviceRoleKey: '', descriptorRaw: '' });
});

test('returns 404 outside preview deployments even when env vars are present', async () => {
  const fetchImpl = () => { throw new Error('must not fetch'); };
  const response = await invoke(
    createPrivateEvaluationHandler({
      env: previewEnv({ FOLDARIUM_ENV: 'production' }),
      fetchImpl,
    }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 404);
  assert.equal(response.body.error, 'Not found');
});

test('non-preview config returns blanks before reading server credentials', () => {
  const config = privateEvaluationConfig(previewEnv({ FOLDARIUM_ENV: 'production' }));
  assert.deepEqual(config, { url: '', serviceRoleKey: '', descriptorRaw: '' });
});

test('preview handler does not require a second in-app password', async () => {
  const fixture = buildFixture();
  const response = await invoke(
    createPrivateEvaluationHandler({
      env: previewEnv(),
      fetchImpl: recordingFetch(fixture),
    }),
    {},
  );
  assert.equal(response.statusCode, 200);
});

test('preview handler falls back to generic server-only supabase credentials', async () => {
  const fixture = buildFixture();
  const fetchImpl = recordingFetch(fixture);
  const response = await invoke(
    createPrivateEvaluationHandler({
      env: previewEnv({
        FOLDARIUM_PRODUCTION_SUPABASE_URL: '',
        FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: '',
      }),
      fetchImpl,
    }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 200);
  assert.match(fetchImpl.calls[0].url, /preview-private\.supabase\.co/);
  assert.doesNotMatch(fetchImpl.calls[0].url, /blind_manifest,/);
  assert.equal(response.body.round_id, fixture.descriptor.round_id);
});

test('returns 500 when preview private evaluation credentials are absent', async () => {
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv({
      FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR: '',
    }), fetchImpl: () => { throw new Error('must not fetch'); } }),
    {},
  );
  assert.equal(response.statusCode, 500);
  assert.equal(response.body.error, 'Private evaluation service is not configured');
});

test('returns artifact blind manifest even when live blind JSON is absent or wrong', async () => {
  const fixture = buildFixture({ tamper: 'live-blind-json' });
  const fetchImpl = recordingFetch(fixture);
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.body.blind_manifest, fixture.blindManifest);
  assert.notEqual(response.body.blind_manifest.items[0].id, 'WRONG');
});

test('returns a production-blind verified bundle without secrets or storage URIs', async () => {
  const fixture = buildFixture();
  const fetchImpl = recordingFetch(fixture);
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(fetchImpl.calls.length, 6);
  assert.doesNotMatch(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_evaluations/);
  assert.match(fetchImpl.calls[0].url, /weekly_quiz_rounds/);
  assert.match(fetchImpl.calls[0].url, /production\.supabase\.co/);
  assert.match(fetchImpl.calls[1].url, /storage\/v1\/object\/authenticated\/prediction-results\/sha256\//);
  assert.match(fetchImpl.calls.map(call => call.url).join('\n'), /weekly_quiz_vote_attempts/);
  assert.equal(response.body.evaluation_id, fixture.descriptor.evaluation_id);
  assert.equal(response.body.item_count, 1);
  assert.equal(response.body.round_id, fixture.descriptor.round_id);
  assert.equal(response.body.blind_manifest_sha256, fixture.liveRound.blind_manifest_sha256);
  assert.ok(response.body.weekly_leaderboard);
  assert.equal(response.body.weekly_leaderboard.participant_count, 1);
  assert.equal(response.body.weekly_leaderboard.complete_runs[0].display_name, 'Smina');
  assert.equal(response.body.weekly_question_results.items[0].answered_count, 1);
  assert.deepEqual(
    response.body.weekly_question_results.items[0].answers[0].display_names,
    ['Smina'],
  );
  assert.equal(
    response.body.weekly_question_results.items[0].answers[0].selection_kind,
    'exact',
  );
  assert.deepEqual(Object.keys(response.body).sort(), [
    'answer_overlays',
    'blind_manifest',
    'blind_manifest_sha256',
    'campaign_id',
    'choice_count',
    'closes_at',
    'evaluation_id',
    'format_version',
    'item_count',
    'opens_at',
    'reveal_manifest',
    'reveal_manifest_sha256',
    'round_id',
    'weekly_leaderboard',
    'weekly_question_results',
  ]);
  assert.doesNotMatch(response.serialized, /sb_secret|artifact_object_uri|supabase:\/\//);
});

test('private question results retain cluster-versus-pose vote provenance', async () => {
  const fixture = buildFixture();
  const item = fixture.revealManifest.items[0];
  const choice = item.choices[0];
  const userId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const vote = {
    round_id: fixture.descriptor.round_id,
    user_id: userId,
    item_id: item.id,
    choice_id: choice.id,
    picked_none: false,
  };
  const fetchImpl = recordingFetch(fixture, {
    votes: [vote],
    voteAttempts: [{
      ...vote,
      app_state: { selection_kind: 'cluster' },
      submitted_at: '2026-08-08T12:00:00Z',
    }],
    currentSessions: [{
      round_id: fixture.descriptor.round_id,
      user_id: userId,
      display_name: 'Ada',
      initial_app_state: { leaderboard_opt_in: true, leaderboard_name_version: 1 },
    }],
    legacySessions: [],
  });
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    {},
  );
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.weekly_question_results.items[0].answers[0].selection_kind, 'cluster');
});

test('legacy unclustered LLM ballots resolve as exact votes and include Smina', async () => {
  const fixture = buildFixture();
  const item = fixture.revealManifest.items[0];
  const representative = fixture.blindManifest.items[0].choices.find(choice => choice.is_rep);
  const userId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  const vote = {
    round_id: fixture.descriptor.round_id,
    user_id: userId,
    item_id: item.id,
    choice_id: representative.id,
    picked_none: false,
  };
  const fetchImpl = recordingFetch(fixture, {
    votes: [vote],
    voteAttempts: [],
    currentSessions: [],
    legacySessions: [{
      round_id: 'weekly-2026-08-08-beta-v4',
      user_id: userId,
      display_name: 'Claude Opus',
    }],
  });
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    {},
  );
  assert.equal(response.statusCode, 200);
  const answers = response.body.weekly_question_results.items[0].answers;
  assert.ok(answers.some(answer => (
    answer.selection_kind === 'exact'
    && answer.display_names.includes('Claude Opus')
  )));
  assert.ok(answers.some(answer => (
    answer.selection_kind === 'exact'
    && answer.display_names.includes('Smina')
  )));
  assert.doesNotMatch(JSON.stringify(answers), /scope unknown|\"selection_kind\":\"unknown\"/);
});

test('rejects descriptor tampering before returning a bundle', async () => {
  const fixture = buildFixture({ tamper: 'descriptor-blind' });
  const response = await invoke(
    createPrivateEvaluationHandler({
      env: previewEnv({ FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_DESCRIPTOR: fixture.descriptorRaw }),
      fetchImpl: recordingFetch(fixture),
    }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 502);
  assert.equal(response.body.error, 'Private evaluation verification failed');
});

test('rejects non-POST requests in preview', async () => {
  const response = await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl: () => { throw new Error('must not fetch'); } }),
    { password: 'preview-secret' },
    { method: 'GET' },
  );
  assert.equal(response.statusCode, 405);
  assert.equal(response.headers.Allow, 'POST');
});

test('ignores request bodies because deployment protection is the only gate', async () => {
  for (const body of [null, {}, { password: 42 }]) {
    const fixture = buildFixture();
    const response = await invoke(
      createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl: recordingFetch(fixture) }),
      body,
    );
    assert.equal(response.statusCode, 200);
  }
});

test('sanitizes upstream failures', async () => {
  const response = await invoke(
    createPrivateEvaluationHandler({
      env: previewEnv(),
      fetchImpl: async () => { throw new Error('sb_secret_preview_private leaked'); },
    }),
    { password: 'preview-secret' },
  );
  assert.equal(response.statusCode, 502);
  assert.equal(response.body.error, 'Private evaluation unavailable');
  assert.doesNotMatch(response.serialized, /sb_secret|preview-secret/);
});

test('does not write to preview database during verification', async () => {
  const fixture = buildFixture();
  const fetchImpl = recordingFetch(fixture);
  await invoke(
    createPrivateEvaluationHandler({ env: previewEnv(), fetchImpl }),
    { password: 'preview-secret' },
  );
  for (const call of fetchImpl.calls) {
    assert.doesNotMatch(call.url, /method=POST|method=PATCH|method=PUT|method=DELETE/i);
    assert.doesNotMatch(call.url, /weekly_quiz_evaluations/);
  }
});

test('python-generated v5 golden artifact verifies in JS', async () => {
  const golden = JSON.parse(await readFile(new URL('./fixtures/private-evaluation-v5.golden.json', import.meta.url), 'utf8'));
  const live = verifyLiveRoundState(golden.liveRound);
  const descriptor = verifyIntegrityDescriptor(golden.descriptor, live);
  const artifactBytes = Buffer.from(golden.artifact_base64, 'base64');
  assert.equal(createHash('sha256').update(artifactBytes).digest('hex'), descriptor.artifactSha256);
  const verified = verifyArtifactEnvelope(artifactBytes, descriptor, live);
  assert.equal(verified.blindManifest.items.length, golden.descriptor.item_count);
});
