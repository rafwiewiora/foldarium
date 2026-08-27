import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const repairUrl = new URL(
  '../supabase/migrations/20260811193000_fix_weekly_trace_auth_uid.sql',
  import.meta.url,
);

test('weekly trace auth repair uses Supabase auth.uid without changing the RPC contract', async () => {
  const sql = (await readFile(repairUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

  assert.match(sql, /create or replace function public\.append_weekly_quiz_trace_batch/);
  assert.match(sql, /v_user_id := auth\.uid\(\)/);
  assert.doesNotMatch(sql, /request\.jwt\.claim\.sub/);
  assert.match(sql, /trace batch identity is already bound to different content/);
  assert.match(sql, /insert into public\.weekly_quiz_trace_batches/);
  assert.doesNotMatch(sql, /update public\.weekly_quiz_trace_batches/);
  assert.doesNotMatch(sql, /delete from public\.weekly_quiz_trace_batches/);
});
