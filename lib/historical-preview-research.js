import { createHash } from 'node:crypto';

export const HISTORICAL_FORMAT = 'foldarium.historical-preview-research/v1';
export const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;
const SHA = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const forbidden = new Set(['user_id', 'session_id', 'participant_link', 'vote_id', 'trace', 'app_state', 'auth', 'object_uri', 'reference_uri', 'private_index', 'prompt', 'reasoning_trace']);
export class HistoricalResearchError extends Error {}
const fail = () => { throw new HistoricalResearchError('Historical research artifact is unavailable'); };
function object(value) { if (!value || Array.isArray(value) || typeof value !== 'object') fail(); return value; }
function exact(value, required, optional = []) {
  object(value);
  if (required.some(key => !(key in value)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) fail();
}
function text(value, maximum = 200) { if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\u0000-\u001f\u007f]/.test(value)) fail(); return value; }
function integer(value, minimum = 0) { if (!Number.isSafeInteger(value) || value < minimum) fail(); return value; }
function timestamp(value) { text(value); const n = Date.parse(value); if (!Number.isFinite(n) || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) fail(); return n; }
function digest(value) { if (!SHA.test(value)) fail(); return value; }
function equalSet(actual, expected) { if (new Set(actual).size !== actual.length || JSON.stringify([...actual].sort()) !== JSON.stringify([...expected].sort())) fail(); }
function safeTree(value) {
  if (Array.isArray(value)) value.forEach(safeTree);
  else if (value && typeof value === 'object') Object.entries(value).forEach(([key, child]) => { if (forbidden.has(key)) fail(); safeTree(child); });
  else if (typeof value === 'string' && (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.includes('://'))) fail();
}

export function historicalSummary(row) {
  object(row);
  if (row.environment !== 'preview' || row.publication_scope !== 'historical-preview-research' || row.human_votes_included !== false) fail();
  text(row.round_id); text(row.campaign_id); timestamp(row.opens_at); timestamp(row.closes_at); timestamp(row.published_at);
  if (timestamp(row.opens_at) >= timestamp(row.closes_at)) fail();
  digest(row.blind_manifest_sha256); digest(row.public_artifact_sha256);
  if (!UUID.test(row.scope_id) || !/^weekly_eval_[0-9a-f]{32}$/.test(row.evaluation_id)) fail();
  integer(row.item_count, 1); integer(row.choice_count, 1); integer(row.public_artifact_size_bytes, 1);
  if (row.public_artifact_size_bytes > MAX_ARTIFACT_BYTES || !Array.isArray(row.required_execution_ids) || row.required_execution_ids.some(id => !UUID.test(id))) fail();
  equalSet(row.required_execution_ids, row.required_execution_ids);
  return { round_id: row.round_id, campaign_id: row.campaign_id, environment: 'preview',
    opens_at: row.opens_at, closes_at: row.closes_at, published_at: row.published_at,
    item_count: row.item_count, choice_count: row.choice_count, benchmark_count: row.required_execution_ids.length,
    publication_scope: 'historical-preview-research', human_votes_included: false };
}

export function historicalArtifactLocation(catalog, summary) {
  historicalSummary(summary);
  const publication = object(catalog.publication), scope = object(catalog.scope), evaluation = object(catalog.evaluation);
  for (const key of ['round_id', 'scope_id', 'evaluation_id', 'public_artifact_sha256', 'public_artifact_size_bytes', 'item_count', 'choice_count']) if (publication[key] !== summary[key]) fail();
  equalSet(publication.required_execution_ids, summary.required_execution_ids);
  for (const key of ['round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'scope_id']) if (scope[key] !== summary[key]) fail();
  for (const key of ['opens_at', 'closes_at']) if (timestamp(scope[key]) !== timestamp(summary[key])) fail();
  if (evaluation.format_version !== 'foldarium.historical-preview-evaluation/v1' || evaluation.evaluation_id !== publication.evaluation_id) fail();
  for (const key of ['round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'private_index_sha256']) if (scope[key] !== evaluation[key]) fail();
  for (const key of ['opens_at', 'closes_at']) if (timestamp(scope[key]) !== timestamp(evaluation[`round_${key}`])) fail();
  const match = /^supabase:\/\/([a-z0-9-]+)\/sha256\/([0-9a-f]{2})\/([0-9a-f]{64})$/.exec(publication.public_artifact_object_uri);
  if (!match || match[1] === 'foldarium-weekly-quiz' || match[1] === 'weekly-public' || match[2] !== match[3].slice(0, 2) || match[3] !== summary.public_artifact_sha256) fail();
  return { bucket: match[1], objectPath: `sha256/${match[2]}/${match[3]}` };
}

function validateItem(item) {
  exact(item, ['id', 'target_id', 'choices', 'evaluation_status'], ['reference_disposition']);
  text(item.id); if (!/^[A-Z0-9]{4}$/.test(item.target_id) || !Array.isArray(item.choices) || !item.choices.length) fail();
  const unscorable = item.evaluation_status === 'unscorable';
  if (!unscorable && item.evaluation_status !== 'scored') fail();
  if (unscorable) {
    const d = item.reference_disposition;
    exact(d, ['policy', 'code', 'component_id', 'expected_heavy_atoms', 'observed_heavy_atoms', 'explicitly_unobserved_heavy_atoms', 'reference_coverage', 'minimum_reference_coverage', 'reference_sha256']);
    if (d.policy !== 'foldarium.released-reference-disposition/v1' || d.code !== 'insufficient_reference_coverage' || !/^[A-Z0-9]{1,12}$/.test(d.component_id)) fail();
    integer(d.expected_heavy_atoms, 1); integer(d.observed_heavy_atoms, 1); integer(d.explicitly_unobserved_heavy_atoms, 1); digest(d.reference_sha256);
    if (d.explicitly_unobserved_heavy_atoms !== d.expected_heavy_atoms - d.observed_heavy_atoms || d.reference_coverage !== d.observed_heavy_atoms / d.expected_heavy_atoms || d.reference_coverage >= .8 || d.minimum_reference_coverage !== .8) fail();
  } else if ('reference_disposition' in item) fail();
  equalSet(item.choices.map(c => c.id), item.choices.map(c => c.id));
  for (const choice of item.choices) {
    exact(choice, ['id', 'method', 'method_version', 'rmsd', 'correct', 'accepted_correct', 'evaluator_version', 'cluster_id', 'reference_sha256'], ['smina_affinity_kcal_mol']);
    for (const key of ['id', 'method', 'method_version', 'evaluator_version']) text(choice[key]);
    digest(choice.reference_sha256); if (choice.cluster_id !== null) text(choice.cluster_id);
    if ('smina_affinity_kcal_mol' in choice && !Number.isFinite(choice.smina_affinity_kcal_mol)) fail();
    if (unscorable) {
      if (choice.reference_sha256 !== item.reference_disposition.reference_sha256 || choice.rmsd !== null || choice.correct !== null || choice.accepted_correct !== null) fail();
    } else if (!Number.isFinite(choice.rmsd) || choice.rmsd < 0 || typeof choice.correct !== 'boolean' || typeof choice.accepted_correct !== 'boolean') fail();
  }
  return unscorable;
}

export function verifyHistoricalArtifact(bytes, catalog, summary) {
  historicalArtifactLocation(catalog, summary);
  if (bytes.length !== summary.public_artifact_size_bytes || bytes.length > MAX_ARTIFACT_BYTES || createHash('sha256').update(bytes).digest('hex') !== summary.public_artifact_sha256) fail();
  let artifact; try { artifact = JSON.parse(Buffer.from(bytes).toString('utf8')); } catch { fail(); }
  exact(artifact, ['format_version', 'scope', 'human_cohort', 'source', 'counts', 'policy', 'items', 'benchmarks']);
  if (artifact.format_version !== HISTORICAL_FORMAT || artifact.scope !== 'historical-preview-research') fail();
  exact(artifact.human_cohort, ['included', 'denominator', 'reason']);
  if (artifact.human_cohort.included !== false || artifact.human_cohort.denominator !== null || artifact.human_cohort.reason !== 'no-human-votes-in-historical-research') fail();
  const source = artifact.source, scope = catalog.scope, evaluation = catalog.evaluation;
  exact(source, ['round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'private_index_sha256', 'opens_at', 'closes_at', 'scope_id', 'evaluation_id', 'evaluation_artifact_sha256', 'reveal_manifest_sha256', 'reference_set_sha256', 'prediction_set_sha256']);
  for (const key of ['round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'private_index_sha256', 'scope_id']) if (source[key] !== scope[key]) fail();
  for (const key of ['opens_at', 'closes_at']) if (timestamp(source[key]) !== timestamp(scope[key])) fail();
  for (const key of ['evaluation_id', 'reveal_manifest_sha256', 'reference_set_sha256', 'prediction_set_sha256']) if (source[key] !== evaluation[key]) fail();
  if (source.evaluation_artifact_sha256 !== evaluation.artifact_sha256) fail();
  for (const [key, value] of Object.entries(source)) if (key.endsWith('_sha256')) digest(value);
  exact(artifact.policy, ['reveal_policy_version', 'acceptance_policy_version', 'correct_rmsd_threshold_angstrom', 'evaluator_versions']);
  for (const key of Object.keys(artifact.policy)) if (JSON.stringify(artifact.policy[key]) !== JSON.stringify(evaluation[key])) fail();
  if (!Array.isArray(artifact.items) || !Array.isArray(artifact.benchmarks)) fail();
  equalSet(artifact.items.map(i => i.id), artifact.items.map(i => i.id));
  const excluded = artifact.items.filter(validateItem).length, choices = artifact.items.reduce((sum, item) => sum + item.choices.length, 0);
  exact(artifact.counts, ['item_count', 'choice_count'], ['scorable_item_count', 'excluded_item_count']);
  if (artifact.items.length !== summary.item_count || choices !== summary.choice_count || artifact.counts.item_count !== summary.item_count || artifact.counts.choice_count !== summary.choice_count) fail();
  if (excluded || 'excluded_item_count' in artifact.counts || 'scorable_item_count' in artifact.counts) {
    if (artifact.counts.excluded_item_count !== excluded || artifact.counts.scorable_item_count !== artifact.items.length - excluded || evaluation.excluded_item_count !== excluded || evaluation.scorable_item_count !== artifact.items.length - excluded) fail();
  }
  equalSet(artifact.benchmarks.map(b => b.execution_id), summary.required_execution_ids);
  const byItem = new Map(artifact.items.map(item => [item.id, item]));
  for (const benchmark of artifact.benchmarks) {
    exact(benchmark, ['execution_id', 'driver', 'model_id', 'config_sha256', 'execution_sha256', 'payload_digest', 'decisions']);
    text(benchmark.driver); text(benchmark.model_id); digest(benchmark.config_sha256); digest(benchmark.execution_sha256); digest(benchmark.payload_digest);
    if (!Array.isArray(benchmark.decisions)) fail();
    equalSet(benchmark.decisions.map(d => d.item_id), [...byItem.keys()]);
    for (const decision of benchmark.decisions) {
      exact(decision, ['item_id', 'clustered', 'unclustered', 'clustered_correct', 'unclustered_correct']);
      const item = byItem.get(decision.item_id);
      for (const [mode, key] of [['clustered', 'cluster_id'], ['unclustered', 'choice_id']]) {
        const selection = decision[mode];
        if (selection?.selection_kind === 'none') exact(selection, ['selection_kind']);
        else {
          exact(selection, ['selection_kind', key]);
          if (selection.selection_kind !== (mode === 'clustered' ? 'cluster' : 'exact') || !item.choices.some(c => c[mode === 'clustered' ? 'cluster_id' : 'id'] === selection[key])) fail();
        }
        const scoreKey = mode === 'clustered' ? 'accepted_correct' : 'correct';
        const expected = item.evaluation_status === 'unscorable' ? null : selection.selection_kind === 'none'
          ? !item.choices.some(c => c[scoreKey])
          : item.choices.some(c => c[mode === 'clustered' ? 'cluster_id' : 'id'] === selection[key] && c[scoreKey]);
        if (decision[`${mode}_correct`] !== expected) fail();
      }
    }
  }
  safeTree(artifact);
  return artifact;
}
