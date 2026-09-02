import test from 'node:test';
import assert from 'node:assert/strict';
import { createViewerPerformanceReporter } from '../viewer-performance.js';

test('reports question milestones and aggregated asynchronous stages', async () => {
  let now = 0;
  const logged = [];
  const marks = [];
  const measures = [];
  const observed = [];
  const reporter = createViewerPerformanceReporter({
    enabled: true,
    clock: () => now,
    logger: report => logged.push(report),
    observer: {
      begin: report => observed.push(['begin', report.id]),
      milestone: (report, name, elapsedMs) => observed.push(['milestone', report.id, name, elapsedMs]),
      finish: report => observed.push(['finish', report.id, report.totalMs]),
    },
    performanceImpl: {
      mark: name => marks.push(name),
      measure: (...args) => measures.push(args),
    },
  });
  const report = reporter.beginQuestion({ itemId: 'item-1', mode: 'grid' });

  await reporter.measure(report, 'structure-download', async () => {
    now += 12;
  });
  reporter.milestone(report, 'first-grid-card-ready', { card: 1 });
  await reporter.measure(report, 'structure-download', async () => {
    now += 8;
  });
  await reporter.measure(report, 'trajectory-parse', async () => {
    now += 5;
  });
  now += 2;
  const completed = reporter.finishQuestion(report, { cards: 9 });

  assert.equal(completed.totalMs, 27);
  assert.deepEqual(completed.milestones['first-grid-card-ready'], {
    elapsedMs: 12,
    details: { card: 1 },
  });
  assert.deepEqual(completed.stageSummary, {
    'structure-download': { count: 2, aggregateMs: 20, maxMs: 12 },
    'trajectory-parse': { count: 1, aggregateMs: 5, maxMs: 5 },
  });
  assert.equal(logged[0], completed);
  assert.equal(reporter.reports[0], completed);
  assert.ok(marks.includes('foldarium:question-1:start'));
  assert.equal(measures.length, 3);
  assert.deepEqual(observed, [
    ['begin', 'question-1'],
    ['milestone', 'question-1', 'first-grid-card-ready', 12],
    ['finish', 'question-1', 27],
  ]);
});

test('is transparent when performance reporting is disabled', async () => {
  const reporter = createViewerPerformanceReporter({ enabled: false });
  assert.equal(reporter.beginQuestion(), null);
  assert.equal(await reporter.measure(null, 'stage', async () => 42), 42);
  assert.equal(await reporter.measureStartup('startup', async () => 17), 17);
  assert.deepEqual(reporter.reports, []);
});

test('reports standalone startup timings', async () => {
  let now = 10;
  const logged = [];
  const reporter = createViewerPerformanceReporter({
    enabled: true,
    clock: () => now,
    logger: report => logged.push(report),
  });

  const value = await reporter.measureStartup('weekly-round-rpc', async () => {
    now += 6.25;
    return 'round';
  });

  assert.equal(value, 'round');
  assert.deepEqual(logged[0], {
    id: 'startup-1',
    stage: 'weekly-round-rpc',
    totalMs: 6.3,
  });
});
