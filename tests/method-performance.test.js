import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  aggregateMethodStats,
  methodTrend,
  scoreMethodPoses,
  validateMethodStats,
} from '../method-performance.js';

const confidence = value => ({
  metric: 'ligand_plddt',
  value,
});

test('scores oracle and top-1 from raw correctness rather than cluster acceptance', () => {
  const score = scoreMethodPoses([
    { id: 'pose-b', correct: true, accepted_correct: true, confidence: confidence(70) },
    { id: 'pose-a', correct: false, accepted_correct: true, confidence: confidence(90) },
  ]);
  assert.deepEqual(score, {
    oracle_success: true,
    top1_success: false,
    top1_choice_id: 'pose-a',
    top1_plddt: 90,
  });
});

test('breaks equal ligand pLDDT ties by stable choice ID', () => {
  const score = scoreMethodPoses([
    { id: 'pose-b', correct: true, confidence: confidence(80) },
    { id: 'pose-a', correct: false, confidence: confidence(80) },
  ]);
  assert.equal(score.top1_choice_id, 'pose-a');
  assert.equal(score.top1_success, false);
});

test('excludes a target without ligand pLDDT from top-1 only', () => {
  const score = scoreMethodPoses([
    { id: 'pose-a', correct: true, confidence: { metric: 'ranking_score', value: 0.8 } },
  ]);
  assert.equal(score.oracle_success, true);
  assert.equal(score.top1_success, null);
  assert.equal(score.top1_choice_id, null);
});

test('aggregates method totals and returns ordered weekly trends', () => {
  const data = {
    schema_version: 1,
    weeks: [
      {
        week: '2026-08-15', method: 'boltz2', targets: 3,
        oracle_successes: 2, top1_evaluated: 2, top1_successes: 1,
      },
      {
        week: '2026-08-08', method: 'boltz2', targets: 2,
        oracle_successes: 1, top1_evaluated: 2, top1_successes: 1,
      },
      {
        week: '2026-08-08', method: 'openfold3', targets: 2,
        oracle_successes: 0, top1_evaluated: 2, top1_successes: 0,
      },
    ],
  };
  assert.deepEqual(aggregateMethodStats(data)[0], {
    method: 'boltz2',
    targets: 5,
    oracle_successes: 3,
    top1_evaluated: 4,
    top1_successes: 2,
    oracle_rate: 60,
    top1_rate: 50,
  });
  assert.deepEqual(methodTrend(data, 'boltz2').map(row => row.week), [
    '2026-08-08',
    '2026-08-15',
  ]);
});

test('the preview fixture contains the three revealed production weeks', async () => {
  const data = JSON.parse(await readFile(
    new URL('../weekly_method_stats.json', import.meta.url),
    'utf8',
  ));
  const rows = validateMethodStats(data);
  const methods = aggregateMethodStats(data);
  assert.deepEqual([...new Set(rows.map(row => row.week))], [
    '2026-08-08',
    '2026-08-15',
    '2026-08-22',
  ]);
  assert.deepEqual(methods.map(entry => ({
    method: entry.method,
    targets: entry.targets,
    oracle_successes: entry.oracle_successes,
    top1_successes: entry.top1_successes,
  })), [
    { method: 'boltz2', targets: 100, oracle_successes: 46, top1_successes: 41 },
    { method: 'openfold3', targets: 100, oracle_successes: 44, top1_successes: 31 },
  ]);
});

test('rejects duplicate or internally inconsistent weekly counts', () => {
  assert.throws(() => validateMethodStats({
    schema_version: 1,
    weeks: [{
      week: '2026-08-08', method: 'boltz2', targets: 1,
      oracle_successes: 2, top1_evaluated: 1, top1_successes: 1,
    }],
  }), /invalid/);
});
