import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const sql = await readFile(new URL(
  '../supabase/migrations/20260909203000_add_exact_open_weekly_round_lookup.sql',
  import.meta.url,
), 'utf8');

test('exact Weekly lookup exposes only a currently votable blind round', () => {
  assert.match(sql, /returns setof public\.public_weekly_quiz_rounds/i);
  assert.match(sql, /round_id = p_round_id/i);
  assert.match(sql, /environment = p_environment/i);
  assert.match(sql, /public_status = 'open'/i);
  assert.match(sql, /opens_at <= clock_timestamp\(\)/i);
  assert.match(sql, /closes_at > clock_timestamp\(\)/i);
  assert.doesNotMatch(sql, /reveal_manifest|private_index|service_role/i);
});

test('exact Weekly lookup is read-only and available to browser roles', () => {
  assert.match(sql, /language sql[\s\S]*stable[\s\S]*security invoker/i);
  assert.match(sql, /grant execute[\s\S]*to anon/i);
  assert.match(sql, /grant execute[\s\S]*to authenticated/i);
  assert.doesNotMatch(sql, /\binsert\b|\bupdate\b|\bdelete\b/i);
});
