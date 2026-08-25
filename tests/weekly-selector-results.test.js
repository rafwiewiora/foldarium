import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SELECTOR_RESULTS_FORMAT_VERSION,
  WeeklySelectorResultsError,
  assertSanitizedSelectorResult,
  buildSelectorAnswerKeys,
  buildSminaSubmission,
  manifestSha256,
  normalizeLatestSubmissionRows,
  scoreSelectorSubmission,
  scoreWeeklySelectorResults,
  verifyRevealedSelectorRound,
} from '../lib/weekly-selector-results.js';
import {
  createWeeklySelectorResultsHandler,
  weeklySelectorResultsConfig,
} from '../api/weekly-selector-results.js';

const ROUND_ID = 'weekly-2026-08-25-preview';
const HASH = character => character.repeat(64);

function smina(value, scoringFunction = 'vina') {
  return {
    metric: 'smina_affinity',
    value,
    units: 'kcal/mol',
    protocol: 'score_only',
    scoring_function: scoringFunction,
  };
}

function fixture() {
  const blindManifest = {
    schema_version: 1,
    round_id: ROUND_ID,
    items: [{
      id: 'ITEM01',
      choices: [
        { id: 'choice-a1', cluster_id: 'cluster-a', smina_score: smina(-8) },
        { id: 'choice-a2', cluster_id: 'cluster-a', smina_score: smina(-7) },
        { id: 'choice-b1', cluster_id: 'cluster-b', smina_score: smina(-8) },
      ],
    }, {
      id: 'ITEM02',
      choices: [
        { id: 'choice-c1', cluster_id: 'cluster-c', smina_score: smina(-6) },
        { id: 'choice-c2', cluster_id: 'cluster-c', smina_score: smina(-5) },
      ],
    }],
  };
  const revealManifest = {
    schema_version: 1,
    round_id: ROUND_ID,
    blind_manifest_sha256: manifestSha256(blindManifest),
    items: [{
      id: 'ITEM01',
      choices: [
        { id: 'choice-a1', correct: false, accepted_correct: true },
        { id: 'choice-a2', correct: true, accepted_correct: true },
        { id: 'choice-b1', correct: false, accepted_correct: false },
      ],
    }, {
      id: 'ITEM02',
      choices: [
        { id: 'choice-c1', correct: false, accepted_correct: false },
        { id: 'choice-c2', correct: false, accepted_correct: false },
      ],
    }],
  };
  return { blindManifest, revealManifest };
}

function decisions({
  item1Cluster = 'cluster-b',
  item1Choice = 'choice-a2',
  item2Cluster = null,
  item2Choice = null,
} = {}) {
  const clustered = value => value == null
    ? { selection_kind: 'none' }
    : { selection_kind: 'cluster', cluster_id: value };
  const unclustered = value => value == null
    ? { selection_kind: 'none' }
    : { selection_kind: 'exact', choice_id: value };
  return [{
    item_id: 'ITEM01',
    clustered: clustered(item1Cluster),
    unclustered: unclustered(item1Choice),
  }, {
    item_id: 'ITEM02',
    clustered: clustered(item2Cluster),
    unclustered: unclustered(item2Choice),
  }];
}

function identity(overrides = {}) {
  return {
    display_name: 'Ada',
    method_name: 'rules',
    method_version: '2',
    provider: 'example',
    model_name: 'rules',
    model_version: '2',
    prompt_sha256: HASH('a'),
    tools_sha256: HASH('b'),
    config_sha256: HASH('c'),
    ...overrides,
  };
}

function invoke(handler, { method = 'GET', query = {} } = {}) {
  const headers = {};
  let statusCode;
  let body;
  const response = {
    setHeader(name, value) { headers[name] = value; return this; },
    status(value) { statusCode = value; return this; },
    json(value) { body = value; return this; },
  };
  return handler({ method, query }, response).then(() => ({
    statusCode,
    headers,
    body,
    serialized: JSON.stringify(body),
  }));
}

function previewEnv(overrides = {}) {
  return {
    VERCEL_ENV: 'preview',
    FOLDARIUM_PREVIEW_SUPABASE_URL: 'https://preview.supabase.co',
    FOLDARIUM_PREVIEW_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_preview',
    ...overrides,
  };
}

function revealedRound({ blindManifest, revealManifest, status = 'revealed' }) {
  return {
    round_id: ROUND_ID,
    environment: 'preview',
    status,
    item_count: blindManifest.items.length,
    blind_manifest: blindManifest,
    blind_manifest_sha256: manifestSha256(blindManifest),
    reveal_manifest: revealManifest,
    reveal_manifest_sha256: manifestSha256(revealManifest),
    revealed_at: status === 'revealed' ? '2026-08-25T12:00:00Z' : null,
  };
}

function recordingFetch({ round, catalog, submissions = [] }) {
  async function fetchImpl(url, options = {}) {
    fetchImpl.calls.push({
      url,
      method: options.method || 'GET',
      body: options.body ? JSON.parse(options.body) : null,
    });
    let payload;
    if (url.includes('/weekly_quiz_rounds')) payload = [round];
    else if (url.includes('/get_weekly_selector_kit_descriptor')) payload = [catalog];
    else if (url.includes('/get_weekly_selector_latest_submissions')) payload = submissions;
    else throw new Error(`unexpected fetch ${url}`);
    return { ok: true, json: async () => payload };
  }
  fetchImpl.calls = [];
  return fetchImpl;
}

test('v2 scoring keeps clustered, unclustered, and None decisions independent', () => {
  const { blindManifest, revealManifest } = fixture();
  const keys = buildSelectorAnswerKeys(blindManifest, revealManifest, 2);
  assert.deepEqual(scoreSelectorSubmission(decisions(), keys, 2), {
    clusteredCorrect: 1,
    unclusteredCorrect: 2,
    answered: 2,
  });

  const crossed = scoreSelectorSubmission(decisions({
    item1Cluster: 'cluster-a',
    item1Choice: 'choice-b1',
    item2Cluster: 'cluster-c',
    item2Choice: 'choice-c1',
  }), keys, 2);
  assert.deepEqual(crossed, {
    clusteredCorrect: 1,
    unclusteredCorrect: 0,
    answered: 2,
  });

  revealManifest.items[0].choices.find(choice => choice.id === 'choice-a2').correct = false;
  const independentNoneKeys = buildSelectorAnswerKeys(blindManifest, revealManifest, 2);
  const allNone = scoreSelectorSubmission(decisions({
    item1Cluster: null,
    item1Choice: null,
  }), independentNoneKeys, 2);
  assert.deepEqual(allNone, {
    clusteredCorrect: 1,
    unclusteredCorrect: 2,
    answered: 2,
  });
});

test('v2 scoring rejects implicit, malformed, and cross-item decisions', () => {
  const { blindManifest, revealManifest } = fixture();
  const keys = buildSelectorAnswerKeys(blindManifest, revealManifest, 2);
  const implicit = decisions();
  implicit[0] = { item_id: 'ITEM01', choice_id: 'choice-a2', cluster_id: 'cluster-a' };
  assert.throws(() => scoreSelectorSubmission(implicit, keys, 2), WeeklySelectorResultsError);

  const malformedNone = decisions();
  malformedNone[1].clustered.cluster_id = 'cluster-c';
  assert.throws(() => scoreSelectorSubmission(malformedNone, keys, 2), /unknown key/);

  const crossed = decisions({ item1Choice: 'choice-c1' });
  assert.throws(() => scoreSelectorSubmission(crossed, keys, 2), /unknown submission choice/);
});

test('Smina chooses the lowest finite affinity and breaks exact ties by immutable choice ID', () => {
  const { blindManifest, revealManifest } = fixture();
  const submission = buildSminaSubmission(buildSelectorAnswerKeys(blindManifest, revealManifest, 2));
  assert.deepEqual(submission.items[0], {
    item_id: 'ITEM01',
    clustered: { selection_kind: 'cluster', cluster_id: 'cluster-a' },
    unclustered: { selection_kind: 'exact', choice_id: 'choice-a1' },
  });
  assert.equal(submission.identity.display_name, 'Smina');
});

test('Smina fails closed for missing, non-finite, or inconsistent score provenance', () => {
  for (const mutate of [
    choice => { delete choice.smina_score; },
    choice => { choice.smina_score.value = Number.NaN; },
    choice => { choice.smina_score.scoring_function = 'other'; },
  ]) {
    const { blindManifest, revealManifest } = fixture();
    mutate(blindManifest.items[0].choices[1]);
    const keys = buildSelectorAnswerKeys(blindManifest, revealManifest, 2);
    assert.throws(() => buildSminaSubmission(keys), /Smina/);
  }
});

test('results expose dual-track overall ranks and labeled per-question counts without IDs', () => {
  const { blindManifest, revealManifest } = fixture();
  const result = scoreWeeklySelectorResults({
    roundId: ROUND_ID,
    itemCount: 2,
    blindManifest,
    revealManifest,
    submissions: [{ identity: identity(), items: decisions() }],
  });

  assert.equal(result.format_version, SELECTOR_RESULTS_FORMAT_VERSION);
  assert.equal(result.participant_count, 2);
  assert.equal(result.selector_count, 1);
  const ada = result.rows.find(row => row.identity.display_name === 'Ada');
  assert.equal(ada.clustered.correct, 1);
  assert.equal(ada.unclustered.correct, 2);
  assert.equal(ada.unclustered.rank, 1);

  const item = result.questions.find(question => question.item_id === 'ITEM01');
  assert.deepEqual(
    item.clustered.answers.find(answer => answer.label === 'Cluster A').display_names,
    ['Smina'],
  );
  assert.deepEqual(
    item.unclustered.answers.find(answer => answer.label === 'Pose A-1').display_names,
    ['Smina'],
  );
  assert.deepEqual(
    item.unclustered.answers.find(answer => answer.label === 'Pose A-2').display_names,
    ['Ada'],
  );
  const none = result.questions.find(question => question.item_id === 'ITEM02')
    .unclustered.answers.find(answer => answer.label === 'None');
  assert.deepEqual(none, {
    selection_kind: 'none',
    label: 'None',
    correct: true,
    count: 1,
    display_names: ['Ada'],
  });
  assert.doesNotMatch(JSON.stringify(result), /choice-a1|cluster-a|submission_id|user_id|token_hash/);
  assert.doesNotThrow(() => assertSanitizedSelectorResult(result));
});

test('normalization publishes only approved identity metadata', () => {
  const normalized = normalizeLatestSubmissionRows([{
    round_id: ROUND_ID,
    environment: 'preview',
    payload: { schema_version: 'foldarium.selector-submission/v2', items: decisions() },
    display_name: 'Ada',
    provider: 'example',
    model: 'rules',
    model_version: '2',
    prompt_sha256: HASH('a'),
    tools_sha256: HASH('b'),
    config_sha256: HASH('c'),
    user_id: '11111111-1111-4111-8111-111111111111',
    identity_id: '22222222-2222-4222-8222-222222222222',
    token_hash: HASH('d'),
    submission_id: '33333333-3333-4333-8333-333333333333',
  }], ROUND_ID);
  assert.deepEqual(normalized, [{ identity: identity(), items: decisions() }]);
  assert.doesNotMatch(JSON.stringify(normalized), /11111111|token_hash|submission_id/);
});

test('reveal verification checks status, bindings, and actual manifest digests', () => {
  const data = fixture();
  const live = revealedRound(data);
  assert.equal(verifyRevealedSelectorRound(live).roundId, ROUND_ID);
  assert.throws(
    () => verifyRevealedSelectorRound({ ...live, status: 'open' }),
    /not revealed/,
  );
  assert.throws(
    () => verifyRevealedSelectorRound({ ...live, reveal_manifest_sha256: HASH('f') }),
    /digest/,
  );
});

test('results endpoint is reveal-gated, environment-bound, sanitized, and passes RPC environment', async () => {
  const data = fixture();
  const round = revealedRound(data);
  const fetchImpl = recordingFetch({
    round,
    catalog: {
      round_id: ROUND_ID,
      kit_sha256: HASH('d'),
      blind_manifest_sha256: round.blind_manifest_sha256,
      item_count: 2,
    },
    submissions: [{
      round_id: ROUND_ID,
      environment: 'preview',
      payload: { items: decisions() },
      ...identity(),
      token_hash: HASH('e'),
    }],
  });
  const response = await invoke(createWeeklySelectorResultsHandler({
    env: previewEnv(),
    fetchImpl,
  }), { query: { round_id: ROUND_ID } });

  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body.rows.length, 2);
  assert.deepEqual(fetchImpl.calls[2].body, {
    p_round_id: ROUND_ID,
    p_environment: 'preview',
  });
  assert.doesNotMatch(response.serialized, /sb_publishable_preview|token_hash|submission_id/);

  const closedResponse = await invoke(createWeeklySelectorResultsHandler({
    env: previewEnv(),
    fetchImpl: recordingFetch({
      round: { ...round, status: 'open', revealed_at: null },
      catalog: null,
    }),
  }), { query: { round_id: ROUND_ID } });
  assert.equal(closedResponse.statusCode, 502);
});

test('results endpoint fails closed when Smina inputs are incomplete', async () => {
  const data = fixture();
  delete data.blindManifest.items[0].choices[0].smina_score;
  data.revealManifest.blind_manifest_sha256 = manifestSha256(data.blindManifest);
  const round = revealedRound(data);
  const response = await invoke(createWeeklySelectorResultsHandler({
    env: previewEnv(),
    fetchImpl: recordingFetch({
      round,
      catalog: {
        round_id: ROUND_ID,
        kit_sha256: HASH('d'),
        blind_manifest_sha256: round.blind_manifest_sha256,
        item_count: 2,
      },
    }),
  }), { query: { round_id: ROUND_ID } });
  assert.equal(response.statusCode, 502);
  assert.deepEqual(response.body, { error: 'Weekly selector results unavailable' });
});

test('results configuration remains fail-closed per deployment environment', () => {
  assert.equal(weeklySelectorResultsConfig(previewEnv()).environment, 'preview');
  assert.equal(weeklySelectorResultsConfig({
    VERCEL_ENV: 'preview',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://production.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_PUBLISHABLE_KEY: 'wrong',
  }).publishableKey, '');
});
