import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260826210000_add_weekly_selector_post_close_benchmarks.sql',
  import.meta.url,
);
const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
const rpcFixUrl = new URL(
  '../supabase/migrations/20260826232500_fix_weekly_selector_benchmark_rpc_conflict.sql',
  import.meta.url,
);
const rpcFixSql = (await readFile(rpcFixUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

test('benchmark migration is append-only and physically separate from ballots', () => {
  assert.match(
    sql,
    /create table if not exists public\.weekly_selector_post_close_benchmarks_v1/,
  );
  assert.match(sql, /run_class = 'post_close_benchmark'/);
  assert.match(sql, /weekly_selector_post_close_benchmarks_append_only/);
  assert.match(sql, /before update or delete/);
  assert.doesNotMatch(sql, /insert into public\.weekly_selector_submission_revisions_v2/);
  assert.doesNotMatch(sql, /insert into public\.weekly_quiz_votes/);
  assert.doesNotMatch(sql, /update public\.weekly_quiz/);
});

test('registration requires service role, closed-unrevealed state, and exact bindings', () => {
  assert.match(sql, /auth\.role\(\) is distinct from 'service_role'/);
  assert.match(
    sql,
    /select count\(\*\) from jsonb_object_keys\( case when jsonb_typeof\(p_execution\) = 'object' then p_execution else '\{\}'::jsonb end \) \) <> 23/,
  );
  assert.doesNotMatch(sql, /jsonb_object_length/);
  assert.match(sql, /clock_timestamp\(\) < v_round\.closes_at/);
  assert.match(sql, /v_round\.reveal_manifest is not null/);
  assert.match(sql, /v_round\.revealed_at is not null/);
  assert.match(sql, /selector benchmark round must be closed and unrevealed/);
  assert.match(sql, /weekly_selector_validate_complete_payload_v2/);
  assert.match(sql, /blind_manifest_sha256 <> v_round\.blind_manifest_sha256/);
  assert.match(sql, /kit_sha256 <> p_execution ->> 'kit_sha256'/);
  assert.match(
    sql,
    /prompt_profile_id' <> 'weekly-pose-selector-v1'/,
  );
  assert.match(
    sql,
    /prompt_sha256' <> 'e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85'/,
  );
  assert.match(sql, /weekly_selector_blindness_attestation_is_valid_v2/);
});

test('registration is canonical, content-idempotent, and revision-safe', () => {
  assert.match(sql, /weekly_selector_canonical_json\(p_execution\)/);
  assert.match(sql, /weekly_selector_canonical_json\(p_execution -> 'payload'\)/);
  assert.match(sql, /on conflict \(execution_id\) do nothing/);
  assert.match(sql, /execution id is already bound differently/);
  assert.match(sql, /supersedes_execution_id/);
  assert.match(sql, /selector benchmark supersession target is invalid/);
  assert.match(sql, /idempotent := v_inserted_count = 0/);
});

test('registration conflict target cannot collide with an output parameter', () => {
  assert.match(
    rpcFixSql,
    /on conflict on constraint weekly_selector_post_close_benchmarks_v1_pkey do nothing/,
  );
  assert.doesNotMatch(rpcFixSql, /on conflict \(execution_id\)/);
});

test('public projection is reveal-gated and strips runtime identifiers and usage', () => {
  assert.match(sql, /create or replace function public\.get_weekly_selector_benchmarks_v1/);
  assert.match(sql, /quiz_round\.status = 'revealed'/);
  assert.match(sql, /quiz_round\.reveal_manifest is not null/);
  assert.match(sql, /quiz_round\.revealed_at is not null/);
  const projection = sql.slice(
    sql.indexOf('create or replace function public.get_weekly_selector_benchmarks_v1'),
    sql.indexOf('alter table public.weekly_selector_post_close_benchmarks_v1'),
  );
  assert.doesNotMatch(projection, /session_id|run_id|usage|cost_usd|output_sha256/);
  assert.match(sql, /grant execute on function public\.get_weekly_selector_benchmarks_v1/);
  assert.doesNotMatch(
    sql,
    /grant (?:select|insert|update|delete)[^;]+weekly_selector_post_close_benchmarks_v1 to anon/,
  );
});

const retrospectiveMigrationUrl = new URL(
  '../supabase/migrations/20260826233000_add_retrospective_post_close_benchmarks.sql',
  import.meta.url,
);
const retrospectiveSql = (await readFile(retrospectiveMigrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

test('retrospective source migration filters superseded benchmark executions', () => {
  assert.match(retrospectiveSql, /successor\.supersedes_execution_id = benchmark\.execution_id/);
  assert.match(retrospectiveSql, /item\.value -> 'unclustered' ->> 'selection_kind'/);
  assert.match(retrospectiveSql, /'gpt-5\.6 sol'/);
  assert.match(
    retrospectiveSql,
    /lock table public\.weekly_selector_post_close_benchmarks_v1 in share mode/,
  );
});

test('retrospective benchmark migration keeps active-only projection', () => {
  const projection = retrospectiveSql.slice(
    retrospectiveSql.indexOf('create or replace function public.get_weekly_selector_benchmarks_v1'),
    retrospectiveSql.indexOf('create or replace function public.register_weekly_retrospective_publication'),
  );
  assert.match(projection, /successor\.supersedes_execution_id = benchmark\.execution_id/);
});
