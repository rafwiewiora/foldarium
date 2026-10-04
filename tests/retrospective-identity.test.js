import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { verifySourceSnapshot, verifyPublicArtifact, verifyAdminArtifact, assertResponseSafe } from '../lib/weekly-retrospectives.js';

const load = name => JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url)));
const source = () => load('retrospective-source-v2-authorized.golden.json');
const artifacts = () => load('retrospective-authorized-model.golden.json');
const encoded = value => Buffer.from(JSON.stringify(value));
const descriptor = bytes => ({ sha256: createHash('sha256').update(bytes).digest('hex'), sizeBytes: bytes.length, mediaType: 'application/json' });
function fixture(snapshot = source(), archive = artifacts()) {
  const bytes = encoded(snapshot), publicBytes = encoded(archive.public), adminBytes = encoded(archive.admin);
  const round = archive.public.round;
  return { bytes, publicBytes, adminBytes, publication: {
    roundId: round.round_id, campaignId: round.campaign_id,
    opensAt: round.opens_at, closesAt: round.closes_at, revealedAt: round.revealed_at,
    itemCount: round.item_count, choiceCount: round.choice_count,
    evaluationFormatVersion: 'foldarium.weekly-private-evaluation/v5',
    digests: { blind_manifest_sha256: 'a'.repeat(64) },
    descriptors: { source_snapshot: descriptor(bytes), public_artifact: descriptor(publicBytes), admin_artifact: descriptor(adminBytes) },
  } };
}

test('Python source/v2 authorizes exact model labels only within its publication', () => {
  const f = fixture();
  assert.throws(() => verifyPublicArtifact(f.publicBytes, f.publication), /identity/i);
  assert.throws(() => verifyAdminArtifact(f.adminBytes, f.publication), /identity/i);
  const snapshot = verifySourceSnapshot(f.bytes, f.publication);
  assert.ok(snapshot.authorizedLlmIdentities.has('fixture-model'));
  const publicArtifact = verifyPublicArtifact(f.publicBytes, f.publication, snapshot);
  const adminArtifact = verifyAdminArtifact(f.adminBytes, f.publication, snapshot);
  assert.ok(publicArtifact.automated_entries.some(row => row.participant === 'fixture-model'));
  assert.ok(adminArtifact.participants.some(row => row.participant === 'fixture-model'));
  assertResponseSafe(publicArtifact); assertResponseSafe(adminArtifact);
  assert.doesNotMatch(JSON.stringify([publicArtifact, adminArtifact]), /benchmark_authorization|execution_id|config_sha256|payload_digest/);
  assert.throws(() => verifyPublicArtifact(f.publicBytes, { ...f.publication, roundId: 'another-round' }, snapshot), /bound to this publication/);
  assert.throws(() => verifyPublicArtifact(f.publicBytes, { ...f.publication, descriptors: { ...f.publication.descriptors, source_snapshot: { ...f.publication.descriptors.source_snapshot, sha256: 'b'.repeat(64) } } }, snapshot), /bound to this publication/);
  // Verifying an authorized source never changes the default global allowlist.
  assert.throws(() => verifyPublicArtifact(f.publicBytes, f.publication), /identity/i);
});

test('forged and mutated authorization sets cannot widen authority', () => {
  const f = fixture();
  assert.throws(() => verifyPublicArtifact(f.publicBytes, f.publication, { authorizedLlmIdentities: new Set(['fixture-model']) }), /authorization/i);
  const snapshot = verifySourceSnapshot(f.bytes, f.publication);
  snapshot.authorizedLlmIdentities.add('PocketFox Impostor');
  const archive = artifacts();
  for (const row of archive.public.automated_entries) if (row.participant === 'fixture-model') row.participant = 'PocketFox Impostor';
  const altered = fixture(source(), archive);
  assert.throws(() => verifyPublicArtifact(altered.publicBytes, altered.publication, snapshot), /identity/i);
});

test('source proof shape, execution, model, round, manifest, provider and version fail closed', () => {
  for (const mutate of [
    s => { s.format_version = 'foldarium.weekly-retrospective-source/v1'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.extra = 'unreviewed'; },
    s => { delete s.participants.find(p => p.benchmark_authorization).benchmark_authorization.artifact_sha256; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.provider = 'cursor'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.environment = 'preview'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.round_id = 'other'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.execution_id = '11111111-1111-4111-8111-111111111111'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.model_id = 'Human Name'; },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.blind_manifest_sha256 = '9'.repeat(64); },
    s => { s.participants.find(p => p.benchmark_authorization).benchmark_authorization.payload_digest = 'invalid'; },
    s => { s.participants.find(p => p.benchmark_authorization).current_session_count = 1; },
    s => { s.participants.find(p => p.benchmark_authorization).participant_kind = 'human'; },
    s => { s.participants.find(p => p.participant_kind === 'human').benchmark_authorization = s.participants.find(p => p.benchmark_authorization).benchmark_authorization; },
  ]) {
    const input = source(); mutate(input); const f = fixture(input);
    assert.throws(() => verifySourceSnapshot(f.bytes, f.publication));
  }
  const f = fixture();
  const tampered = Buffer.from(f.bytes); tampered[20] ^= 1;
  assert.throws(() => verifySourceSnapshot(tampered, f.publication), /descriptor/i);
});

test('legacy source/v1 retains legacy-only labels and rejects a v2 marker without proof', () => {
  const s = source(); const api = s.participants.find(p => p.benchmark_authorization);
  s.participants = s.participants.filter(p => p !== api);
  s.votes = s.votes.filter(v => v.participant_link !== api.participant_link);
  s.format_version = 'foldarium.weekly-retrospective-source/v1';
  const f = fixture(s); const verified = verifySourceSnapshot(f.bytes, f.publication);
  assert.equal(verified.participants.length, 2);
  assert.equal(verified.authorizedLlmIdentities.has('fixture-model'), false);
  s.format_version = 'foldarium.weekly-retrospective-source/v2';
  const wrong = fixture(s);
  assert.throws(() => verifySourceSnapshot(wrong.bytes, wrong.publication), /version/i);
});
