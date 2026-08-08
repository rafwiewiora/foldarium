import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { quizEntryMode } from '../quiz-entry-mode.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

test('weekly routes select weekly-only mode without changing classic routes', () => {
  assert.equal(quizEntryMode('/'), 'classic');
  assert.equal(quizEntryMode('/index.html'), 'classic');
  assert.equal(quizEntryMode('/weekly-ish'), 'classic');
  assert.equal(quizEntryMode('/weekly'), 'weekly');
  assert.equal(quizEntryMode('/weekly/'), 'weekly');
  assert.equal(quizEntryMode('/weekly.html'), 'weekly');
});

test('Vercel serves both weekly entry points through the shared quiz shell', async () => {
  const config = JSON.parse(await read('vercel.json'));
  assert.deepEqual(config.rewrites, [
    { source: '/weekly', destination: '/index.html' },
    { source: '/weekly.html', destination: '/index.html' },
  ]);
});

test('weekly-only chrome keeps progress, voting, named start, and a Wednesday results panel', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);

  for (const id of ['quizsrc', 'diff', 'leaderboard-link', 'score']) {
    assert.match(html, new RegExp(`html\\[data-quiz-mode="weekly"\\][^}]*#${id}`));
  }
  assert.match(app, /const WEEKLY_ONLY = window\.FOLDARIUM_QUIZ_MODE === 'weekly'/);
  assert.match(app, /quizSource = WEEKLY_ONLY \? 'weekly' : 'cameo'/);
  assert.match(app, /displayMode = WEEKLY_ONLY \? 'one' : 'all'/);
  assert.match(app, /question \$\{idx \+ 1\} \/ \$\{ITEMS\.length\}/);
  assert.match(app, /startNamedSession\(\{/);
  assert.match(html, /id="participant-setup"/);
  assert.match(html, /Your name — required before starting/);
  assert.match(app, /function syncStartGate\(\)/);
  assert.match(html, /id="mode"/);
  assert.match(html, /id="choices"/);
  assert.match(html, /id="lock"/);
  assert.match(html, /id="weekly-results"/);
  assert.match(html, /Results and vote totals will be available Wednesday/);
  assert.match(app, /function renderWeeklyResultsStatus\(\)/);
  assert.match(app, /Wednesday results are available\./);
});

test('weekly pose-specific protein policy is explicit in one-at-a-time and Grid paths', async () => {
  const app = await read('app.js');
  assert.match(app, /if \(item\.source === 'weekly'\) \{[\s\S]*?choice\.afprotein_file/);
  assert.match(app, /if \(cur\.item\.source === 'weekly'\) \{[\s\S]*?shown\.afprotein_file/);
  assert.match(app, /cluster: choice\.cluster_id \|\| `choice-\$\{index\}`/);
  assert.match(app, /clustering_available: clusteringAvailable/);
});
