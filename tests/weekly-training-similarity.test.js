import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  WEEKLY_SIMILARITY_NOVELTY_THRESHOLD,
  WEEKLY_SIMILARITY_TRAINING_CUTOFF,
  fetchWeeklyTrainingSimilarityReport,
  parseContentAddressedSupabaseOverlay,
  parseWeeklyTrainingSimilarityReport,
  sortWeeklySimilarityRows,
  weeklySimilarityRecord,
} from '../weekly-training-similarity.js';

function record(overrides = {}) {
  return {
    week: '2026-08-22',
    item_id: '9ABC',
    ligand: 'LIG',
    classification: 'familiar',
    reason: 'training-ligand-overlap-at-least-0.25',
    train_pdb: '1ABC',
    train_het: 'LIG',
    train_identity: 0.8,
    train_align_rmsd: 0.4,
    train_shape_overlap: 0.5,
    nearest_score: 0.4,
    pocket_aware_score: 0.6,
    ...overrides,
  };
}

function report(records, overrides = {}) {
  return {
    format_version: 'foldarium.weekly-training-similarity-report/v1',
    training_cutoff: WEEKLY_SIMILARITY_TRAINING_CUTOFF,
    novelty_threshold: WEEKLY_SIMILARITY_NOVELTY_THRESHOLD,
    records,
    ...overrides,
  };
}

test('checked-in Weekly similarity report satisfies the browser contract', async () => {
  const raw = JSON.parse(await readFile(
    new URL('../docs/weekly-training-similarity-results.json', import.meta.url),
    'utf8',
  ));
  const parsed = parseWeeklyTrainingSimilarityReport(raw);
  assert.equal(parsed.records.length, 100);
  assert.equal(
    weeklySimilarityRecord(parsed, '2026-08-22', '9PXD')?.train_shape_overlap,
    0.7653,
  );
  assert.equal(weeklySimilarityRecord(parsed, '2026-08-22', 'missing'), null);
});

test('report fetch rejects unavailable and invalid payloads for fail-open UI fallback', async () => {
  await assert.rejects(
    fetchWeeklyTrainingSimilarityReport('/report.json', async () => new Response('missing', {
      status: 404,
    })),
    /unavailable/,
  );
  await assert.rejects(
    fetchWeeklyTrainingSimilarityReport('/report.json', async () => Response.json({})),
    /format_version/,
  );
});

test('report parser pins version, cutoff, threshold, identities, and score semantics', () => {
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([], { training_cutoff: '2021-10-01' })),
    /training_cutoff/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([], { novelty_threshold: 0.2 })),
    /novelty_threshold/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([], { format_version: 'report/v3' })),
    /format_version/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record(),
      record({ train_shape_overlap: 0.7 }),
    ])),
    /duplicate item\/week/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record({ classification: 'novel', train_shape_overlap: 0.25 }),
    ])),
    /novel classification is inconsistent/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record({ classification: 'Novel' }),
    ])),
    /classification is invalid/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record({ nearest_classification: 'maybe' }),
    ])),
    /nearest_classification is invalid/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record({ train_identity: 1.01 }),
    ])),
    /between 0 and 1/,
  );
  assert.throws(
    () => parseWeeklyTrainingSimilarityReport(report([
      record({ classification: 'unknown', train_shape_overlap: 0.1 }),
    ])),
    /unknown classification has a score/,
  );
});

test('optional training overlay must be content-addressed by its declared digest', () => {
  const sha256 = 'ab'.repeat(32);
  const overlay = {
    object_uri: `supabase://foldarium-quiz-public/sha256/ab/${sha256}`,
    sha256,
    size_bytes: 1234,
    media_type: 'chemical/x-pdb',
  };
  assert.deepEqual(parseContentAddressedSupabaseOverlay(overlay), overlay);
  const parsed = parseWeeklyTrainingSimilarityReport(report([
    record({ training_system_overlay: overlay }),
  ], {
    format_version: 'foldarium.weekly-training-similarity-report/v2',
  }));
  assert.deepEqual(parsed.records[0].overlay, overlay);
  assert.throws(
    () => parseContentAddressedSupabaseOverlay({
      ...overlay,
      object_uri: `supabase://foldarium-quiz-public/sha256/cd/${sha256}`,
    }),
    /content-addressed/,
  );
});

test('question sorting is stable with no-analog novel first, numeric direction, and unknown last', () => {
  const rows = [
    { index: 0, similarity: record({ item_id: '9AA0', train_shape_overlap: 0.8 }) },
    {
      index: 1,
      similarity: record({
        item_id: '9AA1',
        classification: 'unknown',
        train_shape_overlap: null,
      }),
    },
    {
      index: 2,
      similarity: record({
        item_id: '9AA2',
        classification: 'novel',
        train_shape_overlap: 0.1,
      }),
    },
    {
      index: 3,
      similarity: record({
        item_id: '9AA3',
        classification: 'novel',
        train_shape_overlap: null,
      }),
    },
    { index: 4, similarity: record({ item_id: '9AA4', train_shape_overlap: 0.8 }) },
    { index: 5, similarity: null },
  ];
  assert.deepEqual(sortWeeklySimilarityRows(rows).map(row => row.index), [0, 1, 2, 3, 4, 5]);
  assert.deepEqual(
    sortWeeklySimilarityRows(rows, 'novel-first').map(row => row.index),
    [3, 2, 0, 4, 1, 5],
  );
  assert.deepEqual(
    sortWeeklySimilarityRows(rows, 'familiar-first').map(row => row.index),
    [0, 4, 2, 3, 1, 5],
  );
  assert.equal(rows[3].similarity.train_shape_overlap, null);
});
