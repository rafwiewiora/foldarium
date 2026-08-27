import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import {
  programmaticVotingEntry,
  quizEntryMode,
} from '../quiz-entry-mode.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

test('weekly routes select weekly-only mode without changing classic routes', () => {
  assert.equal(quizEntryMode('/'), 'classic');
  assert.equal(quizEntryMode('/index.html'), 'classic');
  assert.equal(quizEntryMode('/weekly-ish'), 'classic');
  assert.equal(quizEntryMode('/weekly'), 'weekly');
  assert.equal(quizEntryMode('/weekly/'), 'weekly');
  assert.equal(quizEntryMode('/weekly.html'), 'weekly');
});

test('programmatic voting requires its unlisted explicit query', () => {
  assert.equal(programmaticVotingEntry(''), false);
  assert.equal(programmaticVotingEntry('?programmatic=true'), false);
  assert.equal(programmaticVotingEntry('?programmatic=1'), true);
});

test('Vercel serves weekly and retrospective entry points through their shells', async () => {
  const config = JSON.parse(await read('vercel.json'));
  assert.deepEqual(config.rewrites, [
    { source: '/weekly/retrospectives/:roundId', destination: '/weekly-retrospectives.html' },
    { source: '/weekly/retrospectives', destination: '/weekly-retrospectives.html' },
    { source: '/api/weekly-selector/:path*', destination: '/api/weekly-selector?action=:path*' },
    { source: '/weekly', destination: '/index.html' },
    { source: '/weekly.html', destination: '/index.html' },
  ]);
});

test('weekly-only chrome keeps player voting separate from unlisted programmatic access', async () => {
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
  assert.match(html, /<label>Leaderboard name\s*<input id="participant-name"/);
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
  assert.match(html, /Programmatic voting · v2/);
  assert.match(html, /independent clustered \(Cluster or None\) and unclustered \(exact Pose or None\)/);
  assert.match(
    html,
    /html\[data-quiz-mode="weekly"\]\[data-programmatic-voting="true"\] #programmatic-voting\{display:flex\}/,
  );
  assert.match(html, /\.programmatic-voting\[hidden\]\{display:none!important\}/);
  assert.doesNotMatch(html, /href="[^"]*programmatic=1/);
  assert.match(html, /#wrap:not\(\.intro\) #programmatic-voting\{display:none!important\}/);
  assert.match(app, /const PROGRAMMATIC_VOTING = window\.FOLDARIUM_PROGRAMMATIC_VOTING === true/);
  assert.match(app, /if \(!WEEKLY_ONLY \|\| !PROGRAMMATIC_VOTING\) return/);
  assert.match(app, /function renderWeeklyResultsStatus\(\)/);
  assert.match(app, /Wednesday results are available\./);
  assert.match(app, /isReadOnlyPreview\(\)[\s\S]*?participantDisplayName = displayName;[\s\S]*?beginQuiz\(\)/);
  assert.match(app, /Read-only Preview:[\s\S]*?this vote was not saved/);
  assert.match(app, /Read-only Preview: you can inspect this dialog, but Send is disabled/);
  assert.match(app, /suggestion-open'\)\.disabled = !\(remoteSessionId \|\| isReadOnlyPreview\(\)\)/);
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
