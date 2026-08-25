import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260825220000_add_weekly_vote_selection_provenance.sql',
  import.meta.url,
);
const rawSql = await readFile(migrationUrl, 'utf8');
const sql = rawSql.replace(/\s+/g, ' ').toLowerCase();

test('adds nullable typed provenance without rewriting historical attempts or app state', () => {
  assert.match(
    sql,
    /create type public\.weekly_quiz_selection_kind as enum \( 'cluster', 'exact', 'none' \)/,
  );
  assert.match(
    sql,
    /alter table public\.weekly_quiz_vote_attempts add column selection_kind public\.weekly_quiz_selection_kind, add column selection_id text/,
  );
  assert.match(
    sql,
    /alter table public\.weekly_quiz_votes add column selection_kind public\.weekly_quiz_selection_kind, add column selection_id text/,
  );
  assert.match(sql, /weekly_quiz_vote_attempts_selection_shape/);
  assert.match(sql, /selection_kind = 'exact' and selection_id = choice_id/);
  assert.match(sql, /selection_kind = 'cluster' and nullif\(selection_id, ''\) is not null/);
  assert.match(sql, /selection_kind = 'none' and selection_id is null/);
  assert.match(sql, /selection_source_attempt_id uuid/);
  assert.match(sql, /selection_revision bigint not null default 0/);
  assert.match(sql, /selection_source_metadata jsonb/);
  assert.doesNotMatch(sql, /update public\.weekly_quiz_vote_attempts/);
  assert.doesNotMatch(sql, /delete from public\.weekly_quiz_vote_attempts/);
  assert.doesNotMatch(sql, /update\s+\S+\s+set\s+app_state\s*=/);
});

test('v2 submission requires an explicit mode and validates exact and cluster IDs', () => {
  assert.match(sql, /create or replace function public\.submit_weekly_quiz_vote_attempt_v2/);
  assert.match(sql, /p_selection_kind public\.weekly_quiz_selection_kind/);
  assert.match(sql, /v2 weekly votes require cluster, exact, or none provenance/);
  assert.match(sql, /create or replace function private\.weekly_quiz_selection_matches_manifest/);
  assert.match(sql, /p_selection_kind = 'exact'[\s\S]*?p_selection_id = p_choice_id/);
  assert.match(
    sql,
    /p_selection_kind = 'cluster'[\s\S]*?choice\.value ->> 'cluster_id' = p_selection_id/,
  );
  assert.match(sql, /p_selection_kind = 'none'[\s\S]*?p_picked_none/);
  assert.match(sql, /selection provenance does not reference the immutable blind manifest/);
  assert.match(sql, /before update of blind_manifest, blind_manifest_sha256, item_count/);
  assert.match(sql, /v_user_id := auth\.uid\(\)/);
  assert.doesNotMatch(sql, /request\.jwt\.claim\.sub/);
  assert.match(sql, /insert into public\.weekly_quiz_vote_attempts/);
  assert.doesNotMatch(
    sql,
    /on conflict[^;]+weekly_quiz_vote_attempts[^;]+do update/,
  );
});

test('legacy RPCs preserve compatibility but clear unvalidated projection provenance', () => {
  assert.match(
    sql,
    /create or replace function public\.submit_weekly_quiz_vote_attempt\([\s\S]*?p_vote_comment text/,
  );
  assert.match(
    sql,
    /null::public\.weekly_quiz_selection_kind, null, false/,
  );
  assert.match(sql, /create or replace function public\.submit_weekly_quiz_vote\(/);
  for (const assignment of [
    'selection_kind = null',
    'selection_id = null',
    'selection_source_attempt_id = null',
    'selection_source = null',
    'selection_source_metadata = null',
    'selection_resolution_id = null',
  ]) {
    assert.ok(sql.includes(assignment), `missing legacy clear: ${assignment}`);
  }
  assert.match(
    sql,
    /selection_revision = public\.weekly_quiz_votes\.selection_revision \+ 1/,
  );
  assert.doesNotMatch(sql, /jsonb_set\s*\([^)]*app_state/);
  assert.doesNotMatch(sql, /app_state\s*=\s*app_state\s*\|\|/);
});

test('resolution ledger is append-only and records review evidence and immutable fingerprints', () => {
  assert.match(sql, /create table public\.weekly_quiz_vote_selection_resolutions/);
  for (const field of [
    'source_vote_attempt_id uuid not null',
    'vote_fingerprint_sha256 text not null',
    'evidence_sha256 text not null',
    'evidence_metadata jsonb not null',
    'actor text not null',
    'reviewer text not null',
    'reason text not null',
    'supersedes_resolution_id uuid',
  ]) {
    assert.ok(sql.includes(field), `missing resolution evidence field: ${field}`);
  }
  assert.match(sql, /unique \(supersedes_resolution_id\)/);
  assert.match(sql, /create trigger weekly_selection_resolutions_append_only/);
  assert.match(
    sql,
    /before update or delete on public\.weekly_quiz_vote_selection_resolutions/,
  );
  assert.match(sql, /weekly selection resolutions are append-only/);
  assert.match(sql, /create or replace function private\.weekly_quiz_vote_attempt_fingerprint/);
  assert.match(sql, /extensions\.digest/);
  assert.match(sql, /'app_state', v_attempt\.app_state/);
  assert.match(
    sql,
    /insert into public\.weekly_quiz_vote_selection_resolutions/,
  );
  assert.doesNotMatch(
    sql,
    /update public\.weekly_quiz_vote_selection_resolutions/,
  );
  assert.doesNotMatch(
    sql,
    /delete from public\.weekly_quiz_vote_selection_resolutions/,
  );
});

test('service resolution uses optimistic guards and append-only supersession', () => {
  assert.match(sql, /create or replace function public\.resolve_weekly_quiz_vote_selection/);
  assert.match(sql, /p_expected_selection_revision bigint/);
  assert.match(sql, /p_expected_vote_fingerprint_sha256 text/);
  assert.match(sql, /for update/);
  assert.match(sql, /weekly vote fingerprint changed or was not expected/);
  assert.match(sql, /weekly vote projection changed; refresh the resolution plan/);
  assert.match(
    sql,
    /v_vote\.selection_resolution_id is distinct from p_supersedes_resolution_id/,
  );
  assert.match(sql, /resulting_selection_revision = previous_selection_revision \+ 1/);
  assert.match(sql, /selection_source = 'resolution'/);
  assert.match(sql, /selection_resolution_id = p_resolution_id/);
});

test('read-only completeness check can gate retrospective switches', () => {
  assert.match(
    sql,
    /create or replace function public\.check_weekly_quiz_selection_provenance/,
  );
  assert.match(
    sql,
    /unresolved_votes bigint, inconsistent_votes bigint, ready boolean/,
  );
  assert.match(sql, /language plpgsql stable security definer/);
  assert.match(
    sql,
    /counts\.unresolved_votes = 0 and counts\.inconsistent_votes = 0/,
  );
  const checkStart = sql.indexOf(
    'create or replace function public.check_weekly_quiz_selection_provenance',
  );
  const checkEnd = sql.indexOf(
    'create or replace view public.replay_weekly_vote_attempts_safe',
  );
  const checkBody = sql.slice(checkStart, checkEnd);
  assert.doesNotMatch(checkBody, /\b(insert|update|delete|truncate)\b/);
});

test('RLS and least privilege expose browser v2 writes and service-only resolution', () => {
  assert.match(
    sql,
    /alter table public\.weekly_quiz_vote_attempts enable row level security/,
  );
  assert.match(
    sql,
    /alter table public\.weekly_quiz_votes enable row level security/,
  );
  assert.match(
    sql,
    /alter table public\.weekly_quiz_vote_selection_resolutions enable row level security/,
  );
  assert.match(
    sql,
    /revoke all on table public\.weekly_quiz_vote_selection_resolutions from authenticated/,
  );
  assert.match(
    sql,
    /grant execute on function public\.submit_weekly_quiz_vote_attempt_v2\([\s\S]*?\) to authenticated/,
  );
  assert.match(
    sql,
    /revoke all on function public\.resolve_weekly_quiz_vote_selection\([\s\S]*?\) from authenticated/,
  );
  assert.match(
    sql,
    /grant execute on function public\.resolve_weekly_quiz_vote_selection\([\s\S]*?\) to service_role/,
  );
  assert.match(
    sql,
    /grant execute on function public\.check_weekly_quiz_selection_provenance\(text\) to service_role/,
  );
  assert.match(
    sql,
    /revoke update, delete, truncate on table public\.weekly_quiz_vote_attempts from service_role/,
  );
  assert.match(
    sql,
    /revoke insert, update, delete, truncate on table public\.weekly_quiz_votes from service_role/,
  );
  assert.match(
    sql,
    /grant select on table public\.weekly_quiz_vote_selection_resolutions to service_role/,
  );
  assert.doesNotMatch(
    sql,
    /grant (?:all|select, insert, update, delete)[^;]*weekly_quiz_vote_selection_resolutions/,
  );
  assert.doesNotMatch(
    sql,
    /grant (?:all|select, insert, update, delete)[^;]*weekly_quiz_vote_attempts/,
  );
});

test('migration explicitly guards required ordering before replacing overloads', () => {
  assert.match(sql, /to_regclass\('public\.weekly_quiz_vote_attempts'\)/);
  assert.match(
    sql,
    /to_regprocedure\( 'public\.submit_weekly_quiz_vote_attempt\(uuid,uuid,text,text,integer,text,boolean,text,jsonb,jsonb,text\)' \)/,
  );
  assert.match(sql, /must run after the vote-comment and auth\.uid\(\) migrations/);
});
