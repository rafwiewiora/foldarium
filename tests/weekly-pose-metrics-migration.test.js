import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260808010600_allow_weekly_pose_metrics.sql',
  import.meta.url,
);
const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('v3 weekly rounds allow only reviewed pose provenance during voting', () => {
  assert.match(normalized, /create or replace function public\.open_weekly_quiz_round/);
  assert.match(normalized, /confidence,metric.*ligand_plddt/);
  assert.match(normalized, /smina_score,metric.*smina_affinity/);
  assert.match(normalized, /interaction_count,metric.*prolif_unique_residue_interaction_type/);
  assert.match(normalized, /arithmetic-mean-selected-ligand-heavy-atoms/);
  assert.match(normalized, /smina_score,protocol.*score_only/);
});

test('v3 still rejects answers and private execution identities', () => {
  assert.match(
    normalized,
    /'correct', 'rmsd', 'answer', 'answer_metadata', 'score', 'run_id', 'sample_id', 'reference'/,
  );
  assert.doesNotMatch(
    normalized,
    /'score', 'method', 'method_version', 'run_id'/,
  );
  assert.match(normalized, /on conflict \(round_id\) do nothing/);
  assert.match(normalized, /weekly round identity is already bound to different content/);
});

test('weekly environments are partitioned while legacy callers stay on production', () => {
  assert.match(normalized, /add column if not exists environment text/);
  assert.match(normalized, /set environment = 'production' where environment is null/);
  assert.match(
    normalized,
    /check \(environment in \('production', 'preview', 'development'\)\)/,
  );
  assert.match(normalized, /p_environment text/);
  assert.match(normalized, /v_round\.environment <> p_environment/);
  assert.match(
    normalized,
    /select \* from public\.get_current_weekly_quiz_round\('production'\)/,
  );
  assert.match(
    normalized,
    /where environment = p_environment .* opens_at <= clock_timestamp\(\)/,
  );
});
