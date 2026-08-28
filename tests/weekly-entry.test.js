import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { quizEntryMode } from '../quiz-entry-mode.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

test('the root and legacy weekly routes select weekly-only mode', () => {
  assert.equal(quizEntryMode('/'), 'weekly');
  assert.equal(quizEntryMode('/index.html'), 'weekly');
  assert.equal(quizEntryMode('/weekly-ish'), 'classic');
  assert.equal(quizEntryMode('/weekly'), 'weekly');
  assert.equal(quizEntryMode('/weekly/'), 'weekly');
  assert.equal(quizEntryMode('/weekly.html'), 'weekly');
  assert.equal(quizEntryMode('/datasets'), 'classic');
  assert.equal(quizEntryMode('/datasets/'), 'classic');
  assert.equal(quizEntryMode('/datasets.html'), 'classic');
});

test('weekly-only chrome stays focused on human play while the Selector API remains separate', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);

  for (const id of ['setup', 'leaderboard-link', 'score', 'score-summary']) {
    assert.match(html, new RegExp(`html\\[data-quiz-mode="weekly"\\][^}]*#${id}`));
  }
  assert.match(app, /const WEEKLY_ONLY = window\.FOLDARIUM_QUIZ_MODE === 'weekly'/);
  assert.match(app, /quizSource = WEEKLY_ONLY \? 'weekly' : 'cameo'/);
  assert.match(app, /displayMode = WEEKLY_ONLY \? 'grid' : 'all'/);
  assert.match(app, /const questionOrdinal = isRetrospectiveReview\(\)/);
  assert.match(app, /`question \$\{questionOrdinal\}`/);
  assert.match(app, /startNamedSession\(\{/);
  assert.match(html, /id="participant-setup"/);
  assert.match(html, /<label>Player name\s*<input id="participant-name"/);
  assert.match(html, /Shown on the results leaderboard after release/);
  assert.doesNotMatch(html, /required before starting/);
  assert.match(app, /function syncStartGate\(\)/);
  assert.match(html, /Do not invite name entry until the backend, round, and Mol\* are ready/);
  assert.match(html, /foldariumWeeklySessionResume\.hasToken\(\)/);
  assert.match(app, /resumeWeeklyQuizIfAvailable\(\)/);
  assert.match(html, /id="mode"/);
  assert.match(html, /id="choices"/);
  assert.match(html, /id="lock"/);
  assert.match(html, /id="weekly-results"/);
  assert.match(html, /Available Wednesday\./);
  assert.match(html, /id="datasets-link"[\s\S]*?href="\/datasets"/);
  assert.doesNotMatch(html, /Programmatic voting|id="programmatic-voting"|selector-download-kit/);
  assert.match(app, /function renderWeeklyResultsStatus\(\)/);
  assert.match(app, /new votes are recorded as post-reveal and excluded from blind-week scores/);
  assert.match(app, /isReadOnlyPreview\(\)[\s\S]*?participantDisplayName = displayName;[\s\S]*?beginQuiz\(\)/);
  assert.match(app, /Read-only Preview:[\s\S]*?this vote was not saved/);
  assert.match(app, /Read-only Preview: you can inspect this dialog, but Send is disabled/);
  assert.match(app, /suggestion-open'\)\.disabled = WEEKLY_ROUND\?\.public_status === 'revealed'/);
  assert.match(html, /id="revealed-weekly-modes" hidden/);
  assert.match(html, /This week’s Weekly is revealed/);
  assert.match(html, /Next blind Weekly opens Saturday/);
  assert.match(app, /Enter a player name to activate Play for fun/);
  assert.match(app, /nameHint\.classList\.toggle\('action-required', showRevealedModes\)/);
  assert.match(html, /id="play-for-fun-start"[\s\S]*Play for fun/);
  assert.match(html, /No Xtal · opponents shown after each answer/);
  assert.match(html, /id="current-retrospective-link"[\s\S]*Review with Xtal/);
  assert.match(app, /with a correct pose[\s\S]*where “None” is correct/);
  assert.match(app, /retrospectiveLink\.href = `\/weekly\?retrospective_round=\$\{encodeURIComponent\(WEEKLY_ROUND\.round_id\)\}`/);
  assert.match(app, /showRevealedModes[\s\S]*start'\)\.style\.display/);
  assert.match(app, /crystalReviewAllowed = cur\?\.item\?\.source !== 'weekly' \|\| isRetrospectiveReview\(\)/);
  assert.match(app, /quizSource === 'weekly' \? 'Ligand pLDDT'/);
  assert.match(app, /plddt_pick_sample: plddtPick\?\.af3_sample \?\? -1/);
  assert.match(app, /opponentChoiceCorrect = choice => quizSource === 'weekly'[\s\S]*choice\?\.correct === true/);
});

test('the classic leaderboard returns to the datasets route', async () => {
  const html = await read('leaderboard.html');
  assert.match(html, /href="\/datasets">play →<\/a>/);
});

test('weekly pose-specific protein policy is explicit in one-at-a-time and Grid paths', async () => {
  const app = await read('app.js');
  assert.match(app, /if \(item\.source === 'weekly'\) \{[\s\S]*?choice\.afprotein_file/);
  assert.match(app, /if \(cur\.item\.source === 'weekly'\) \{[\s\S]*?displayMode !== 'one'[\s\S]*?shown\?\.afprotein_file/);
  assert.match(app, /cluster: choice\.cluster_id \|\| `choice-\$\{index\}`/);
  assert.match(app, /clustering_available: clusteringAvailable/);
  assert.match(app, /alignment_warning: item\.metadata\?\.display_alignment \|\| null/);
  assert.match(app, /cur\.item\.alignment_warning\?\.message/);
});
