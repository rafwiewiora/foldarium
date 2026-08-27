import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260808010800_allow_weekly_hbond_metric.sql',
  import.meta.url,
);
const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('v4 allows an H-bond-only public metric without invalidating v3 rounds', () => {
  assert.match(normalized, /pg_get_functiondef/);
  assert.match(
    normalized,
    /not in \(''prolif_unique_residue_interaction_type'', ''prolif_hbond_residue_count''\)/,
  );
  assert.match(normalized, /if repaired_definition = definition then raise exception/);
  assert.match(normalized, /execute repaired_definition/);
});
