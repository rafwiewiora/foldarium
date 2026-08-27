import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const sql = await readFile(new URL(
  '../supabase/migrations/20260827034500_harden_weekly_post_reveal_votes.sql',
  import.meta.url,
), 'utf8');

test('post-reveal public writes lock identity before delegated idempotency lookup', () => {
  assert.match(sql, /weekly-post-reveal-session-id:' \|\| p_session_id::text/);
  assert.match(sql, /weekly-post-reveal-vote-id:' \|\| p_vote_attempt_id::text/);
  assert.match(sql, /set schema private/);
  assert.match(sql, /return private\.start_named_weekly_post_reveal_session_unlocked_v1/);
  assert.match(sql, /return private\.submit_weekly_post_reveal_vote_attempt_unlocked_v1/);
  assert.ok(
    sql.indexOf("weekly-post-reveal-session-id:' || p_session_id::text")
      < sql.indexOf('return private.start_named_weekly_post_reveal_session_unlocked_v1'),
  );
  assert.ok(
    sql.indexOf("weekly-post-reveal-vote-id:' || p_vote_attempt_id::text")
      < sql.indexOf('return private.submit_weekly_post_reveal_vote_attempt_unlocked_v1'),
  );
});

test('post-reveal safe replay omits plaintext participant names', () => {
  const viewStart = sql.indexOf(
    'create view public.replay_weekly_post_reveal_vote_attempts_safe',
  );
  const viewEnd = sql.indexOf(
    'revoke all on function private.start_named_weekly_post_reveal_session_unlocked_v1',
  );
  assert.ok(viewStart >= 0 && viewEnd > viewStart);
  const view = sql.slice(viewStart, viewEnd);
  assert.match(view, /session\.participant_hash, session\.display_name_hash/);
  assert.doesNotMatch(view, /session\.display_name(?:[,\s])/);
});

test('service replay access cannot mutate post-reveal base tables', () => {
  assert.match(sql, /revoke insert, update, delete, truncate\s+on table public\.weekly_quiz_post_reveal_sessions from service_role/);
  assert.match(sql, /revoke insert, update, delete, truncate\s+on table public\.weekly_quiz_post_reveal_vote_attempts from service_role/);
  assert.match(sql, /grant select on table public\.replay_weekly_post_reveal_vote_attempts_safe\s+to service_role/);
  assert.match(sql, /revoke all on function private\.start_named_weekly_post_reveal_session_unlocked_v1[\s\S]*from service_role/);
  assert.match(sql, /revoke all on function private\.submit_weekly_post_reveal_vote_attempt_unlocked_v1[\s\S]*from service_role/);
});
