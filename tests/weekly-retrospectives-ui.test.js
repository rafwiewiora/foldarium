import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  OUTCOME_FILTERS,
  archiveRoute,
  humanAnswerSummary,
  questionOutcome,
} from '../weekly-retrospectives.js';
import { quizEntryMode } from '../quiz-entry-mode.js';

const root = new URL('../', import.meta.url);
const source = name => readFile(new URL(name, root), 'utf8');

test('archive routes resolve list, detail, and all-time without changing weekly entry', async () => {
  assert.deepEqual(archiveRoute('/weekly/retrospectives'), {
    view: 'archive',
    roundId: null,
  });
  assert.deepEqual(archiveRoute('/weekly/retrospectives', '?view=all-time'), {
    view: 'all-time',
    roundId: null,
  });
  assert.deepEqual(archiveRoute('/weekly/retrospectives/weekly-2026-08-20'), {
    view: 'archive',
    roundId: 'weekly-2026-08-20',
  });
  assert.equal(archiveRoute('/weekly/retrospectives/%2e%2e').roundId, null);
  assert.equal(quizEntryMode('/weekly'), 'weekly');
  assert.equal(quizEntryMode('/weekly/retrospectives'), 'classic');
});

test('four outcome semantics classify pose and None questions by human solve state', () => {
  assert.deepEqual(OUTCOME_FILTERS.map(([value]) => value), [
    'pose-solved',
    'pose-unsolved',
    'none-solved',
    'none-unsolved',
  ]);
  const pose = { choices: [{ correct: false }, { correct: true }] };
  const none = { choices: [{ correct: false }] };
  assert.equal(questionOutcome({ human_aggregate: { correct_count: 2 } }, pose), 'pose-solved');
  assert.equal(questionOutcome({ human_aggregate: { correct_count: 0 } }, pose), 'pose-unsolved');
  assert.equal(questionOutcome({ human_aggregate: { correct_count: 1 } }, none), 'none-solved');
  assert.equal(questionOutcome({ human_aggregate: { correct_count: 0 } }, none), 'none-unsolved');
  assert.equal(
    questionOutcome({ human_aggregate: { suppressed: true, correct_count: null } }, pose),
    'suppressed',
  );
});

test('question results distinguish absent human answers from automated methods', () => {
  assert.equal(humanAnswerSummary({ answered_count: 0, correct_count: 0 }), 'No answers');
  assert.equal(humanAnswerSummary({ answered_count: 3, correct_count: 2 }), '2/3 correct');
});

test('standalone archive stays Mol-star-free and renders API names with safe DOM text', async () => {
  const [html, ui, css] = await Promise.all([
    source('weekly-retrospectives.html'),
    source('weekly-retrospectives.js'),
    source('weekly-retrospectives.css'),
  ]);
  assert.doesNotMatch(`${html}\n${ui}`, /molstar|Mol\*/i);
  assert.doesNotMatch(ui, /\.innerHTML\s*=/);
  assert.match(ui, /\.textContent\s*=/);
  assert.match(html, /data-kind="human" title="Show player pseudonyms"/);
  assert.match(ui, /'Human players'/);
  assert.match(ui, /'Automated methods'/);
  assert.match(ui, /Human outcomes · share of questions/);
  assert.match(ui, /answer\.display_names\.join\(', '\)/);
  assert.match(css, /\.rail-fill\{display:block;/);
  assert.match(css, /@media\(max-width:620px\)/);
  assert.match(css, /min-width:320px/);
  assert.match(css, /prefers-reduced-motion:reduce/);
  assert.match(css, /min-height:44px/);
});

test('archive molecular review uses exact detail and bypasses weekly session and vote reads', async () => {
  const [index, app] = await Promise.all([source('index.html'), source('app.js')]);
  assert.match(index, /weekly-retrospectives\?round_id=/);
  assert.match(index, /FOLDARIUM_ARCHIVE_DETAIL_READY/);
  assert.match(index, /id="archive-review-loading" hidden/);
  assert.match(index, /wrap\.hidden = Boolean\(archiveRoundId\)/);
  assert.match(index, /window\.foldariumRevealArchiveReview =/);
  assert.match(app, /if \(isArchiveRetrospective\(\)\) \{\s*activateArchiveDetail/);
  assert.match(
    app,
    /loadQuestion\(questionIndex\);\s*if \(isArchiveRetrospective\(\)\) window\.foldariumRevealArchiveReview\?\.\(\)/,
  );
  assert.match(app, /if \(DEV \|\| isRetrospectiveReview\(\)\)/);
  assert.match(app, /foldariumPrivateReview\.enrichPrivateWeeklyPool/);
  assert.match(app, /detail\.answer_overlays/);
  assert.doesNotMatch(app, /scope unknown|Aggregate answers are hidden/);
  assert.match(app, /Player answers/);
  const archiveStart = app.indexOf(
    'if (isArchiveRetrospective()) {\n      activateArchiveDetail',
  );
  assert.ok(archiveStart >= 0);
  const archiveBranch = app.slice(
    archiveStart,
    app.indexOf('} else {', archiveStart),
  );
  assert.doesNotMatch(archiveBranch, /getWeeklyRound|getWeeklyVotes|submitWeeklyVote|startNamedSession/);
  assert.match(index, /Past results/);
});

test('archive documents the authenticated-proxy admin attestation', async () => {
  const [api, readme] = await Promise.all([
    source('api/weekly-retrospectives.js'),
    source('README.md'),
  ]);
  assert.match(api, /FOLDARIUM_WEEKLY_RETROSPECTIVE_ADMIN_ACCESS/);
  assert.match(api, /authenticated-proxy/);
  assert.match(readme, /authenticated reverse proxy/i);
});
