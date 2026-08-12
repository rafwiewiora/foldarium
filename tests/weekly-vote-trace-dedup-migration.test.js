import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260812010000_deduplicate_weekly_vote_traces.sql', import.meta.url,
);

test('weekly vote revisions preserve compact comments and nullable legacy replay traces', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
  assert.match(sql, /add column vote_comment text/);
  assert.match(sql, /char_length\(vote_comment\) between 1 and 4000/);
  assert.match(sql, /octet_length\(vote_comment\) <= 16000/);
  assert.match(sql, /v_attempt\.vote_comment is distinct from p_vote_comment/);
  assert.doesNotMatch(sql, /drop function public\.submit_weekly_quiz_vote_attempt/);
  assert.match(sql, /uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text/);
  assert.match(sql, /insert into public\.weekly_quiz_vote_attempts/);
  assert.match(sql, /on conflict \(round_id, user_id, item_id\) do update/);
  assert.match(sql, /vote\.viewer_trace, vote\.app_state, vote\.active_pane_id, vote\.vote_comment/);
  assert.doesNotMatch(sql, /update public\.weekly_quiz_vote_attempts/);
  assert.doesNotMatch(sql, /delete from public\.weekly_quiz_vote_attempts/);
});

test('v2 continuous batches are server-validated as contiguous with replay metadata', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
  assert.match(sql, /create or replace function public\.validate_weekly_trace_stream_v2/);
  assert.match(sql, /stream_schema_version/);
  assert.match(sql, /molstar_version/);
  assert.match(sql, /visit_started_at/);
  assert.match(sql, /visit_ordinal/);
  assert.match(sql, /new\.first_sequence \+ entry\.ordinal_position - 1/);
  assert.match(sql, /before insert on public\.weekly_quiz_trace_batches/);
  assert.match(sql, /regexp_replace\(lower\(app_key\.value\), '\[\^a-z0-9\]', '', 'g'\) = 'votecomment'/);
  assert.match(sql, /revoke all on function public\.validate_weekly_trace_stream_v2\(\) from public, anon, authenticated/);
});
