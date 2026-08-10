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

test('dev2 shell separates inspection, preference, rejection, and optional vote notes', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  assert.match(app, /displayMode = WEEKLY_ONLY \? 'grid' : 'all'/);
  assert.match(app, /Math\.min\(3, n\)/);
  assert.match(app, /inspectCanonicalChoice\(choice\)/);
  assert.match(app, /inspectGridChoice\(cell\.entry, cell\.paneId, 'ligand-click'\)/);
  assert.match(app, /choice_preferred/);
  assert.match(app, /choice_rejected/);
  assert.match(app, /vote_comment: typeof cur\?\.voteCommentText/);
  assert.match(app, /cur\.voteCommentText = text/);
  assert.match(html, /data-review="prefer"|grid-review-actions/);
  assert.match(html, /id="vote-comment-enabled"[^>]*checked/);
  assert.match(html, /id="vote-comment-dialog"/);
  assert.doesNotMatch(html, /html\[data-quiz-mode="weekly"\] \.vote-comment-option/);
  assert.match(app, /\$\('#vote-comment-option'\)\.style\.display = 'none'/);
  assert.match(html, /<span class="control-label">Layout<\/span>/);
  assert.match(html, /<span class="control-label">View<\/span>/);
  assert.match(html, />✦ Feedback</);
});

test('Grid compacts the actual Molstar residue highlight overlay', async () => {
  const html = await read('index.html');
  assert.match(html, /\.grid-card \.msp-highlight-toast-wrapper/);
  assert.match(html, /\.grid-card \.msp-highlight-info\{[^}]*max-width:220px!important/);
  assert.doesNotMatch(html, /\.grid-card \.msp-hover-box\{/);
});
