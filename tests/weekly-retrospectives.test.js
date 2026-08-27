import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';

import {
  APPROVED_LLM_IDENTITIES,
  ARCHIVE_ADMIN_DETAIL_FORMAT_VERSION,
  ARCHIVE_ALL_TIME_FORMAT_VERSION,
  ARCHIVE_DETAIL_FORMAT_VERSION,
  ARCHIVE_LIST_FORMAT_VERSION,
  LIGAND_PLDDT_BASELINE_IDENTITY,
  canonicalJson,
  decodeArchiveCursor,
  projectPublicAssetUri,
} from '../lib/weekly-retrospectives.js';
import {
  ARTIFACT_LOAD_CONCURRENCY,
  createWeeklyRetrospectivesHandler,
  mapWithConcurrency,
} from '../api/weekly-retrospectives.js';
import {
  createDeferredBackend,
  createQuizBackend,
  initQuizBackend,
} from '../quiz-backend.js';

const HUMAN_ID = '11111111-1111-4111-8111-111111111111';
const LLM_ID = '22222222-2222-4222-8222-222222222222';
const HMAC_KEY = 'retrospective-participant-test-key-32-bytes';
const ITEM_ID = '9XYZ';
const REFERENCE_URI = `https://files.rcsb.org/download/${ITEM_ID}.cif.gz`;

test('public archive accepts the audited Sol benchmark identity', () => {
  assert.ok(APPROVED_LLM_IDENTITIES.includes('GPT-5.6 Sol'));
});

function digest(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

function stableId(prefix, value, length = 32) {
  return `${prefix}_${digest(Buffer.from(canonicalJson(value))).slice(0, length)}`;
}

function stored(bytes, bucket = 'private') {
  const sha256 = digest(bytes);
  return {
    object_uri: `supabase://${bucket}/sha256/${sha256.slice(0, 2)}/${sha256}`,
    sha256,
    size_bytes: bytes.length,
    media_type: 'application/json',
  };
}

function score(participant, participantKind, correct, {
  answered = 1,
  total = 1,
} = {}) {
  return {
    participant,
    participant_kind: participantKind,
    correct,
    answered,
    total,
    accuracy: answered ? (correct / answered) * 100 : 0,
    coverage: (answered / total) * 100,
    complete: answered === total,
  };
}

function responseRow(participant, participantKind, choiceId, correct) {
  return {
    participant,
    participant_kind: participantKind,
    choice_id: choiceId,
    picked_none: false,
    selection_kind: 'exact',
    correct,
  };
}

function buildWeek({
  index = 0,
  roundId = `weekly-archive-arbitrary-${index}`,
  humanName = 'PocketFox',
  humanChoice = 'choice-a',
  suppressHumans = false,
  overlayTamper = null,
  referenceUri = REFERENCE_URI,
  revealedAt = `2026-08-${String(20 + index).padStart(2, '0')}T00:00:00Z`,
} = {}) {
  const opensAt = `2026-08-${String(10 + index).padStart(2, '0')}T00:00:00Z`;
  const closesAt = `2026-08-${String(17 + index).padStart(2, '0')}T00:00:00Z`;
  const campaignId = `campaign-${index}`;
  const blindManifest = {
    schema_version: 1,
    round_id: roundId,
    items: [{
      id: ITEM_ID,
      ligand: { component_id: 'DRG', heavy_atoms: 17 },
      week: '2026-08-08',
      protein_uri: 'supabase://structures/weekly/protein.pdb',
      pocket_uri: 'supabase://structures/weekly/pocket.pdb',
      metadata: {
        presentation: {
          policy: 'weekly-presentation/v1',
          group: 'multi-cluster',
          cluster_count: 2,
        },
      },
      expected_none: false,
      choices: [
        {
          id: 'choice-a',
          method: 'model-a',
          method_version: '1.0',
          pose_uri: 'supabase://structures/weekly/pose-a.pdb',
          cluster_id: 'cluster-a',
          is_rep: true,
          confidence: {
            metric: 'ligand_plddt',
            value: 80,
            scale_min: 0,
            scale_max: 100,
            aggregation: 'arithmetic-mean-selected-ligand-heavy-atoms',
          },
          smina_score: {
            metric: 'smina_affinity',
            protocol: 'score_only',
            scoring_function: 'vina',
            units: 'kcal/mol',
            value: -7,
          },
        },
        {
          id: 'choice-b',
          method: 'model-b',
          method_version: '1.0',
          pose_uri: 'supabase://structures/weekly/pose-b.pdb',
          cluster_id: 'cluster-b',
          is_rep: true,
          confidence: {
            metric: 'ligand_plddt',
            value: 90,
            scale_min: 0,
            scale_max: 100,
            aggregation: 'arithmetic-mean-selected-ligand-heavy-atoms',
          },
          smina_score: {
            metric: 'smina_affinity',
            protocol: 'score_only',
            scoring_function: 'vina',
            units: 'kcal/mol',
            value: -6,
          },
        },
      ],
    }],
  };
  const blindCanonical = canonicalJson(blindManifest);
  const blindSha = digest(Buffer.from(blindCanonical));
  const revealManifest = {
    schema_version: 1,
    round_id: roundId,
    blind_manifest_sha256: blindSha,
    items: [{
      id: ITEM_ID,
      choices: [
        {
          id: 'choice-a',
          correct: true,
          accepted_correct: true,
          rmsd: 0.8,
          reference_uri: referenceUri,
          prediction_sha256: 'ab'.repeat(32),
        },
        {
          id: 'choice-b',
          correct: false,
          accepted_correct: false,
          rmsd: 2.2,
          reference_uri: referenceUri,
          prediction_sha256: 'cd'.repeat(32),
        },
      ],
    }],
  };
  const revealCanonical = canonicalJson(revealManifest);
  const revealSha = digest(Buffer.from(revealCanonical));
  const privateIndexSha = digest(Buffer.from(`private-index-${index}`));
  const referenceSha = digest(Buffer.from(`references-${index}`));
  const predictionSha = digest(Buffer.from(`predictions-${index}`));
  const crystalPdb = 'HETATM    1  C1  DRG Q   1       0.000   0.000   0.000\nEND\n';
  const poseAPdb = 'HETATM    1  C1  DRG Z   1       0.100   0.000   0.000\nEND\n';
  const poseBPdb = 'HETATM    1  C1  DRG Z   1       2.200   0.000   0.000\nEND\n';
  const pocketPdb = 'ATOM      1  CA  ALA P   1       1.000   1.000   1.000\nEND\n';
  const answerOverlays = [{
    item_id: ITEM_ID,
    crystal_source_id: 'choice-a',
    crystal_source_rmsd: 0.8,
    crystal_ligand_pdb: crystalPdb,
    crystal_ligand_sha256: digest(Buffer.from(crystalPdb)),
    poses: [{
      id: 'choice-a',
      rmsd: 0.8,
      correct: true,
      predicted_pose_pdb: poseAPdb,
      predicted_pose_sha256: digest(Buffer.from(poseAPdb)),
      crystal_ligand_pdb: crystalPdb,
      crystal_ligand_sha256: digest(Buffer.from(crystalPdb)),
      crystal_pocket_pdb: pocketPdb,
      crystal_pocket_sha256: digest(Buffer.from(pocketPdb)),
    }, {
      id: 'choice-b',
      rmsd: 2.2,
      correct: false,
      predicted_pose_pdb: poseBPdb,
      predicted_pose_sha256: digest(Buffer.from(poseBPdb)),
      crystal_ligand_pdb: crystalPdb,
      crystal_ligand_sha256: digest(Buffer.from(crystalPdb)),
      crystal_pocket_pdb: pocketPdb,
      crystal_pocket_sha256: digest(Buffer.from(pocketPdb)),
    }],
  }];
  if (overlayTamper === 'missing') answerOverlays.length = 0;
  if (overlayTamper === 'pdb-digest') {
    answerOverlays[0].poses[0].predicted_pose_pdb = poseAPdb.replace('0.100', '0.101');
  }
  if (overlayTamper === 'pdb-end') {
    const invalid = poseAPdb.replace('\nEND\n', '\n');
    answerOverlays[0].poses[0].predicted_pose_pdb = invalid;
    answerOverlays[0].poses[0].predicted_pose_sha256 = digest(Buffer.from(invalid));
  }
  if (overlayTamper === 'pdb-size') {
    const oversized = `${'H'.repeat(1_000_001)}\nEND\n`;
    answerOverlays[0].poses[0].predicted_pose_pdb = oversized;
    answerOverlays[0].poses[0].predicted_pose_sha256 = digest(Buffer.from(oversized));
  }
  if (overlayTamper === 'choice-coverage') answerOverlays[0].poses.pop();
  if (overlayTamper === 'score-binding') answerOverlays[0].poses[0].rmsd = 0.9;
  if (overlayTamper === 'correct-binding') answerOverlays[0].poses[0].correct = false;
  if (overlayTamper === 'crystal-source') {
    const otherCrystal = crystalPdb.replace('0.000', '0.500');
    answerOverlays[0].crystal_ligand_pdb = otherCrystal;
    answerOverlays[0].crystal_ligand_sha256 = digest(Buffer.from(otherCrystal));
  }
  const evaluationArtifact = {
    format_version: 'foldarium.weekly-private-evaluation/v5',
    blind_manifest_canonical_json: blindCanonical,
    blind_manifest: blindManifest,
    reveal_manifest_canonical_json: revealCanonical,
    reveal_manifest: revealManifest,
    counts: { item_count: 1, choice_count: 2 },
    integrity: {
      reveal_manifest_sha256: revealSha,
      reference_set_sha256: referenceSha,
      prediction_set_sha256: predictionSha,
      answer_overlay_set_sha256: overlayTamper === 'set-digest'
        ? 'ff'.repeat(32)
        : digest(Buffer.from(canonicalJson(answerOverlays))),
    },
    answer_overlays: answerOverlays,
  };
  const evaluationBytes = Buffer.from(JSON.stringify(evaluationArtifact));
  const evaluationStored = stored(evaluationBytes);
  const evaluationId = stableId('weekly_eval', {
    artifact_sha256: evaluationStored.sha256,
    blind_manifest_sha256: blindSha,
    format_version: 'foldarium.weekly-private-evaluation/v5',
    private_index_sha256: privateIndexSha,
    round_id: roundId,
  });
  const roundBlock = {
    round_id: roundId,
    campaign_id: campaignId,
    opens_at: opensAt,
    closes_at: closesAt,
    revealed_at: revealedAt,
    item_count: 1,
    choice_count: 2,
  };
  const humanCorrect = humanChoice === 'choice-a' ? 1 : 0;
  const publicArtifact = {
    format_version: 'foldarium.weekly-retrospective-public/v1',
    round: roundBlock,
    human_aggregate: {
      participant_count: 1,
      suppressed: suppressHumans,
      complete_count: suppressHumans ? null : 1,
      partial_count: suppressHumans ? null : 0,
      score_distribution: suppressHumans ? [] : [{
        correct: humanCorrect,
        answered: 1,
        participant_count: 1,
      }],
    },
    automated_entries: [
      score('Claude Opus', 'llm', 1),
      score('Smina', 'baseline', 0),
    ],
    questions: [{
      item_id: ITEM_ID,
      human_aggregate: {
        answered_count: 1,
        suppressed: suppressHumans,
        correct_count: suppressHumans ? null : humanCorrect,
        answers: suppressHumans ? [] : [{
          choice_id: humanChoice,
          picked_none: false,
          selection_kind: 'exact',
          correct: Boolean(humanCorrect),
          vote_count: 1,
        }],
      },
      automated_entries: [
        responseRow('Claude Opus', 'llm', 'choice-a', true),
        responseRow('Smina', 'baseline', 'choice-b', false),
      ],
    }],
  };
  const adminArtifact = {
    format_version: 'foldarium.weekly-retrospective-admin/v1',
    round: roundBlock,
    participants: [
      score('Claude Opus', 'llm', 1),
      score('Smina', 'baseline', 0),
      score(humanName, 'human', humanCorrect),
    ],
    questions: [{
      item_id: ITEM_ID,
      responses: [
        responseRow('Claude Opus', 'llm', 'choice-a', true),
        responseRow('Smina', 'baseline', 'choice-b', false),
        responseRow(humanName, 'human', humanChoice, Boolean(humanCorrect)),
      ],
    }],
  };
  const sourceSnapshot = {
    format_version: 'foldarium.weekly-retrospective-source/v1',
    round_id: roundId,
    participants: [
      {
        participant_link: HUMAN_ID,
        participant_kind: 'human',
        automated_identity: null,
        display_name: humanName,
        current_session_count: 1,
      },
      {
        participant_link: LLM_ID,
        participant_kind: 'automated',
        automated_identity: 'Claude Opus',
        display_name: null,
        current_session_count: 1,
      },
    ],
    votes: [
      {
        participant_link: HUMAN_ID,
        item_id: ITEM_ID,
        choice_id: humanChoice,
        picked_none: false,
        selection_kind: 'exact',
      },
      {
        participant_link: LLM_ID,
        item_id: ITEM_ID,
        choice_id: 'choice-a',
        picked_none: false,
        selection_kind: 'cluster',
      },
    ],
  };
  const publicBytes = Buffer.from(canonicalJson(publicArtifact));
  const adminBytes = Buffer.from(canonicalJson(adminArtifact));
  const sourceBytes = Buffer.from(canonicalJson(sourceSnapshot));
  const publicStored = stored(publicBytes);
  const adminStored = stored(adminBytes);
  const sourceStored = stored(sourceBytes);
  const publicationIdentity = {
    format_version: 'foldarium.weekly-retrospective-publication/v1',
    round_id: roundId,
    evaluation_id: evaluationId,
    evaluation_artifact_sha256: evaluationStored.sha256,
    source_snapshot_sha256: sourceStored.sha256,
    public_artifact_sha256: publicStored.sha256,
    admin_artifact_sha256: adminStored.sha256,
  };
  const publication = {
    publication_id: stableId('weekly_archive', publicationIdentity),
    round_id: roundId,
    campaign_id: campaignId,
    environment: 'production',
    format_version: 'foldarium.weekly-retrospective-publication/v1',
    evaluation_id: evaluationId,
    evaluation_format_version: 'foldarium.weekly-private-evaluation/v5',
    round_opens_at: opensAt,
    round_closes_at: closesAt,
    round_revealed_at: revealedAt,
    blind_manifest_sha256: blindSha,
    private_index_sha256: privateIndexSha,
    reveal_manifest_sha256: revealSha,
    reference_set_sha256: referenceSha,
    prediction_set_sha256: predictionSha,
    evaluation_artifact_sha256: evaluationStored.sha256,
    item_count: 1,
    choice_count: 2,
    source_snapshot_object_uri: sourceStored.object_uri,
    source_snapshot_sha256: sourceStored.sha256,
    source_snapshot_size_bytes: sourceStored.size_bytes,
    source_snapshot_media_type: sourceStored.media_type,
    public_artifact_object_uri: publicStored.object_uri,
    public_artifact_sha256: publicStored.sha256,
    public_artifact_size_bytes: publicStored.size_bytes,
    public_artifact_media_type: publicStored.media_type,
    admin_artifact_object_uri: adminStored.object_uri,
    admin_artifact_sha256: adminStored.sha256,
    admin_artifact_size_bytes: adminStored.size_bytes,
    admin_artifact_media_type: adminStored.media_type,
    created_at: revealedAt,
  };
  const evaluation = {
    evaluation_id: evaluationId,
    round_id: roundId,
    campaign_id: campaignId,
    environment: 'production',
    round_opens_at: opensAt,
    round_closes_at: closesAt,
    blind_manifest_sha256: blindSha,
    private_index_sha256: privateIndexSha,
    reveal_manifest_sha256: revealSha,
    reference_set_sha256: referenceSha,
    prediction_set_sha256: predictionSha,
    format_version: 'foldarium.weekly-private-evaluation/v5',
    item_count: 1,
    choice_count: 2,
    artifact_object_uri: evaluationStored.object_uri,
    artifact_sha256: evaluationStored.sha256,
    artifact_size_bytes: evaluationStored.size_bytes,
    artifact_media_type: evaluationStored.media_type,
  };
  const round = {
    round_id: roundId,
    campaign_id: campaignId,
    environment: 'production',
    status: 'revealed',
    opens_at: opensAt,
    closes_at: closesAt,
    blind_manifest: blindManifest,
    blind_manifest_sha256: blindSha,
    reveal_manifest: revealManifest,
    reveal_manifest_sha256: revealSha,
    item_count: 1,
    revealed_at: revealedAt,
  };
  return {
    publication,
    evaluation,
    round,
    objects: new Map([
      [evaluationStored.sha256, evaluationBytes],
      [publicStored.sha256, publicBytes],
      [adminStored.sha256, adminBytes],
      [sourceStored.sha256, sourceBytes],
    ]),
  };
}

function archiveFetch(weeks) {
  const calls = [];
  const allObjects = new Map(weeks.flatMap(week => [...week.objects]));
  const fetchImpl = async (rawUrl, options = {}) => {
    const url = new URL(rawUrl);
    calls.push({ url, options });
    if (url.pathname.startsWith('/storage/v1/object/authenticated/')) {
      const objectDigest = url.pathname.split('/').at(-1);
      const bytes = allObjects.get(objectDigest);
      return bytes ? new Response(bytes) : new Response('missing', { status: 404 });
    }
    if (!url.pathname.startsWith('/rest/v1/')) {
      return new Response('missing', { status: 404 });
    }
    const table = url.pathname.split('/').at(-1);
    let rows;
    if (table === 'weekly_retrospective_publications') {
      rows = weeks.map(week => week.publication);
      const roundFilter = url.searchParams.get('round_id');
      if (roundFilter) rows = rows.filter(row => row.round_id === roundFilter.slice(3));
      rows = rows.sort((left, right) => (
        right.round_revealed_at.localeCompare(left.round_revealed_at)
        || right.round_id.localeCompare(left.round_id)
      ));
      const keyset = url.searchParams.get('or');
      if (keyset) {
        const match = /round_revealed_at\.lt\.([^,]+),and\(round_revealed_at\.eq\.([^,]+),round_id\.lt\.([^)]+)\)/.exec(
          keyset.slice(1, -1),
        );
        if (!match) return new Response('bad cursor query', { status: 400 });
        rows = rows.filter(row => (
          row.round_revealed_at < match[1]
          || (row.round_revealed_at === match[2] && row.round_id < match[3])
        ));
      }
      const range = options.headers?.Range;
      if (range) {
        const [start, end] = range.split('-').map(Number);
        rows = rows.slice(start, end + 1);
      } else {
        rows = rows.slice(0, Number(url.searchParams.get('limit') || rows.length));
      }
    } else if (table === 'weekly_quiz_evaluations') {
      const identity = url.searchParams.get('evaluation_id')?.slice(3);
      rows = weeks.map(week => week.evaluation)
        .filter(row => !identity || row.evaluation_id === identity);
    } else if (table === 'weekly_quiz_rounds') {
      const identity = url.searchParams.get('round_id')?.slice(3);
      rows = weeks.map(week => week.round)
        .filter(row => !identity || row.round_id === identity);
    } else {
      rows = [];
    }
    return Response.json(rows);
  };
  fetchImpl.calls = calls;
  fetchImpl.objects = allObjects;
  return fetchImpl;
}

function env(overrides = {}) {
  return {
    FOLDARIUM_ENV: 'production',
    FOLDARIUM_PRODUCTION_SUPABASE_URL: 'https://example.supabase.co',
    FOLDARIUM_PRODUCTION_SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_server_only',
    ...overrides,
  };
}

async function invoke(handler, {
  query = {},
  headers = {},
  method = 'GET',
} = {}) {
  const result = {
    statusCode: null,
    headers: {},
    body: null,
    ended: false,
  };
  const response = {
    setHeader(name, value) {
      result.headers[name] = value;
    },
    status(statusCode) {
      result.statusCode = statusCode;
      return this;
    },
    json(body) {
      result.body = body;
      result.serialized = JSON.stringify(body);
      return this;
    },
    end() {
      result.ended = true;
      return this;
    },
  };
  await handler({ method, query, headers }, response);
  return result;
}

test('list uses newest-first opaque keyset cursors and validates limits', async () => {
  const weeks = [
    buildWeek({ index: 0, revealedAt: '2026-08-20T00:00:00Z' }),
    buildWeek({ index: 1, revealedAt: '2026-08-21T00:00:00Z' }),
    buildWeek({ index: 2, revealedAt: '2026-08-22T00:00:00Z' }),
  ];
  const fetchImpl = archiveFetch(weeks);
  const handler = createWeeklyRetrospectivesHandler({ env: env(), fetchImpl });
  const first = await invoke(handler, { query: { limit: '2' } });
  assert.equal(first.statusCode, 200);
  assert.equal(first.body.format_version, ARCHIVE_LIST_FORMAT_VERSION);
  assert.deepEqual(
    first.body.publications.map(row => row.round_id),
    ['weekly-archive-arbitrary-2', 'weekly-archive-arbitrary-1'],
  );
  assert.deepEqual(first.body.publications[0].summary.outcomes, {
    pose_solved: 1,
    pose_unsolved: 0,
    none_solved: 0,
    none_unsolved: 0,
    suppressed: 0,
  });
  assert.equal(first.body.publications[0].summary.human_participant_count, 1);
  assert.equal(first.body.publications[0].summary.human_entries[0].participant, 'PocketFox');
  assert.equal(first.body.publications[0].summary.automated_entries[0].participant, 'Claude Opus');
  assert.deepEqual(
    first.body.publications[0].summary.automated_entries.find(
      row => row.participant === LIGAND_PLDDT_BASELINE_IDENTITY,
    ),
    score(LIGAND_PLDDT_BASELINE_IDENTITY, 'baseline', 0),
  );
  assert.equal(first.body.publications[0].summary.automated_winner.participant, 'Claude Opus');
  assert.ok(first.body.next_cursor);
  assert.doesNotMatch(first.body.next_cursor, /weekly|archive/);
  assert.deepEqual(decodeArchiveCursor(first.body.next_cursor), {
    revealedAt: '2026-08-21T00:00:00Z',
    roundId: 'weekly-archive-arbitrary-1',
  });
  assert.doesNotMatch(first.serialized, /publication_id|evaluation_id|sha256|object_uri/);
  assert.match(first.headers['Cache-Control'], /s-maxage=300/);

  const second = await invoke(handler, {
    query: { limit: '2', cursor: first.body.next_cursor },
  });
  assert.equal(second.statusCode, 200);
  assert.deepEqual(
    second.body.publications.map(row => row.round_id),
    ['weekly-archive-arbitrary-0'],
  );
  assert.equal(second.body.next_cursor, null);
  assert.match(
    fetchImpl.calls.map(call => call.url.searchParams.get('or')).filter(Boolean)[0],
    /round_revealed_at\.lt/,
  );

  for (const query of [{ limit: '0' }, { limit: '51' }, { cursor: 'not+base64' }]) {
    const invalid = await invoke(handler, { query });
    assert.equal(invalid.statusCode, 400);
    assert.equal(invalid.headers['Cache-Control'], 'no-store');
  }
});

test('public API reveals chosen pseudonyms even for a one-player artifact', async () => {
  const week = buildWeek({ suppressHumans: true });
  const handler = createWeeklyRetrospectivesHandler({
    env: env(),
    fetchImpl: archiveFetch([week]),
  });
  const list = await invoke(handler);
  assert.equal(list.statusCode, 200);
  assert.deepEqual(list.body.publications[0].summary.outcomes, {
    pose_solved: 1,
    pose_unsolved: 0,
    none_solved: 0,
    none_unsolved: 0,
    suppressed: 0,
  });
  assert.equal(list.body.publications[0].summary.human_complete_count, 1);
  assert.equal(list.body.publications[0].summary.human_partial_count, 0);

  const detail = await invoke(handler, {
    query: { round_id: week.publication.round_id },
  });
  assert.equal(detail.statusCode, 200);
  const aggregate = detail.body.retrospective.questions[0].human_aggregate;
  assert.deepEqual(aggregate, {
    answered_count: 1,
    correct_count: 1,
    suppressed: false,
    answers: [{
      choice_id: 'choice-a',
      picked_none: false,
      selection_kind: 'exact',
      correct: true,
      vote_count: 1,
      display_names: ['PocketFox'],
    }],
  });
  assert.deepEqual(
    detail.body.retrospective.questions[0].automated_entries.find(
      row => row.participant === LIGAND_PLDDT_BASELINE_IDENTITY,
    ),
    responseRow(LIGAND_PLDDT_BASELINE_IDENTITY, 'baseline', 'choice-b', false),
  );
  assert.match(detail.serialized, /PocketFox/);
});

test('artifact loading preserves order and never exceeds five publication workers', async () => {
  let active = 0;
  let peak = 0;
  const values = Array.from({ length: 23 }, (_, index) => index);
  const result = await mapWithConcurrency(
    values,
    ARTIFACT_LOAD_CONCURRENCY,
    async value => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 2));
      active -= 1;
      return value * 2;
    },
  );
  assert.equal(ARTIFACT_LOAD_CONCURRENCY, 5);
  assert.equal(peak, 5);
  assert.deepEqual(result, values.map(value => value * 2));
});

test('exact detail accepts arbitrary round IDs, strips private manifest fields, and supports ETags', async () => {
  const week = buildWeek({ roundId: 'round-not-the-current-campaign' });
  const fetchImpl = archiveFetch([week]);
  const handler = createWeeklyRetrospectivesHandler({ env: env(), fetchImpl });
  const detail = await invoke(handler, {
    query: { round_id: week.publication.round_id },
  });
  assert.equal(detail.statusCode, 200);
  assert.equal(detail.body.format_version, ARCHIVE_DETAIL_FORMAT_VERSION);
  assert.equal(detail.body.round.round_id, week.publication.round_id);
  assert.equal(detail.body.retrospective.human_aggregate.participant_count, 1);
  assert.equal(detail.body.reveal_manifest.items[0].choices[0].rmsd, 0.8);
  assert.equal(detail.body.reveal_manifest.items[0].choices[0].reference_uri, REFERENCE_URI);
  assert.equal(detail.body.answer_overlays[0].item_id, ITEM_ID);
  assert.match(detail.body.answer_overlays[0].crystal_ligand_pdb, /\nEND\n$/);
  assert.match(detail.body.answer_overlays[0].poses[0].predicted_pose_pdb, /\nEND\n$/);
  assert.match(detail.body.answer_overlays[0].poses[0].crystal_pocket_pdb, /\nEND\n$/);
  assert.equal(
    detail.body.blind_manifest.items[0].choices[0].pose_uri,
    'https://example.supabase.co/storage/v1/object/public/structures/weekly/pose-a.pdb',
  );
  assert.equal(detail.body.blind_manifest.items[0].ligand.component_id, 'DRG');
  assert.match(detail.serialized, /PocketFox/);
  assert.doesNotMatch(
    detail.serialized,
    /participant_link|prediction_sha256|s3:|supabase:|"_sha256"|[a-f0-9]{64}/,
  );
  assert.match(detail.headers['Cache-Control'], /s-maxage=86400/);
  assert.match(
    detail.headers.ETag,
    /^"weekly-retrospective-names-v1-[a-f0-9]{64}"$/,
  );

  const cached = await invoke(handler, {
    query: { round_id: week.publication.round_id },
    headers: { 'if-none-match': detail.headers.ETag },
  });
  assert.equal(cached.statusCode, 304);
  assert.equal(cached.body, null);
  assert.equal(cached.ended, true);
  assert.equal(cached.headers.ETag, detail.headers.ETag);
});

test('detail fails closed for absent or tampered overlays and noncanonical crystal references', async () => {
  const variants = [
    buildWeek({ overlayTamper: 'missing' }),
    buildWeek({ overlayTamper: 'pdb-digest' }),
    buildWeek({ overlayTamper: 'pdb-end' }),
    buildWeek({ overlayTamper: 'pdb-size' }),
    buildWeek({ overlayTamper: 'choice-coverage' }),
    buildWeek({ overlayTamper: 'score-binding' }),
    buildWeek({ overlayTamper: 'correct-binding' }),
    buildWeek({ overlayTamper: 'crystal-source' }),
    buildWeek({ overlayTamper: 'set-digest' }),
    buildWeek({ referenceUri: 'https://files.rcsb.org/download/8ABC.cif.gz' }),
    buildWeek({ referenceUri: `${REFERENCE_URI}?download=1` }),
  ];
  for (const week of variants) {
    const response = await invoke(createWeeklyRetrospectivesHandler({
      env: env(),
      fetchImpl: archiveFetch([week]),
    }), {
      query: { round_id: week.publication.round_id },
    });
    assert.equal(response.statusCode, 404);
    assert.deepEqual(response.body, { error: 'Not found' });
    assert.equal(response.headers['Cache-Control'], 'no-store');
  }
});

test('admin exact is preview-only, no-store, and returns normalized raw pseudonyms', async () => {
  const week = buildWeek({ humanName: '<img src=x onerror="steal()">' });
  const fetchImpl = archiveFetch([week]);
  const disabled = await invoke(createWeeklyRetrospectivesHandler({
    env: env({ FOLDARIUM_ENV: 'production' }),
    fetchImpl,
  }), {
    query: { admin: '1', round_id: week.publication.round_id },
  });
  assert.equal(disabled.statusCode, 404);
  assert.equal(fetchImpl.calls.length, 0);
  const disabledInvalidMode = await invoke(createWeeklyRetrospectivesHandler({
    env: env({ FOLDARIUM_ENV: 'production' }),
    fetchImpl,
  }), { query: { admin: '1' } });
  assert.equal(disabledInvalidMode.statusCode, 404);

  const unattested = await invoke(createWeeklyRetrospectivesHandler({
    env: env({
      FOLDARIUM_ENV: 'preview',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ENABLED: '1',
    }),
    fetchImpl,
  }), {
    query: { admin: '1', round_id: week.publication.round_id },
  });
  assert.equal(unattested.statusCode, 404);
  assert.equal(fetchImpl.calls.length, 0);

  const enabled = await invoke(createWeeklyRetrospectivesHandler({
    env: env({
      FOLDARIUM_ENV: 'preview',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ENABLED: '1',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ACCESS: 'authenticated-proxy',
    }),
    fetchImpl,
  }), {
    query: { admin: '1', round_id: week.publication.round_id },
  });
  assert.equal(enabled.statusCode, 200);
  assert.equal(enabled.body.format_version, ARCHIVE_ADMIN_DETAIL_FORMAT_VERSION);
  assert.equal(enabled.headers['Cache-Control'], 'no-store');
  assert.equal(enabled.body.answer_overlays[0].item_id, ITEM_ID);
  assert.equal(
    enabled.body.retrospective.participants.find(row => row.participant_kind === 'human').participant,
    '<img src=x onerror="steal()">',
  );
  assert.doesNotMatch(enabled.serialized, /participant_link|11111111|object_uri|sha256/);
});

test('public all-time includes chosen human pseudonyms without private linkage', async () => {
  const weeks = [buildWeek({ index: 0 }), buildWeek({ index: 1 })];
  const response = await invoke(createWeeklyRetrospectivesHandler({
    env: env(),
    fetchImpl: archiveFetch(weeks),
  }), {
    query: { all_time: '1', ranking: 'weighted_average_accuracy' },
  });
  assert.equal(response.statusCode, 200);
  assert.equal(response.body.format_version, ARCHIVE_ALL_TIME_FORMAT_VERSION);
  assert.equal(response.body.scope, 'public');
  assert.deepEqual(
    response.body.participants.map(row => row.participant_kind),
    ['llm', 'human', 'baseline'],
  );
  assert.equal(response.body.participants[0].total_correct, 2);
  assert.equal(response.body.participants[0].total_questions, 2);
  assert.equal(response.body.participants[0].weighted_average_accuracy, 100);
  assert.equal(response.body.participants[0].provisional, true);
  const human = response.body.participants.find(
    row => row.participant_kind === 'human',
  );
  assert.equal(human.participant, 'PocketFox');
  assert.equal(human.total_correct, 2);
  assert.doesNotMatch(response.serialized, /participant_link|11111111/);
  assert.match(response.headers['Cache-Control'], /s-maxage=300/);

  const humansOnly = await invoke(createWeeklyRetrospectivesHandler({
    env: env(),
    fetchImpl: archiveFetch(weeks),
  }), {
    query: {
      all_time: '1',
      ranking: 'weighted_average_accuracy',
      participant_kind: 'human',
    },
  });
  assert.equal(humansOnly.statusCode, 200);
  assert.deepEqual(
    humansOnly.body.participants.map(row => row.participant),
    ['PocketFox'],
  );
});

test('admin all-time groups humans by HMAC linkage and uses the latest raw pseudonym', async () => {
  const weeks = [
    buildWeek({
      index: 0,
      humanName: 'OlderFox',
      humanChoice: 'choice-a',
      revealedAt: '2026-08-20T00:00:00Z',
    }),
    buildWeek({
      index: 1,
      humanName: '<script>newer()</script>',
      humanChoice: 'choice-b',
      revealedAt: '2026-08-22T00:00:00Z',
    }),
  ];
  const response = await invoke(createWeeklyRetrospectivesHandler({
    env: env({
      FOLDARIUM_ENV: 'preview',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ENABLED: '1',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ACCESS: 'authenticated-proxy',
      FOLDARIUM_WEEKLY_RETROSPECTIVE_PARTICIPANT_HMAC_KEY: HMAC_KEY,
    }),
    fetchImpl: archiveFetch(weeks),
  }), {
    query: {
      admin: '1',
      all_time: '1',
      participant_kind: 'human',
    },
  });
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers['Cache-Control'], 'no-store');
  assert.equal(response.body.scope, 'admin');
  assert.equal(response.body.participants.length, 1);
  const human = response.body.participants[0];
  assert.equal(human.participant, '<script>newer()</script>');
  assert.equal(human.complete_weeks, 2);
  assert.equal(human.total_correct, 1);
  assert.equal(human.total_questions, 2);
  assert.equal(human.weighted_average_accuracy, 50);
  assert.equal(human.provisional, true);
  assert.doesNotMatch(
    response.serialized,
    new RegExp(`${HUMAN_ID}|${HMAC_KEY}|OlderFox|participant_link|sha256`),
  );
});

test('tampered, open, unknown, and unpublished exact rounds share one fail-closed response', async () => {
  const original = buildWeek();
  const variants = [
    [buildWeek(), week => { week.round.status = 'open'; }],
    [buildWeek(), week => { week.round.reveal_manifest.items[0].choices[0].rmsd = 9; }],
    [buildWeek(), week => {
      const publicDigest = week.publication.public_artifact_sha256;
      week.objects.set(publicDigest, Buffer.from('{}'));
    }],
  ];
  for (const [week, mutate] of variants) {
    mutate(week);
    const response = await invoke(createWeeklyRetrospectivesHandler({
      env: env(),
      fetchImpl: archiveFetch([week]),
    }), {
      query: { round_id: week.publication.round_id },
    });
    assert.equal(response.statusCode, 404);
    assert.deepEqual(response.body, { error: 'Not found' });
    assert.equal(response.headers['Cache-Control'], 'no-store');
  }
  for (const roundId of ['unknown-round', 'unpublished-round']) {
    const response = await invoke(createWeeklyRetrospectivesHandler({
      env: env(),
      fetchImpl: archiveFetch([original]),
    }), { query: { round_id: roundId } });
    assert.equal(response.statusCode, 404);
    assert.deepEqual(response.body, { error: 'Not found' });
  }
});

test('archive assets are confined to the configured HTTPS public storage origin', () => {
  const origin = 'https://example.supabase.co';
  assert.equal(
    projectPublicAssetUri('supabase://structures/round/pose.pdb', origin),
    `${origin}/storage/v1/object/public/structures/round/pose.pdb`,
  );
  assert.equal(
    projectPublicAssetUri(
      `${origin}/storage/v1/object/public/structures/round/pose.pdb`,
      origin,
    ),
    `${origin}/storage/v1/object/public/structures/round/pose.pdb`,
  );
  for (const value of [
    'javascript:alert(1)',
    'http://example.supabase.co/storage/v1/object/public/structures/pose.pdb',
    'https://evil.example/storage/v1/object/public/structures/pose.pdb',
    `${origin}/storage/v1/object/authenticated/structures/pose.pdb`,
    `${origin}/storage/v1/object/public/structures/../private/pose.pdb`,
  ]) {
    assert.throws(() => projectPublicAssetUri(value, origin));
  }
});

test('config and participant HMAC validation fail closed without deriving a fallback key', async () => {
  const week = buildWeek();
  const unconfigured = await invoke(createWeeklyRetrospectivesHandler({
    env: {},
    fetchImpl: archiveFetch([week]),
  }));
  assert.equal(unconfigured.statusCode, 503);
  assert.equal(unconfigured.headers['Cache-Control'], 'no-store');

  for (const key of [
    undefined,
    'too-short',
    `malicious\n${'x'.repeat(40)}`,
  ]) {
    const response = await invoke(createWeeklyRetrospectivesHandler({
      env: env({
        FOLDARIUM_ENV: 'preview',
        FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ENABLED: '1',
        FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ACCESS: 'authenticated-proxy',
        FOLDARIUM_WEEKLY_RETROSPECTIVE_PARTICIPANT_HMAC_KEY: key,
      }),
      fetchImpl: archiveFetch([week]),
    }), {
      query: { admin: '1', all_time: '1' },
    });
    assert.equal(response.statusCode, 404);
    assert.deepEqual(response.body, { error: 'Not found' });
    assert.equal(response.headers['Cache-Control'], 'no-store');
  }
});

test('quiz backend exposes retrospective methods on remote, deferred, read-only, and disabled surfaces', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(String(url));
    return Response.json({ ok: true });
  };
  const storage = {
    length: 0,
    key: () => null,
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  try {
    const remote = createQuizBackend({
      client: {},
      getClient: async () => ({}),
      storage,
    });
    await remote.getWeeklyRetrospectiveArchive({ limit: 5, cursor: 'opaque' });
    await remote.getWeeklyRetrospectiveDetail('round-7');
    await remote.getWeeklyRetrospectiveAllTime({ participantKind: 'llm' });
    await remote.getWeeklyRetrospectiveAdmin({ roundId: 'round-7' });
    assert.match(calls[0], /weekly-retrospectives\?limit=5&cursor=opaque/);
    assert.match(calls[1], /round_id=round-7/);
    assert.match(calls[2], /all_time=1.*participant_kind=llm/);
    assert.match(calls[3], /admin=1.*round_id=round-7/);

    const deferred = createDeferredBackend();
    const pending = deferred.getWeeklyRetrospectiveDetail('round-8');
    deferred.attach({ getWeeklyRetrospectiveDetail: async roundId => ({ roundId }) });
    assert.deepEqual(await pending, { roundId: 'round-8' });

    const readOnly = initQuizBackend(
      {
        url: 'https://example.supabase.co',
        publishableKey: 'sb_publishable_test',
        writable: false,
      },
      {
        createClient: () => ({}),
        storage,
      },
    );
    assert.deepEqual(await readOnly.getWeeklyRetrospectiveAllTime(), { ok: true });

    const disabled = initQuizBackend({}, { storage });
    await assert.rejects(
      disabled.getWeeklyRetrospectiveArchive(),
      /archive is unavailable/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
