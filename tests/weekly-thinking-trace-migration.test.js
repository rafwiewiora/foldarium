import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260811192000_add_weekly_thinking_trace_batches.sql',
  import.meta.url,
);

test('weekly thinking-trace migration is append-only, bounded, and owner-authenticated', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

  assert.match(sql, /create table public\.weekly_quiz_trace_batches/);
  assert.match(sql, /trace_batch_id uuid primary key/);
  assert.match(sql, /unique \(session_id, visit_id, first_sequence, last_sequence\)/);
  assert.match(sql, /octet_length\(trace::text\) <= 491520/);
  assert.match(sql, /jsonb_array_length\(trace -> 'entries'\) between 1 and 500/);
  assert.match(sql, /trace ->> 'visit_id' = visit_id::text/);
  assert.match(sql, /create or replace function public\.append_weekly_quiz_trace_batch/);
  assert.match(sql, /v_user_id := auth\.uid\(\)/);
  assert.doesNotMatch(sql, /request\.jwt\.claim\.sub/);
  assert.match(sql, /v_session\.user_id <> v_user_id/);
  assert.match(sql, /item\.ordinal_position - 1 = p_question_index/);
  assert.match(sql, /trace batch identity is already bound to different content/);
  assert.match(sql, /weekly trace batch sequence binding is invalid/);
  assert.match(sql, /weekly-trace-batch:/);
  assert.match(sql, /insert into public\.weekly_quiz_trace_batches/);
  assert.doesNotMatch(sql, /update public\.weekly_quiz_trace_batches/);
  assert.doesNotMatch(sql, /on conflict[^;]+weekly_quiz_trace_batches[^;]+do update/);
});

test('only authenticated RPC and server-safe replay access are exposed', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
  const signature = 'uuid, uuid, text, text, integer, uuid, integer, integer, text, jsonb, jsonb';

  assert.match(sql, /alter table public\.weekly_quiz_trace_batches enable row level security/);
  assert.match(sql, /revoke all on table public\.weekly_quiz_trace_batches from authenticated/);
  assert.ok(sql.includes(`grant execute on function public.append_weekly_quiz_trace_batch( ${signature} ) to authenticated`));
  assert.match(sql, /create view public\.replay_weekly_trace_batches_safe/);
  assert.match(sql, /session\.participant_hash/);
  assert.match(sql, /session\.display_name_hash/);
  assert.doesNotMatch(
    sql.slice(sql.indexOf('create view public.replay_weekly_trace_batches_safe'), sql.indexOf('alter table public.weekly_quiz_trace_batches')),
    /session\.display_name[,\s]/,
  );
  assert.match(sql, /grant select on table public\.replay_weekly_trace_batches_safe to service_role/);
  assert.match(sql, /revoke insert, update, delete, truncate on table public\.weekly_quiz_trace_batches from service_role/);
});
