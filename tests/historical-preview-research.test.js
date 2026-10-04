import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { createHistoricalPreviewResearchHandler } from '../api/historical-preview-research.js';
import { verifyHistoricalArtifact } from '../lib/historical-preview-research.js';
const seed = JSON.parse(fs.readFileSync(new URL('./fixtures/historical-preview-research.json', import.meta.url)));
function fixture() {
  const f = structuredClone(seed), bytes = Buffer.from(JSON.stringify(f.public)), sha = createHash('sha256').update(bytes).digest('hex');
  const publication = { round_id: f.scope.round_id, scope_id: f.scope.scope_id, evaluation_id: f.descriptor.evaluation_id,
    public_artifact_sha256: sha, public_artifact_size_bytes: bytes.length, public_artifact_object_uri: `supabase://prediction-results/sha256/${sha.slice(0, 2)}/${sha}`,
    item_count: 1, choice_count: 2, required_execution_ids: [], published_at: '2026-10-04T20:00:00Z' };
  const summary = { ...f.scope, ...publication, publication_scope: 'historical-preview-research', human_votes_included: false };
  const catalog = { scope: f.scope, evaluation: f.descriptor, publication };
  return { artifact: f.public, bytes, summary, catalog };
}
function rehash(f) {
  f.bytes = Buffer.from(JSON.stringify(f.artifact)); const sha = createHash('sha256').update(f.bytes).digest('hex');
  for (const row of [f.summary, f.catalog.publication]) { row.public_artifact_sha256 = sha; row.public_artifact_size_bytes = f.bytes.length; }
  f.catalog.publication.public_artifact_object_uri = `supabase://prediction-results/sha256/${sha.slice(0, 2)}/${sha}`;
}
function response() { return { headers: {}, setHeader(k, v) { this.headers[k] = v; }, status(n) { this.code = n; return this; }, json(v) { this.value = v; return this; }, end() { return this; } }; }
const env = { SUPABASE_URL: 'https://example.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'sb_secret_fixture_only' };
function fetcher(f, calls = []) { return async (url, init) => {
  calls.push({ url, init });
  if (url.includes('/public_weekly_historical_preview_research?')) return Response.json([f.summary]);
  if (url.endsWith('/rpc/get_weekly_historical_preview_v1')) return Response.json(f.catalog);
  if (url.includes('/storage/')) return new Response(f.bytes);
  throw new Error('Unexpected request');
}; }
test('historical consumer preserves unscorable population and excludes human cohort', () => {
  const f = fixture(), a = verifyHistoricalArtifact(f.bytes, f.catalog, f.summary);
  assert.equal(a.source.environment, 'preview'); assert.equal(a.counts.scorable_item_count, 0);
  assert.equal(a.items[0].choices.length, 2); assert.equal(a.items[0].choices[0].rmsd, null);
  assert.equal(a.human_cohort.included, false); assert.equal(a.human_cohort.denominator, null);
});
test('unknown private fields, altered metrics, source windows and cohort changes fail closed even with fresh hash', () => {
  for (const change of [f => { f.artifact.items[0].user_id = 'private'; }, f => { f.artifact.items[0].choices[0].correct = false; }, f => { f.artifact.source.opens_at = '2026-08-15T00:00:00Z'; }, f => { f.artifact.human_cohort.included = true; }, f => { f.artifact.counts.scorable_item_count = 1; }, f => { f.artifact.items[0].reference_disposition.reference_coverage = .5; }]) {
    const f = fixture(); change(f); rehash(f); assert.throws(() => verifyHistoricalArtifact(f.bytes, f.catalog, f.summary));
  }
});
test('wrong bytes and external Storage locations fail closed', () => {
  const f = fixture(); assert.throws(() => verifyHistoricalArtifact(Buffer.from('{}'), f.catalog, f.summary));
  f.catalog.publication.public_artifact_object_uri = 'https://attacker.example/private';
  assert.throws(() => verifyHistoricalArtifact(f.bytes, f.catalog, f.summary));
});
test('API lists safe summaries without private catalog or downloads', async () => {
  const f = fixture(), calls = [], res = response();
  await createHistoricalPreviewResearchHandler({ env, fetchImpl: fetcher(f, calls) })({ method: 'GET', query: {} }, res);
  assert.equal(res.code, 200); assert.equal(calls.length, 1); assert.equal(res.value.items[0].human_votes_included, false);
  assert.equal(JSON.stringify(res.value).includes('supabase://'), false); assert.equal('evaluation_id' in res.value.items[0], false);
});
test('API reads only registered content address and returns verified research projection', async () => {
  const f = fixture(), calls = [], res = response();
  await createHistoricalPreviewResearchHandler({ env, fetchImpl: fetcher(f, calls) })({ method: 'GET', query: { round_id: f.scope?.round_id || f.summary.round_id } }, res);
  assert.equal(res.code, 200); assert.equal(calls.length, 3); assert.equal(res.value.human_cohort.included, false);
  assert.equal(JSON.stringify(res.value).includes('sb_secret'), false); assert.equal(JSON.stringify(res.value).includes('supabase://'), false);
  assert.ok(calls[2].url.startsWith('https://example.supabase.co/storage/v1/object/authenticated/prediction-results/sha256/'));
});
test('API never reads unpublished catalog or echoes private upstream errors', async () => {
  const calls = [], res = response();
  await createHistoricalPreviewResearchHandler({ env, fetchImpl: async (...args) => { calls.push(args); return Response.json([]); } })({ method: 'GET', query: { round_id: 'unpublished' } }, res);
  assert.equal(res.code, 404); assert.equal(calls.length, 1); assert.deepEqual(res.value, { error: 'Not found' });
});
test('API rejects mutation and injected filters before any upstream call', async () => {
  const calls = [], handler = createHistoricalPreviewResearchHandler({ env, fetchImpl: async () => { calls.push(1); throw Error(); } });
  for (const req of [{ method: 'POST', query: {} }, { method: 'GET', query: { round_id: 'x,or(secret)' } }, { method: 'GET', query: { admin: '1' } }]) { const res = response(); await handler(req, res); assert.ok([400, 405].includes(res.code)); }
  assert.equal(calls.length, 0);
});
test('verified model decisions keep null scores and reject fabricated None rewards', () => {
  const f = fixture(), executionId = '00000000-0000-4000-8000-000000000003';
  f.summary.required_execution_ids = [executionId]; f.catalog.publication.required_execution_ids = [executionId];
  f.artifact.benchmarks = [{ execution_id: executionId, driver: 'cursor', model_id: 'exact-model', config_sha256: 'a'.repeat(64), execution_sha256: 'b'.repeat(64), payload_digest: 'c'.repeat(64), decisions: [{ item_id: f.artifact.items[0].id, clustered: { selection_kind: 'none' }, unclustered: { selection_kind: 'none' }, clustered_correct: null, unclustered_correct: null }] }];
  rehash(f); assert.equal(verifyHistoricalArtifact(f.bytes, f.catalog, f.summary).benchmarks[0].decisions[0].unclustered_correct, null);
  f.artifact.benchmarks[0].decisions[0].unclustered_correct = true; rehash(f);
  assert.throws(() => verifyHistoricalArtifact(f.bytes, f.catalog, f.summary));
});
test('oversized body and corrupt artifact are never served or cached', async () => {
  const f = fixture(), standard = fetcher(f);
  for (const bad of [() => new Response('{}'), () => new Response('{}', { headers: { 'Content-Length': String(17 * 1024 * 1024) } })]) {
    const res = response(); await createHistoricalPreviewResearchHandler({ env, fetchImpl: (url, init) => url.includes('/storage/') ? bad() : standard(url, init) })({ method: 'GET', query: { round_id: f.summary.round_id }, headers: { 'if-none-match': `"historical-preview-v1-${f.summary.public_artifact_sha256}"` } }, res);
    assert.equal(res.code, 404); assert.equal(res.headers['Cache-Control'], 'no-store');
  }
});
test('exact model accuracy uses raw correctness even when its cluster is accepted', () => {
  const f = fixture(), item = f.artifact.items[0], executionId = '00000000-0000-4000-8000-000000000003';
  item.evaluation_status = 'scored'; delete item.reference_disposition;
  item.choices.forEach((c, index) => { c.rmsd = index ? .8 : 2; c.correct = Boolean(index); c.accepted_correct = true; });
  delete f.artifact.counts.scorable_item_count; delete f.artifact.counts.excluded_item_count;
  delete f.catalog.evaluation.scorable_item_count; delete f.catalog.evaluation.excluded_item_count;
  f.summary.required_execution_ids = [executionId]; f.catalog.publication.required_execution_ids = [executionId];
  f.artifact.benchmarks = [{ execution_id: executionId, driver: 'cursor', model_id: 'exact-model', config_sha256: 'a'.repeat(64), execution_sha256: 'b'.repeat(64), payload_digest: 'c'.repeat(64), decisions: [{ item_id: item.id, clustered: { selection_kind: 'cluster', cluster_id: item.choices[0].cluster_id }, unclustered: { selection_kind: 'exact', choice_id: item.choices[0].id }, clustered_correct: true, unclustered_correct: false }] }];
  rehash(f); assert.equal(verifyHistoricalArtifact(f.bytes, f.catalog, f.summary).benchmarks[0].decisions[0].unclustered_correct, false);
  f.artifact.benchmarks[0].decisions[0].unclustered_correct = true; rehash(f);
  assert.throws(() => verifyHistoricalArtifact(f.bytes, f.catalog, f.summary));
});
