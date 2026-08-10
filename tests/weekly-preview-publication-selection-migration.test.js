import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const migrationUrl = new URL(
  '../supabase/migrations/20260810010000_prefer_latest_preview_publication.sql',
  import.meta.url,
);
const sql = await readFile(migrationUrl, 'utf8');
const normalized = sql.replace(/\s+/g, ' ').toLowerCase();

test('Preview selects the latest publication while other environments remain schedule-first', () => {
  assert.match(
    normalized,
    /create or replace function public\.get_current_weekly_quiz_round\(p_environment text\)/,
  );
  assert.match(
    normalized,
    /where environment = p_environment .* p_environment in \('production', 'preview', 'development'\) .* opens_at <= clock_timestamp\(\)/,
  );
  assert.match(
    normalized,
    /order by case when p_environment = 'preview' then opened_at end desc nulls last, opens_at desc, opened_at desc, round_id desc limit 1/,
  );
});

test('selector migration changes no weekly round rows or schema', () => {
  assert.doesNotMatch(normalized, /\b(insert|update|delete|alter|drop|truncate)\b/);
  assert.doesNotMatch(normalized, /get_current_weekly_quiz_round\(\)/);
});
