import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { buildFeaturedResults, buildFeaturedAllTime, verifyFeaturedSelection } from '../lib/weekly-featured-results.js';
import { projectReferenceDisposition } from '../lib/reference-disposition.js';
const hash = value => createHash('sha256').update(value).digest('hex');
const proof = JSON.parse(readFileSync(new URL('./fixtures/private-evaluation-v6-unscorable.golden.json', import.meta.url))).reveal_manifest.items[0].reference_disposition;

function featuredFixture({ excluded = [4], humanItems = [0, 1, 2, 3, 4], registeredAt = '2026-08-15T00:00:00Z' } = {}) {
  const roundId = 'featured-fixture';
  const items = Array.from({ length: 7 }, (_, index) => ({ id: `item-${index}`,
    ...(excluded.includes(index) ? { evaluation_status: 'unscorable', reference_disposition: proof } : {}),
    choices: ['a', 'b'].map((id, n) => ({ id, correct: excluded.includes(index) ? null : n === 0,
      accepted_correct: excluded.includes(index) ? null : n === 0,
      rmsd: excluded.includes(index) ? null : n === 0 ? 1 : 3, reference_sha256: proof.reference_sha256 })) }));
  const blindManifest = { round_id: roundId, items: items.map(item => ({ id: item.id,
    choices: item.choices.map(c => ({ id: c.id, confidence: { metric: 'ligand_plddt', value: c.id === 'a' ? 90 : 50 } })) })) };
  const context = { blindManifest, revealManifest: { round_id: roundId, items } };
  const publication = { roundId, closesAt: '2026-08-17T20:00:00Z', revealedAt: '2026-08-18T00:00:00Z', itemCount: 7,
    digests: { blind_manifest_sha256: hash(JSON.stringify(blindManifest)), private_index_sha256: 'a'.repeat(64) } };
  const audit = { schema_version: 1, policy: 'foldarium-weekly-question-draw/v1', mode: 'uniform', seed: 'fixture',
    source_round_id: roundId, source_blind_manifest_sha256: publication.digests.blind_manifest_sha256,
    source_private_index_sha256: publication.digests.private_index_sha256, candidate_population_sha256: 'b'.repeat(64),
    source_item_count: 7, candidate_count: 7, requested_question_count: 5, selected_question_count: 5,
    included_item_ids: items.slice(0, 5).map(item => item.id) };
  const bytes = Buffer.from(JSON.stringify(audit));
  const sha256 = hash(bytes);
  const marker = { ...audit, blind_manifest_sha256: audit.source_blind_manifest_sha256, selection_sha256: sha256, item_ids: audit.included_item_ids };
  const row = { round_id: roundId, environment: 'production', blind_manifest_sha256: audit.source_blind_manifest_sha256,
    registered_at: registeredAt, featured_questions: marker,
    selection_artifact: { sha256, object_uri: `supabase://private-bucket/sha256/${sha256.slice(0, 2)}/${sha256}`, media_type: 'application/json', size_bytes: bytes.length } };
  const sourceSnapshot = { participants: [
    { participantLink: 'human-private', participantKind: 'human', displayName: 'PocketFox' },
    { participantLink: 'llm-private', participantKind: 'llm', displayName: 'Claude Opus' },
  ], votes: [
    ...humanItems.map(index => ({ participantLink: 'human-private', itemId: `item-${index}`, choiceId: 'a', pickedNone: false, selectionKind: 'exact' })),
    ...items.map(item => ({ participantLink: 'llm-private', itemId: item.id, choiceId: 'a', pickedNone: false, selectionKind: 'exact' })),
  ] };
  const publicArtifact = { format_version: `foldarium.weekly-retrospective-public/v${excluded.length ? 2 : 1}`,
    round: { item_count: 7, scorable_item_count: 7 - excluded.length, excluded_item_count: excluded.length },
    automated_entries: [{ participant: 'Smina', participant_kind: 'baseline' }],
    questions: items.map(item => ({ item_id: item.id,
      ...(excluded.length ? item.evaluation_status ? projectReferenceDisposition(item) : { evaluation_status: 'scored' } : {}),
      automated_entries: [{ participant: 'Smina', participant_kind: 'baseline', choice_id: 'a', correct: item.choices[0].correct }] })) };
  return { publication, context, sourceSnapshot, publicArtifact, row, bytes,
    selection: verifyFeaturedSelection(row, bytes, publication, context) };
}

test('five featured answers complete the assignment, score only four eligible items, preserve seven-item coverage', () => {
  const fixture = featuredFixture();
  const result = buildFeaturedResults(fixture);
  assert.equal(result.assignment_total, 5);
  assert.equal(result.scorable_item_count, 4);
  assert.equal(result.full_round_item_count, 7);
  assert.equal(result.participants.length, 4);
  for (const row of result.participants) {
    assert.equal(row.assignment_complete, true);
    assert.equal(row.assignment_answered, 5);
    assert.equal(row.correct, 4);
    assert.equal(row.answered, 4);
    assert.equal(row.excluded_answered, 1);
    assert.equal(row.accuracy, 100);
  }
  assert.equal(result.participants.find(r => r.participant_kind === 'human').full_round_answered, 5);
  assert.doesNotMatch(JSON.stringify(result), /human-private|llm-private|sha256|source_snapshot|selection_artifact/);
  const allTime = buildFeaturedAllTime([fixture]);
  const human = allTime.participants.find(r => r.participant_kind === 'human');
  assert.equal(human.complete_weeks, 1);
  assert.equal(human.total_questions, 4);
  assert.equal(human.total_correct, 4);
  assert.equal(human.weighted_average_accuracy, 100);
});

test('skipping the unscorable featured item leaves assignment incomplete without fabricating a None win', () => {
  const fixture = featuredFixture({ humanItems: [0, 1, 2, 3, 5, 6] });
  const human = buildFeaturedResults(fixture).participants.find(r => r.participant_kind === 'human');
  assert.equal(human.assignment_complete, false);
  assert.equal(human.assignment_answered, 4);
  assert.equal(human.correct, 4);
  assert.equal(human.full_round_answered, 6);
  const total = buildFeaturedAllTime([fixture]).participants.find(r => r.participant_kind === 'human');
  assert.equal(total.complete_weeks, 0);
  assert.equal(total.rank, null);
  assert.equal(total.weighted_average_accuracy, null);
});

test('all-unscorable featured assignment completes with a null score and no rank', () => {
  const fixture = featuredFixture({ excluded: [0, 1, 2, 3, 4] });
  for (const row of buildFeaturedResults(fixture).participants) {
    assert.equal(row.assignment_complete, true);
    assert.equal(row.correct, 0);
    assert.equal(row.total, 0);
    assert.equal(row.accuracy, null);
  }
  for (const row of buildFeaturedAllTime([fixture]).participants) {
    assert.equal(row.complete_weeks, 1);
    assert.equal(row.total_questions, 0);
    assert.equal(row.rank, null);
  }
});

test('post-close or absent draws never rewrite historical completion', () => {
  for (const registeredAt of ['2026-08-17T20:00:00Z', '2026-08-18T00:00:00Z']) {
    const fixture = featuredFixture({ registeredAt });
    assert.equal(fixture.selection, null);
    assert.equal(buildFeaturedResults(fixture), null);
    assert.equal(buildFeaturedAllTime([fixture]).eligible_week_count, 0);
  }
  assert.equal(verifyFeaturedSelection(null, null, {}, {}), null);
});

test('forged IDs, source digests, artifact bytes, and client-created scope cannot authorize a cohort', () => {
  const fixture = featuredFixture();
  for (const mutate of [
    row => { row.featured_questions.item_ids = ['item-6']; },
    row => { row.blind_manifest_sha256 = 'c'.repeat(64); },
    row => { row.round_id = 'different-round'; },
    row => { row.environment = 'preview'; },
    row => { row.featured_questions.selection_sha256 = 'd'.repeat(64); },
  ]) {
    const row = structuredClone(fixture.row); mutate(row);
    assert.throws(() => verifyFeaturedSelection(row, fixture.bytes, fixture.publication, fixture.context));
  }
  assert.throws(() => verifyFeaturedSelection(fixture.row, Buffer.concat([fixture.bytes, Buffer.from(' ')]), fixture.publication, fixture.context), /digest/);
  assert.throws(() => buildFeaturedResults({ ...fixture, selection: { itemIds: ['item-0'], mode: 'uniform' } }), /bound/);
  assert.throws(() => buildFeaturedResults({ ...fixture, publication: { ...fixture.publication, closesAt: '2026-08-19T00:00:00Z' } }), /bound/);
});

test('extra full-round votes cannot improve featured score or create another cohort participation', () => {
  const fixture = featuredFixture({ excluded: [], humanItems: [5, 6] });
  assert.equal(buildFeaturedResults(fixture).participants.some(r => r.participant_kind === 'human'), false);
  assert.equal(buildFeaturedAllTime([fixture]).participants.some(r => r.participant_kind === 'human'), false);
  const original = featuredFixture({ excluded: [] });
  const before = buildFeaturedResults(original).participants.find(r => r.participant_kind === 'human');
  original.sourceSnapshot.votes.push({ participantLink: 'human-private', itemId: 'item-5', choiceId: 'a', pickedNone: false, selectionKind: 'exact' });
  const after = buildFeaturedResults(original).participants.find(r => r.participant_kind === 'human');
  assert.equal(before.correct, after.correct);
  assert.equal(after.full_round_answered, before.full_round_answered + 1);
});


test('stripped reference proof cannot turn unscorable featured answers into None wins', () => {
  const fixture = featuredFixture();
  fixture.context.revealManifest.items[4].evaluation_status = undefined;
  delete fixture.context.revealManifest.items[4].reference_disposition;
  Object.assign(fixture.sourceSnapshot.votes.find(v => v.participantLink === 'human-private' && v.itemId === 'item-4'),
    { pickedNone: true, choiceId: null, selectionKind: 'none' });
  assert.throws(() => buildFeaturedResults(fixture), /correctness/);
});

// Both raw poses belong to a correct cluster, but the chosen representative
// itself fails the exact RMSD threshold. All four competitors pick that pose.
function clusterOnlyFixture() {
  const fixture = featuredFixture({ excluded: [] });
  const [a, b] = fixture.context.revealManifest.items[0].choices;
  Object.assign(a, { rmsd: 2, correct: false, accepted_correct: true });
  Object.assign(b, { rmsd: 0.8, correct: true, accepted_correct: true });
  fixture.publicArtifact.questions[0].automated_entries[0].correct = false;
  return fixture;
}

test('featured exact human and LLM choices use raw correctness, matching both baselines', () => {
  const fixture = clusterOnlyFixture();
  const rows = buildFeaturedResults(fixture).participants;
  assert.equal(rows.length, 4);
  for (const row of rows) {
    assert.equal(row.correct, 4, row.participant);
    assert.equal(row.accuracy, 80, row.participant);
  }
  for (const row of buildFeaturedAllTime([fixture]).participants) {
    assert.equal(row.total_correct, 4, row.participant);
    assert.equal(row.total_questions, 5, row.participant);
  }
});

test('only explicit cluster ballots receive correct-cluster credit, for humans and LLMs', () => {
  for (const participantLink of ['human-private', 'llm-private']) {
    const fixture = clusterOnlyFixture();
    fixture.sourceSnapshot.votes.find(v => v.participantLink === participantLink && v.itemId === 'item-0').selectionKind = 'cluster';
    const clusterKind = participantLink === 'human-private' ? 'human' : 'llm';
    for (const row of buildFeaturedResults(fixture).participants) {
      assert.equal(row.correct, row.participant_kind === clusterKind ? 5 : 4, row.participant);
    }
  }
});

test('None fails when any raw pose passes, succeeds when none passes, and never scores unscorable references', () => {
  const fixture = clusterOnlyFixture();
  const vote = fixture.sourceSnapshot.votes.find(v => v.participantLink === 'human-private' && v.itemId === 'item-0');
  Object.assign(vote, { pickedNone: true, choiceId: null, selectionKind: 'none' });
  const human = () => buildFeaturedResults(fixture).participants.find(r => r.participant_kind === 'human');
  assert.equal(human().correct, 4);
  for (const choice of fixture.context.revealManifest.items[0].choices) {
    Object.assign(choice, { rmsd: 3, correct: false, accepted_correct: false });
  }
  assert.equal(human().correct, 5);
  const unscorable = featuredFixture();
  Object.assign(unscorable.sourceSnapshot.votes.find(v => v.participantLink === 'human-private' && v.itemId === 'item-4'),
    { pickedNone: true, choiceId: null, selectionKind: 'none' });
  const result = buildFeaturedResults(unscorable).participants.find(r => r.participant_kind === 'human');
  assert.equal(result.correct, 4);
  assert.equal(result.answered, 4);
  assert.equal(result.assignment_complete, true);
});

test('featured scoring rejects ambiguous or inconsistent selection semantics', () => {
  for (const change of [
    { selectionKind: undefined }, { selectionKind: 'unknown' }, { selectionKind: 'none' },
    { pickedNone: true, selectionKind: 'none' },
    { pickedNone: true, choiceId: null, selectionKind: 'exact' },
    { pickedNone: 'false' },
  ]) {
    const fixture = featuredFixture();
    Object.assign(fixture.sourceSnapshot.votes[0], change);
    assert.throws(() => buildFeaturedResults(fixture), /source vote/);
  }
});
