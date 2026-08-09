import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260808010700_use_auth_uid_for_quiz_writes.sql',
  import.meta.url,
);

const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('repairs every browser-authenticated quiz write RPC with auth.uid()', () => {
  for (const signature of [
    'public.submit_weekly_quiz_vote(uuid,text,text,text,boolean)',
    'public.start_named_quiz_session(uuid,text,text,text)',
    'public.start_named_weekly_quiz_session(uuid,text,text,jsonb)',
    'public.complete_named_weekly_quiz_session(uuid)',
    'public.submit_weekly_quiz_vote_attempt(uuid,uuid,text,text,integer,text,boolean,jsonb,jsonb,text)',
    'public.submit_user_suggestion(uuid,text,text,uuid,uuid,text,text,jsonb,jsonb,jsonb)',
  ]) {
    assert.match(normalized, new RegExp(signature.replace(/[().]/g, '\\$&')));
  }
  assert.match(normalized, /'auth\.uid\(\)'/);
  assert.match(normalized, /expected legacy jwt identity expression/);
});

test('uses auth.uid() for the weekly vote read policy', () => {
  assert.match(normalized, /drop policy if exists "users select own weekly votes"/);
  assert.match(normalized, /using \(user_id = auth\.uid\(\)\)/);
});
