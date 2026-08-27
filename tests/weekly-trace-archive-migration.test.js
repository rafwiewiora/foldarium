import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migration = new URL(
  '../supabase/migrations/20260812160000_add_weekly_trace_archive_catalog.sql',
  import.meta.url,
);

test('cold archive catalog is private, exact, and has no destructive operation', async () => {
  const sql = (await readFile(migration, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

  assert.match(sql, /create table private\.weekly_trace_archive_jobs/);
  assert.match(sql, /create table private\.weekly_trace_archives/);
  assert.match(sql, /create table private\.weekly_trace_archive_members/);
  assert.match(sql, /create table private\.weekly_trace_archive_visits/);
  assert.match(sql, /membership_sha256/);
  assert.match(sql, /sequence_gaps integer not null default 0 check \(sequence_gaps = 0\)/);
  assert.match(sql, /primary key \(archive_id, source_kind, source_id\)/);
  assert.match(sql, /archive_record_ordinal/);
  assert.match(sql, /visit_ordinal/);
  assert.match(sql, /accounted_omitted_sequence_count/);
  assert.match(
    sql,
    /add constraint weekly_quiz_sessions_session_id_round_id_key unique \(session_id, round_id\)/,
  );
  assert.match(
    sql,
    /foreign key \(session_id, round_id\) references public\.weekly_quiz_sessions\(session_id, round_id\)/,
  );
  assert.match(sql, /revoke all on table private\.weekly_trace_archives from authenticated/);
  assert.match(sql, /vote metadata and comments remain hot/);
  assert.doesNotMatch(sql, /create (or replace )?function/);
  assert.doesNotMatch(sql, /delete from/);
  assert.doesNotMatch(sql, /truncate table/);
  assert.doesNotMatch(sql, /storage\.objects/);
});
