import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isUnscorableReference, projectReferenceDisposition, referencePopulation } from '../lib/reference-disposition.js';
import { buildAnswerOverlays } from '../lib/private-evaluation-contract.js';
import { buildPublishedMethodStats } from '../method-performance.js';
import { buildSelectorAnswerKeys, scoreSelectorSubmission, scoreWeeklySelectorResults } from '../lib/weekly-selector-results.js';
import { verifyPublicArtifact, verifyAdminArtifact, publishHumanPseudonyms } from '../lib/weekly-retrospectives.js';
import { createHash } from 'node:crypto';

const artifact = JSON.parse(readFileSync(new URL('./fixtures/private-evaluation-v6-unscorable.golden.json', import.meta.url)));
const item = () => structuredClone(artifact.reveal_manifest.items[0]);

test('Python v6 fixture has no fabricated crystal overlay or score', () => {
  assert.equal(isUnscorableReference(item()), true);
  assert.deepEqual(referencePopulation(artifact.reveal_manifest.items), { item_count: 1, scorable_item_count: 0, excluded_item_count: 1 });
  assert.deepEqual(buildAnswerOverlays([], artifact.reveal_manifest), []);
  assert.throws(() => buildAnswerOverlays([{ item_id: '9XYZ' }], artifact.reveal_manifest));
});

test('only exact complete dispositions permit missing scores', () => {
  for (const change of [
    x => { x.choices[0].correct = false; },
    x => { x.choices[0].reference_sha256 = 'a'.repeat(64); },
    x => { x.reference_disposition.explicitly_unobserved_heavy_atoms += 1; },
    x => { x.reference_disposition.reference_coverage = 0.9; },
    x => { x.reference_disposition.extra = 'unreviewed'; },
    x => { x.evaluation_status = 'skip'; },
    x => { delete x.reference_disposition; },
  ]) {
    const altered = item(); change(altered);
    assert.throws(() => isUnscorableReference(altered));
  }
  const projected = { ...item(), ...projectReferenceDisposition(item()) };
  assert.equal(isUnscorableReference(projected, { publicProjection: true }), true);
  assert.throws(() => isUnscorableReference(projected));
});

function blind() {
  const b = structuredClone(artifact.blind_manifest);
  for (const c of b.items[0].choices) {
    c.smina_score = { metric: 'smina_affinity', protocol: 'score_only', scoring_function: 'vina', units: 'kcal/mol', value: -5 };
    c.confidence = { metric: 'ligand_plddt', value: 90 };
  }
  return b;
}

test('none decisions and every baseline stay unscored while full submissions remain mandatory', () => {
  const b = blind();
  const keys = buildSelectorAnswerKeys(b, artifact.reveal_manifest, 1);
  const submissions = [{ item_id: '9XYZ', clustered: { selection_kind: 'none' }, unclustered: { selection_kind: 'none' } }];
  assert.deepEqual(scoreSelectorSubmission(submissions, keys, 1), { clusteredCorrect: 0, unclusteredCorrect: 0, answered: 1 });
  assert.throws(() => scoreSelectorSubmission([], keys, 1));
  const result = scoreWeeklySelectorResults({ roundId: b.round_id, itemCount: 1, blindManifest: b, revealManifest: artifact.reveal_manifest });
  assert.equal(result.item_count, 1); assert.equal(result.scorable_item_count, 0);
  assert.equal(result.rows[0].clustered.accuracy, null);
  assert.equal(result.rows[0].clustered.item_count, 0);
  assert.ok(result.questions[0].clustered.answers.every(answer => answer.correct === null));
});

test('method aggregates explicitly count exclusions without wins or losses', () => {
  const b = blind();
  const r = structuredClone(artifact.reveal_manifest);
  r.items = r.items.map(x => ({ ...x, ...projectReferenceDisposition(x) }));
  const stats = buildPublishedMethodStats([{ blindProjection: b, revealProjection: r }]);
  assert.equal(stats.weeks.length, 2);
  for (const row of stats.weeks) {
    assert.equal(row.targets, 0); assert.equal(row.excluded_targets, 1);
    assert.equal(row.oracle_successes, 0); assert.equal(row.top1_evaluated, 0);
  }
});

for (const label of ['mixed', 'all']) {
  test(`Python retrospective v2 ${label} fixture preserves scored and full populations`, () => {
    const fixture = JSON.parse(readFileSync(new URL(`./fixtures/retrospective-v2-${label}.golden.json`, import.meta.url)));
    const publicBytes = Buffer.from(JSON.stringify(fixture.public));
    const adminBytes = Buffer.from(JSON.stringify(fixture.admin));
    const descriptor = bytes => ({ sha256: createHash('sha256').update(bytes).digest('hex'), sizeBytes: bytes.length, mediaType: 'application/json' });
    const round = fixture.public.round;
    const publication = { roundId: round.round_id, campaignId: round.campaign_id, opensAt: round.opens_at, closesAt: round.closes_at,
      evaluationFormatVersion: 'foldarium.weekly-private-evaluation/v6',
      revealedAt: round.revealed_at, itemCount: round.item_count, choiceCount: round.choice_count,
      descriptors: { public_artifact: descriptor(publicBytes), admin_artifact: descriptor(adminBytes) } };
    const pub = verifyPublicArtifact(publicBytes, publication);
    const admin = verifyAdminArtifact(adminBytes, publication);
    const exposed = publishHumanPseudonyms({ publication, publicArtifact: pub, adminArtifact: admin });
    assert.equal(pub.round.item_count, 2);
    assert.equal(pub.round.scorable_item_count, label === 'all' ? 0 : 1);
    assert.equal(exposed.questions.length, 2);
    for (const q of exposed.questions.filter(q => q.evaluation_status === 'unscorable')) {
      assert.equal(q.human_aggregate.correct_count, null);
      assert.equal(q.human_aggregate.scorable_answered_count, 0);
      assert.ok(q.automated_entries.every(r => r.correct === null));
    }
    if (label === 'all') for (const r of exposed.automated_entries) { assert.equal(r.accuracy, null); assert.equal(r.complete, false); }
  });
}
