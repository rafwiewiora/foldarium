import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

test('viewer trace constraint rejects non-null rows when a required shape check is NULL', async () => {
  const sql = await readFile(
    new URL('../supabase/migrations/20260805230000_add_viewer_trace.sql', import.meta.url),
    'utf8',
  );
  const normalized = sql.replace(/\s+/g, ' ').trim();

  assert.match(
    normalized,
    /viewer_trace is null or \( jsonb_typeof\(viewer_trace\) = 'object' and viewer_trace ->> 'version' = '1' and jsonb_typeof\(viewer_trace -> 'snapshots'\) = 'array' \) is true/,
  );
});
