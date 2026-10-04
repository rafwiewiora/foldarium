import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { buildSelectorAnswerKeys, scoreSelectorSubmission } from '../lib/weekly-selector-results.js';
import { verifyPublicArtifact, verifyAdminArtifact, buildPublicDetail } from '../lib/weekly-retrospectives.js';
import { projectReferenceDisposition } from '../lib/reference-disposition.js';
import { buildPublishedMethodStats, aggregateMethodStats, validateMethodStats } from '../method-performance.js';

const load = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url)));
const v6 = () => load('private-evaluation-v6-unscorable.golden.json');
const mixed = () => load('retrospective-v2-mixed.golden.json');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
function packaged(fixture, version = 'v6') {
  const pub = Buffer.from(JSON.stringify(fixture.public));
  const admin = Buffer.from(JSON.stringify(fixture.admin));
  const descriptor = bytes => ({ sha256: hash(bytes), sizeBytes: bytes.length, mediaType: 'application/json' });
  const round = fixture.public.round;
  return { pub, admin, publication: {
    roundId: round.round_id, campaignId: round.campaign_id,
    opensAt: round.opens_at, closesAt: round.closes_at, revealedAt: round.revealed_at,
    itemCount: round.item_count, choiceCount: round.choice_count,
    evaluationFormatVersion: `foldarium.weekly-private-evaluation/${version}`,
    descriptors: { public_artifact: descriptor(pub), admin_artifact: descriptor(admin) },
  } };
}

test('stripping reference proof cannot turn null metrics into a correct None decision', () => {
  const fixture = v6();
  const source = fixture.reveal_manifest;
  const item = source.items[0];
  const decisions = [{ item_id: item.id, clustered: { selection_kind: 'none' }, unclustered: { selection_kind: 'none' } }];
  const valid = buildSelectorAnswerKeys(fixture.blind_manifest, source, 1);
  assert.equal(scoreSelectorSubmission(decisions, valid, 1).clusteredCorrect, 0);
  assert.equal(scoreSelectorSubmission(decisions, valid, 1).unclusteredCorrect, 0);
  delete item.evaluation_status;
  delete item.reference_disposition;
  assert.throws(() => buildSelectorAnswerKeys(fixture.blind_manifest, source, 1), /correct|scored|score|boolean|invalid/i);
});

test('public and admin v2 formats require the exact v6 evaluation catalog version', () => {
  const valid = packaged(mixed());
  assert.equal(verifyPublicArtifact(valid.pub, valid.publication).format_version, 'foldarium.weekly-retrospective-public/v2');
  assert.equal(verifyAdminArtifact(valid.admin, valid.publication).format_version, 'foldarium.weekly-retrospective-admin/v2');
  const invalid = packaged(mixed(), 'v5');
  assert.throws(() => verifyPublicArtifact(invalid.pub, invalid.publication), /format|version/i);
  assert.throws(() => verifyAdminArtifact(invalid.admin, invalid.publication), /format|version/i);
});

test('participant full and excluded counts must match the verified round population', () => {
  for (const [kind, verify, key] of [['public', verifyPublicArtifact, 'automated_entries'], ['admin', verifyAdminArtifact, 'participants']]) {
    const fixture = mixed();
    // Internally consistent but falsely claims a third full-population item.
    fixture[kind][key][0].full_total += 1;
    fixture[kind][key][0].excluded_item_count += 1;
    const encoded = packaged(fixture);
    assert.throws(() => verify(kind === 'public' ? encoded.pub : encoded.admin, encoded.publication), /population|coverage|incomplete|count/i);
  }
});

function contextForMixed(fixture) {
  const proof = { ...fixture.public.questions[1].reference_disposition, reference_sha256: 'a'.repeat(64) };
  const answers = fixture.public.questions.map((question, index) => ({
    id: question.item_id,
    ...(index === 1 ? { evaluation_status: 'unscorable', reference_disposition: proof } : {}),
    choices: (index === 0 ? ['choice-a', 'choice-b'] : ['choice-0', 'choice-1']).map((id, choiceIndex) => ({
      id, correct: index ? null : choiceIndex === 0,
      accepted_correct: index ? null : choiceIndex === 0,
      rmsd: index ? null : choiceIndex === 0 ? 1 : 3,
      reference_sha256: proof.reference_sha256,
    })),
  }));
  const revealManifest = { round_id: fixture.public.round.round_id, items: answers };
  const blindManifest = { round_id: revealManifest.round_id, items: answers.map(answer => ({
    id: answer.id, week: '2026-08-08', choices: answer.choices.map(choice => ({ id: choice.id, confidence: { metric: 'ligand_plddt', value: 90 } })),
  })) };
  return { revealManifest, blindManifest,
    blindProjection: blindManifest,
    revealProjection: { ...revealManifest, items: answers.map(item => ({ ...item, ...projectReferenceDisposition(item), choices: item.choices.map(({ reference_sha256: _, ...choice }) => choice) })) },
    answerOverlayProjection: [],
  };
}

test('moving a valid disposition to another question is rejected against the bound reveal', () => {
  const fixture = mixed();
  const context = contextForMixed(fixture);
  const source = packaged(fixture);
  const verified = verifyPublicArtifact(source.pub, source.publication);
  assert.doesNotThrow(() => buildPublicDetail({ publication: source.publication, context, publicArtifact: verified }));
  // Keep the artifact self-consistent: swap complete question identities,
  // preserving each status/proof/score and the aggregate exclusion count.
  [verified.questions[0].item_id, verified.questions[1].item_id]
    = [verified.questions[1].item_id, verified.questions[0].item_id];
  assert.throws(() => buildPublicDetail({ publication: source.publication, context, publicArtifact: verified }), /disposition|population|status|reference|mismatch/i);
});

test('method all-time aggregation preserves exclusions beside its scored denominator', () => {
  const fixture = v6();
  const raw = fixture.reveal_manifest;
  const revealProjection = { ...raw, items: raw.items.map(item => ({ ...item, ...projectReferenceDisposition(item) })) };
  const result = buildPublishedMethodStats([{ blindProjection: fixture.blind_manifest, revealProjection }]);
  for (const row of result.weeks) assert.equal(row.excluded_targets, 1);
  for (const total of aggregateMethodStats(result)) {
    assert.equal(total.targets, 0);
    assert.equal(total.excluded_targets, 1);
    assert.equal(total.oracle_rate, null);
    assert.equal(total.top1_rate, null);
  }
  for (const value of [-1, 0.5, '1', null]) {
    const bad = structuredClone(result);
    bad.weeks[0].excluded_targets = value;
    assert.throws(() => validateMethodStats(bad), /invalid/i);
  }
});
