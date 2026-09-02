import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260902040000_add_weekly_viewer_performance_reports.sql',
  import.meta.url,
);

test('performance reports are private, bounded, consented, and owner-authenticated', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();

  assert.match(sql, /create table private\.weekly_viewer_performance_reports/);
  assert.match(sql, /report_id uuid primary key/);
  assert.match(sql, /octet_length\(report::text\) <= 32768/);
  assert.match(sql, /foldarium\.viewer-performance-diagnostics\/v1/);
  assert.match(sql, /explicit-beta-checkbox/);
  assert.match(sql, /create or replace function public\.append_weekly_viewer_performance_report/);
  assert.match(sql, /v_user_id := auth\.uid\(\)/);
  assert.match(sql, /v_session\.user_id <> v_user_id/);
  assert.match(sql, /item\.ordinal_position - 1 = p_question_index/);
  assert.match(sql, /performance report identity is already bound to different content/);
  assert.match(sql, /weekly-viewer-performance:/);
  assert.match(sql, /insert into private\.weekly_viewer_performance_reports/);
  assert.doesNotMatch(sql, /update private\.weekly_viewer_performance_reports/);
  assert.match(sql, /too many viewer performance reports/);
  assert.ok(sql.includes('user[_-]?agent'));
  assert.match(sql, /plugins\?/);
});

test('telemetry has no replay view and exposes only its append RPC to clients', async () => {
  const sql = (await readFile(migrationUrl, 'utf8')).replace(/\s+/g, ' ').toLowerCase();
  const signature = 'uuid, uuid, text, text, integer, jsonb';

  assert.doesNotMatch(sql, /create (?:or replace )?view/);
  assert.doesNotMatch(sql, /replay_/);
  assert.match(sql, /revoke all on table private\.weekly_viewer_performance_reports from authenticated/);
  assert.ok(sql.includes(
    `grant execute on function public.append_weekly_viewer_performance_report( ${signature} ) to authenticated`,
  ));
  assert.match(sql, /grant select on table private\.weekly_viewer_performance_reports to service_role/);
  assert.match(
    sql,
    /revoke insert, update, delete, truncate on table private\.weekly_viewer_performance_reports from service_role/,
  );
});
