import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260808010900_deterministic_preview_round_selection.sql',
  import.meta.url,
);
const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('same-window weekly rounds select the most recently opened round deterministically', () => {
  assert.match(
    normalized,
    /create or replace function public\.get_current_weekly_quiz_round\(p_environment text\)/,
  );
  assert.match(
    normalized,
    /order by opens_at desc, opened_at desc, round_id desc limit 1/,
  );
  assert.match(
    normalized,
    /where environment = p_environment .* opens_at <= clock_timestamp\(\)/,
  );
});
