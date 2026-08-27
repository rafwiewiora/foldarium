import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import {
  GRID_PAGE_SIZE,
  formatReleaseCountdown,
  gridPage,
  gridPageCount,
  rejectedState,
  reviewChoiceIds,
} from '../dev2-feedback.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

test('weekly Grid pages are bounded to a three-by-three page', () => {
  const entries = Array.from({ length: 10 }, (_, index) => ({ index }));
  assert.equal(GRID_PAGE_SIZE, 9);
  assert.equal(gridPageCount(entries.length), 2);
  assert.deepEqual(gridPage(entries, 0).entries.map(x => x.index), [0, 1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(gridPage(entries, 1).entries.map(x => x.index), [9]);
  assert.equal(gridPage(entries, 99).index, 1);
});

test('countdown copy is deterministic and closes cleanly', () => {
  const closes = '2026-08-12T00:00:00Z';
  assert.equal(
    formatReleaseCountdown(closes, Date.parse('2026-08-10T12:00:00Z')),
    'Results Wednesday · voting closes in 1d 12h',
  );
  assert.equal(
    formatReleaseCountdown(closes, Date.parse('2026-08-12T00:00:01Z')),
    'Voting closed · results processing.',
  );
});

test('cluster rejection covers every raw choice while unclustered rejection is exact', () => {
  const members = [
    { _weeklyChoiceId: 'choice-a' },
    { _weeklyChoiceId: 'choice-b' },
  ];
  const cluster = { members };
  assert.deepEqual(reviewChoiceIds(members[0], cluster, true), ['choice-a', 'choice-b']);
  assert.deepEqual(reviewChoiceIds(members[0], cluster, false), ['choice-a']);
  assert.equal(rejectedState(new Set(['choice-a', 'choice-b']), members[0], cluster, true), true);
  assert.equal(rejectedState(new Set(['choice-a']), members[0], cluster, true), false);
});

test('tester shell separates inspection, selection, rejection, and optional vote notes', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  assert.match(app, /displayMode = WEEKLY_ONLY \? 'grid' : 'all'/);
  assert.match(app, /Math\.min\(3, n\)/);
  assert.match(app, /inspectCanonicalChoice\(choice\)/);
  assert.match(app, /inspectGridChoice\(cell\.entry, cell\.paneId, 'ligand-click'\)/);
  assert.match(app, /choice_selected/);
  assert.match(app, /choice_rejected/);
  assert.match(app, /voteComment: cur\.voteCommentText/);
  assert.match(app, /cur\.voteCommentText = text/);
  assert.match(html, /data-review="select"|grid-review-actions/);
  assert.match(html, /id="vote-comment-enabled"[^>]*checked/);
  assert.match(html, /id="vote-comment-dialog"/);
  assert.doesNotMatch(html, /html\[data-quiz-mode="weekly"\] \.vote-comment-option/);
  assert.match(app, /\$\('#vote-comment-option'\)\.style\.display = 'none'/);
  assert.match(html, /<span class="control-label">Layout<\/span>/);
  assert.match(html, /<span class="control-label">View<\/span>/);
  assert.match(html, />✦ Feedback</);
});

test('weekly voting keeps comment prompting optional and supports free question review', async () => {
  const [app, html, backend, migration] = await Promise.all([
    read('app.js'),
    read('index.html'),
    read('quiz-backend.js'),
    read('supabase/migrations/20260808010500_add_named_quiz_research_events.sql'),
  ]);

  assert.match(html, /class="vote-submit-row"[\s\S]*?id="lock"[\s\S]*?id="vote-comment-enabled"[^>]*checked/);
  assert.match(html, /id="question-prev"[\s\S]*?id="question-next"/);
  assert.match(app, /weeklyCommentPromptEnabled = false;[\s\S]*?vote-comment-enabled'\)\.checked = false/);
  assert.match(app, /if \(idx \+ 1 < ITEMS\.length\) await loadQuestion\(idx \+ 1\)/);
  assert.match(app, /WEEKLY_ITEM_STATES\.set\(cur\.item\.id, cur\)/);
  assert.match(app, /savedWeeklyState\?\.clusters/);
  assert.match(app, /WEEKLY_VOTES\.has\(cur\.item\.id\) \? 'Update vote' : 'Record vote'/);
  assert.match(app, /voteAttemptId: newVoteAttemptId\(\)/);
  assert.match(backend, /voteAttemptId = uuid\(\)/);
  assert.match(backend, /p_vote_attempt_id: voteAttemptId/);
  assert.match(migration, /insert into public\.weekly_quiz_vote_attempts/);
  assert.match(migration, /on conflict \(round_id, user_id, item_id\) do update/);
  assert.doesNotMatch(app, /\$\('#next'\)\.style\.display = '';[\s\S]{0,120}\$\('#next'\)\.textContent = idx \+ 1 < ITEMS\.length \? 'Next →' : 'Finish →'/);
});

test('Grid compacts the actual Molstar residue highlight overlay', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);
  assert.match(html, /\.grid-card \.msp-highlight-toast-wrapper/);
  assert.match(html, /\.grid-card \.msp-highlight-info\{[^}]*max-width:220px!important/);
  assert.match(html, /\.grid-card \.msp-viewport-controls\{display:none!important\}/);
  assert.match(app, /label: 'Foldarium'/);
  assert.doesNotMatch(html, /\.grid-card \.msp-hover-box\{/);
});

test('Grid reserves the measured instruction height above its viewers', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);
  assert.match(html, /--grid-top-clearance:84px/);
  assert.match(html, /#gridview\.on\{display:block;top:var\(--grid-top-clearance\)/);
  assert.match(app, /function reserveGridTopClearance\(\)/);
  assert.match(app, /questionRect\.bottom - stageRect\.top \+ 12/);
  assert.match(app, /observer\.observe\(\$\('#viewer-question'\)\)/);
});

test('View actions use independent switch affordances', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  assert.match(html, /#view-options \.view-actions button::after/);
  assert.match(html, /#view-options \.view-actions button\.on::after/);
  assert.match(app, /uc\.setAttribute\('aria-pressed', String\(!clustered\)\)/);
});
