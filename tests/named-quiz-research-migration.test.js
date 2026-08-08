import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260808010500_add_named_quiz_research_events.sql',
  import.meta.url,
);

const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('generates private HMAC key material in the database', () => {
  assert.match(normalized, /create table if not exists private\.foldarium_secrets/);
  assert.match(normalized, /extensions\.gen_random_bytes\(32\)/);
  assert.match(normalized, /extensions\.hmac\(/);
  assert.match(
    normalized,
    /'display-name', v_user_id::text \|\| ':' \|\| lower\(v_display_name\)/,
  );
  assert.match(normalized, /revoke all on schema private from public/);
  assert.match(normalized, /revoke all on schema private from service_role/);
  assert.doesNotMatch(normalized, /values \('participant-hmac-v1', '\\x[0-9a-f]+'/);
});

test('keeps legacy sessions possible while protecting server-derived identity fields', () => {
  assert.match(normalized, /identity_version smallint not null default 0/);
  assert.match(normalized, /identity_version = 0 or \( display_name is not null/);
  assert.match(normalized, /revoke insert on table public\.quiz_sessions from authenticated/);
  assert.match(
    normalized,
    /grant insert \(id, user_id, source, difficulty, started_at, completed_at\) on table public\.quiz_sessions to authenticated/,
  );
  assert.match(normalized, /create or replace function public\.start_named_quiz_session/);
  assert.match(normalized, /security definer set search_path = pg_catalog/);
});

test('stores bounded append-only weekly vote events and maintains the legacy vote projection', () => {
  assert.match(normalized, /create table public\.weekly_quiz_vote_attempts/);
  assert.match(normalized, /question_index integer not null check \(question_index >= 0\)/);
  assert.match(normalized, /item\.ordinal_position - 1 = p_question_index/);
  assert.match(normalized, /octet_length\(viewer_trace::text\) <= 524288/);
  assert.match(normalized, /octet_length\(app_state::text\) <= 65536/);
  assert.match(normalized, /create or replace function public\.submit_weekly_quiz_vote_attempt/);
  assert.match(
    normalized,
    /grant execute on function public\.submit_weekly_quiz_vote_attempt\( uuid, uuid, text, text, integer, text, boolean, jsonb, jsonb, text \) to authenticated/,
  );
  assert.match(
    normalized,
    /insert into public\.weekly_quiz_vote_attempts[\s\S]*insert into public\.weekly_quiz_votes/,
  );
  assert.doesNotMatch(
    normalized,
    /update public\.weekly_quiz_vote_attempts|on conflict[^;]+weekly_quiz_vote_attempts[^;]+do update/,
  );
});

test('accepts feedback only through a rate-limited named-session RPC', () => {
  assert.match(normalized, /create table public\.user_suggestions/);
  assert.match(normalized, /num_nonnulls\(quiz_session_id, weekly_session_id\) = 1/);
  assert.match(normalized, /suggestions require an owned named quiz session/);
  assert.match(normalized, /suggestion item identity must match the captured app state/);
  assert.match(normalized, /suggestion item is not part of the named weekly round/);
  assert.match(normalized, /interval '1 hour'[\s\S]*>= 5/);
  assert.match(normalized, /interval '1 day'[\s\S]*>= 20/);
  assert.match(normalized, /revoke all on table public\.user_suggestions from authenticated/);
  assert.match(
    normalized,
    /grant execute on function public\.submit_user_suggestion\( uuid, text, text, uuid, uuid, text, text, jsonb, jsonb, jsonb \) to authenticated/,
  );
});

test('enables RLS and provides server-only replay surfaces without raw identities', () => {
  for (const table of [
    'weekly_quiz_sessions',
    'weekly_quiz_vote_attempts',
    'user_suggestions',
  ]) {
    assert.match(normalized, new RegExp(`alter table public\\.${table} enable row level security`));
  }
  for (const view of [
    'replay_quiz_sessions_safe',
    'replay_weekly_sessions_safe',
    'replay_quiz_answers_safe',
    'replay_weekly_vote_attempts_safe',
    'replay_user_suggestions_safe',
  ]) {
    assert.match(normalized, new RegExp(`create or replace view public\\.${view}`));
    assert.match(normalized, new RegExp(`revoke all on table public\\.${view} from public`));
  }
  const replayViews = normalized.slice(
    normalized.indexOf('create or replace view public.replay_quiz_sessions_safe'),
    normalized.indexOf('alter table public.weekly_quiz_sessions enable row level security'),
  );
  for (const statement of replayViews.split(';').filter(Boolean)) {
    const selectList = statement.match(/\bas select\b([\s\S]*?)\bfrom\b/)?.[1] ?? '';
    assert.doesNotMatch(selectList, /\buser_id\b/);
    assert.doesNotMatch(selectList, /\bdisplay_name\b(?!_hash)/);
  }
});
