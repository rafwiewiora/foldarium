import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { verifyPublicArtifact } from '../lib/weekly-retrospectives.js';
import { isUnscorableReference, projectReferenceDisposition, referencePopulation } from '../lib/reference-disposition.js';
import { buildSelectorAnswerKeys, scoreSelectorSubmission, scoreWeeklySelectorResults } from '../lib/weekly-selector-results.js';
import { buildAnswerOverlays } from '../lib/private-evaluation-contract.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/private-evaluation-v6-unscorable.golden.json', import.meta.url)));
function bridgeItem() {
  const item = structuredClone(fixture.reveal_manifest.items[0]);
  Object.assign(item.reference_disposition, {
    policy: 'foldarium.released-reference-disposition/v2', code: 'missing_reference_bridge',
    expected_heavy_atoms: 45, observed_heavy_atoms: 38, explicitly_unobserved_heavy_atoms: 7,
    reference_coverage: 38 / 45,
    missing_heavy_atom_ids: ['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'SI1'],
    observed_component_count: 2, missing_component_boundary_counts: [2],
    ccd_heavy_atom_graph_sha256: 'a'.repeat(64),
  });
  return item;
}

test('missing reference bridge is a distinct audited exclusion above the coverage threshold', () => {
  const item = bridgeItem();
  assert.equal(isUnscorableReference(item), true);
  assert.deepEqual(referencePopulation([item]), { item_count: 1, scorable_item_count: 0, excluded_item_count: 1 });
  const projected = { ...item, ...projectReferenceDisposition(item) };
  assert.equal(isUnscorableReference(projected, { publicProjection: true }), true);
  assert.equal('reference_sha256' in projected.reference_disposition, false);
  assert.equal('ccd_heavy_atom_graph_sha256' in projected.reference_disposition, false);
  assert.throws(() => isUnscorableReference(projected), /disposition/);
});

test('bridge proofs reject unsupported policies, fake topology, atom mismatches and fabricated scores', () => {
  for (const change of [
    d => { d.policy = 'foldarium.released-reference-disposition/v1'; },
    d => { d.code = 'insufficient_reference_coverage'; },
    d => { d.missing_heavy_atom_ids[0] = 'C2'; },
    d => { d.missing_heavy_atom_ids.reverse(); },
    d => { d.missing_heavy_atom_ids.pop(); },
    d => { d.missing_heavy_atom_ids[0] = ''; },
    d => { d.observed_component_count = 1; },
    d => { d.observed_component_count = 39; },
    d => { d.missing_component_boundary_counts = [1]; },
    d => { d.missing_component_boundary_counts = [3]; },
    d => { d.missing_component_boundary_counts = [2, 1]; },
    d => { d.missing_component_boundary_counts = [true]; },
    d => { d.missing_component_boundary_counts = Array(8).fill(2); },
    d => { delete d.ccd_heavy_atom_graph_sha256; },
    d => { d.ccd_heavy_atom_graph_sha256 = 'not a hash'; },
    d => { d.ccd_heavy_atom_graph_sha256 = ['a'.repeat(64)]; },
    d => { d.observed_heavy_atoms = 30; d.explicitly_unobserved_heavy_atoms = 15; d.reference_coverage = 30 / 45; },
    d => { d.extra = 'unknown'; },
  ]) {
    const item = bridgeItem(); change(item.reference_disposition);
    assert.throws(() => isUnscorableReference(item), /disposition/);
  }
  for (const field of ['rmsd', 'correct', 'accepted_correct']) {
    const item = bridgeItem(); item.choices[0][field] = field === 'rmsd' ? 0 : false;
    assert.throws(() => isUnscorableReference(item), /metrics/);
  }
  const legacy = structuredClone(fixture.reveal_manifest.items[0]);
  legacy.reference_disposition.observed_heavy_atoms = 38;
  legacy.reference_disposition.expected_heavy_atoms = 45;
  legacy.reference_disposition.explicitly_unobserved_heavy_atoms = 7;
  legacy.reference_disposition.reference_coverage = 38 / 45;
  assert.throws(() => isUnscorableReference(legacy), /disposition/, 'v1 never relabels above-threshold coverage as insufficient');
});

test('bridge exclusions preserve every choice and None remains unscored across selectors', () => {
  const item = bridgeItem();
  const reveal = { ...fixture.reveal_manifest, items: [item] };
  assert.deepEqual(buildAnswerOverlays([], reveal), []);
  const blind = structuredClone(fixture.blind_manifest);
  for (const choice of blind.items[0].choices) {
    choice.smina_score = { metric: 'smina_affinity', protocol: 'score_only', scoring_function: 'vina', units: 'kcal/mol', value: -5 };
    choice.confidence = { metric: 'ligand_plddt', value: 90 };
  }
  const keys = buildSelectorAnswerKeys(blind, reveal, 1);
  const decisions = [{ item_id: item.id, clustered: { selection_kind: 'none' }, unclustered: { selection_kind: 'none' } }];
  assert.deepEqual(scoreSelectorSubmission(decisions, keys, 1), { clusteredCorrect: 0, unclusteredCorrect: 0, answered: 1 });
  const result = scoreWeeklySelectorResults({ roundId: fixture.blind_manifest.round_id, itemCount: 1, blindManifest: blind, revealManifest: reveal });
  assert.equal(result.item_count, 1); assert.equal(result.scorable_item_count, 0);
  assert.equal(result.rows[0].clustered.accuracy, null);
  assert.ok(result.questions[0].clustered.answers.every(answer => answer.correct === null));
  assert.equal(reveal.items[0].choices.length, fixture.reveal_manifest.items[0].choices.length);
});


test('Python-generated missing-bridge retrospective passes the public consumer without private hashes', () => {
  const artifact = JSON.parse(readFileSync(new URL('./fixtures/retrospective-v2-missing-bridge.golden.json', import.meta.url)));
  const bytes = Buffer.from(JSON.stringify(artifact));
  const round = artifact.round;
  const publication = {
    roundId: round.round_id, campaignId: round.campaign_id,
    opensAt: round.opens_at, closesAt: round.closes_at, revealedAt: round.revealed_at,
    evaluationFormatVersion: 'foldarium.weekly-private-evaluation/v6',
    itemCount: round.item_count, choiceCount: round.choice_count,
    descriptors: { public_artifact: { sha256: createHash('sha256').update(bytes).digest('hex'), sizeBytes: bytes.length, mediaType: 'application/json' } },
  };
  const verified = verifyPublicArtifact(bytes, publication);
  assert.equal(verified.round.item_count, 2);
  assert.equal(verified.round.scorable_item_count, 1);
  const question = verified.questions.find(item => item.evaluation_status === 'unscorable');
  assert.equal(question.reference_disposition.code, 'missing_reference_bridge');
  assert.equal(question.human_aggregate.correct_count, null);
  assert.ok(question.automated_entries.every(entry => entry.correct === null));
  assert.doesNotMatch(JSON.stringify(question.reference_disposition), /_sha256/);
});
