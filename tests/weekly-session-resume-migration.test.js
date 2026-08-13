import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260812170000_resume_weekly_quiz_session.sql', import.meta.url,
);

test('weekly session refresh resume is owner-bound, round-bound, and name-free', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
  assert.match(sql, /create or replace function public\.resume_named_weekly_quiz_session/);
  assert.match(sql, /session\.user_id = v_user_id/);
  assert.match(sql, /session\.round_id = p_round_id/);
  assert.match(sql, /session\.completed_at is null/);
  assert.match(sql, /round\.status = 'open'/);
  assert.match(sql, /clock_timestamp\(\) >= round\.opens_at/);
  assert.match(sql, /clock_timestamp\(\) < round\.closes_at/);
  assert.match(sql, /returns table \( session_id uuid, round_id text, next_visit_ordinal bigint, last_visit_started_at bigint \)/);
  assert.doesNotMatch(sql, /returns table \([^)]*display_name/);
  assert.match(sql, /max\(\(batch\.trace ->> 'visit_ordinal'\)::bigint\)/);
  assert.match(sql, /max\(\(batch\.trace ->> 'visit_started_at'\)::bigint\)/);
  assert.match(sql, /grant execute on function public\.resume_named_weekly_quiz_session\(uuid, text\) to authenticated/);
  assert.match(sql, /revoke all on function public\.resume_named_weekly_quiz_session\(uuid, text\) from anon/);
  assert.doesNotMatch(sql, /insert into|update public|delete from/);
});
