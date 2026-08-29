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
  const [html, ui, css, similarity] = await Promise.all([
    source('weekly-retrospectives.html'),
    source('weekly-retrospectives.js'),
    source('weekly-retrospectives.css'),
    source('weekly-training-similarity.js'),
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
  assert.match(ui, /\['default', 'Default'\]/);
  assert.match(ui, /\['novel-first', 'Novel first'\]/);
  assert.match(ui, /\['familiar-first', 'Familiar first'\]/);
  assert.match(ui, /'Source PDB \+ ligand'/);
  assert.match(ui, /`PDB \$\{pdbId\} ↗`/);
  assert.match(ui, /https:\/\/www\.rcsb\.org\/structure\/\$\{encodeURIComponent\(pdbId\)\}/);
  assert.match(ui, /rel = 'noopener noreferrer'/);
  assert.match(ui, /fetchWeeklyTrainingSimilarityReport\(\)\.catch\(\(\) => null\)/);
  assert.match(ui, /weekly-play-for-fun-results/);
  assert.match(ui, /Blind-week players/);
  assert.match(ui, /Play for fun/);
  assert.match(ui, /`\/weekly\?retrospective_round=\$\{encodeURIComponent\(round\.round_id\)\}&play_for_fun=1`/);
  assert.match(similarity, /\/docs\/weekly-training-similarity-results\.json/);
  assert.match(similarity, /sortWeeklySimilarityRows/);
});

test('archive molecular review uses exact detail and bypasses weekly session and vote reads', async () => {
  const [index, app] = await Promise.all([source('index.html'), source('app.js')]);
  assert.match(index, /weekly-retrospectives\?round_id=/);
  assert.match(index, /FOLDARIUM_ARCHIVE_DETAIL_READY/);
  assert.match(index, /id="archive-review-loading" hidden/);
  assert.match(index, /wrap\.hidden = Boolean\(archiveRoundId && !archivePlayForFun\)/);
  assert.match(index, /window\.FOLDARIUM_ARCHIVE_PLAY = Object\.freeze/);
  assert.match(index, /window\.foldariumRevealArchiveReview =/);
  assert.match(app, /else if \(isArchiveRetrospective\(\)\) \{\s*activateArchiveDetail/);
  assert.match(
    app,
    /loadQuestion\(questionIndex\);\s*if \(isArchiveRetrospective\(\)\) window\.foldariumRevealArchiveReview\?\.\(\)/,
  );
  assert.match(app, /if \(DEV \|\| isRetrospectiveReview\(\)\)/);
  assert.match(app, /foldariumPrivateReview\.enrichPrivateWeeklyPool/);
  assert.match(app, /detail\.answer_overlays/);
  assert.match(app, /if \(answerActive && isRetrospectiveReview\(\)\)/);
  assert.doesNotMatch(app, /answerActive && isArchiveRetrospective\(\)/);
  assert.match(app, /if \(isRetrospectiveReview\(\) && released\?\.pdb_id && released\?\.structure_page_url\)/);
  assert.doesNotMatch(app, /scope unknown|Aggregate answers are hidden/);
  assert.match(app, /Player answers/);
  const archiveStart = app.indexOf(
    'else if (isArchiveRetrospective()) {\n      activateArchiveDetail',
  );
  assert.ok(archiveStart >= 0);
  const archiveBranch = app.slice(
    archiveStart,
    app.indexOf('} else {', archiveStart),
  );
  assert.doesNotMatch(archiveBranch, /getWeeklyRound|getWeeklyVotes|submitWeeklyVote|startNamedSession/);
  assert.match(index, /Past results/);
});

test('archive Play for fun launches a named post-reveal session without molecular overlays', async () => {
  const [index, app, archive] = await Promise.all([
    source('index.html'),
    source('app.js'),
    source('weekly-retrospectives.js'),
  ]);
  assert.match(archive, /Play for fun/);
  assert.match(archive, /play_for_fun=1/);
  assert.match(index, /archiveParams\.get\('play_for_fun'\) === '1'/);
  assert.match(index, /active: archivePlayForFun/);
  assert.match(app, /const isArchivePlayForFun =/);
  assert.match(app, /if \(isArchivePlayForFun\(\)\) \{\s*activateArchivePlayForFun/);
  assert.match(app, /Play for fun · back to results/);
  assert.match(app, /Shown on this round’s Play for fun leaderboard/);
  assert.match(app, /join this round’s separate Play for fun leaderboard/);
  assert.match(
    app,
    /current-retrospective-link'\)\.href =\s*`\/weekly\?retrospective_round=/,
  );
  assert.match(app, /postReveal: true/);
  const activation = app.slice(
    app.indexOf('const activateArchivePlayForFun = detail => {'),
    app.indexOf('// CAMEO:', app.indexOf('const activateArchivePlayForFun = detail => {')),
  );
  assert.match(activation, /POOLS\.weekly = normalizeWeekly/);
  assert.doesNotMatch(activation, /enrichPrivateWeeklyPool|answer_overlays|released_crystal/);
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
