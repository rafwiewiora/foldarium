import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const appUrl = new URL('../app.js', import.meta.url);
const htmlUrl = new URL('../index.html', import.meta.url);

test('weekly thinking trace covers periodic, navigation, vote, visibility, and completion boundaries', async () => {
  const app = await readFile(appUrl, 'utf8');
  const html = await readFile(htmlUrl, 'utf8');

  assert.match(html, /import \{ createWeeklyTraceStream \} from '\.\/weekly-trace-stream\.js'/);
  assert.match(app, /onEntry: entry => weeklyTraceStream\?\.recordEntry\?\.\(entry\)/);
  assert.match(app, /weeklyTraceStream\?\.startVisit\?\.\(\{ itemId: item\.id, questionIndex: i \}\)/);
  assert.match(app, /weeklyTraceStream\?\.endVisit\?\.\('navigation'\)/);
  assert.match(app, /weeklyTraceStream\?\.endVisit\?\.\(idx \+ 1 < ITEMS\.length \? 'vote' : 'completion'\)/);
  assert.match(app, /weeklyTraceStream\?\.flush\?\.\('visibility'\)/);
  assert.match(app, /recordAppEvent\('vote_submitted'\)/);
  assert.match(app, /await weeklyTraceStream\?\.checkpoint\?\.\('vote'\)/);
  assert.match(app, /viewerTrace: !traceCheckpoint \|\| traceCheckpoint\.durable === false/);
  assert.match(app, /voteComment: cur\.voteCommentText/);
  assert.match(app, /selected_choice_ids: selectedChoiceIds/);
  assert.match(app, /rejected_choice_ids:/);
});

test('classic answer persistence keeps its legacy viewer trace path', async () => {
  const app = await readFile(appUrl, 'utf8');
  assert.match(app, /const viewerTrace = viewerTraceRecorder\?\.stop/);
  assert.match(app, /updateScore\(\);\s*if \(!isRetrospectiveReview\(\) && !postRevealVote\) logAnswer\(picked, af3, viewerTrace\)/);
  assert.match(app, /recordAnswer\(remoteSessionId, idx, \{ \.\.\.rec, viewer_trace: viewerTrace \}\)/);
});

test('weekly vote feedback bypasses the Molstar idle gate while classic reveal keeps it', async () => {
  const app = await readFile(appUrl, 'utf8');
  const reveal = app.slice(app.indexOf('async function reveal()'), app.indexOf('async function finalizeReveal()'));

  assert.match(reveal, /setVoteStatus\('Recording…', 'recording'\);\s*await finalizeReveal\(\);/);
  assert.match(reveal, /else \{\s*await revealAfterIdle\(\);/);
  assert.match(reveal, /viewerTransitionBusy/);
});

test('name form waits for readiness while persistence initializes in parallel', async () => {
  const html = await readFile(htmlUrl, 'utf8');
  const persistenceStart = html.indexOf('void initPersistence();');
  const molstarLoad = html.indexOf("await loadScript('https://cdn.jsdelivr.net/npm/molstar");

  assert.match(html, /start\.textContent = 'Loading quiz…'/);
  assert.match(html, /participant-setup'\)\.style\.display = 'none'/);
  assert.match(html, /Do not invite name entry until the backend, round, and Mol\* are ready/);
  assert.ok(persistenceStart > 0 && persistenceStart < molstarLoad);
});

test('the next question immutable structure assets are prefetched with bounded concurrency', async () => {
  const app = await readFile(appUrl, 'utf8');
  assert.match(app, /async function prefetchQuestionAssets\(questionIndex\)/);
  assert.match(app, /Array\.from\(\{ length: Math\.min\(4, urls\.length\) \}, worker\)/);
  assert.match(app, /void prefetchQuestionAssets\(i \+ 1\)/);
  assert.match(app, /fetch\(url, \{ cache: 'force-cache' \}\)/);
});
