import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const sql = await readFile(new URL(
  '../supabase/migrations/20260827032000_add_weekly_post_reveal_votes.sql',
  import.meta.url,
), 'utf8');

test('post-reveal votes are physically separated and server annotated', () => {
  assert.match(sql, /create table public\.weekly_quiz_post_reveal_sessions/);
  assert.match(sql, /create table public\.weekly_quiz_post_reveal_vote_attempts/);
  assert.match(sql, /submission_phase text not null default 'post_reveal'/);
  assert.match(sql, /check \(submission_phase = 'post_reveal'\)/);
  assert.doesNotMatch(sql, /insert into public\.weekly_quiz_votes/);
});

test('post-reveal writes require a revealed round and explicit selection provenance', () => {
  assert.match(sql, /v_round\.status <> 'revealed'/);
  assert.match(sql, /v_round\.reveal_manifest is null/);
  assert.match(sql, /v_round\.revealed_at is null/);
  assert.match(sql, /selection_kind' not in \('cluster', 'exact', 'none'\)/);
  assert.match(sql, /post-reveal selection provenance is inconsistent/);
});

test('post-reveal data stays private behind owner-bound RPCs', () => {
  assert.match(sql, /attempt\.user_id = auth\.uid\(\)/);
  assert.match(sql, /enable row level security/);
  assert.match(sql, /grant execute on function public\.get_my_weekly_post_reveal_votes/);
  assert.match(sql, /replay_weekly_post_reveal_vote_attempts_safe/);
  assert.match(sql, /grant select on table public\.replay_weekly_post_reveal_vote_attempts_safe[\s\S]*to service_role/);
});
