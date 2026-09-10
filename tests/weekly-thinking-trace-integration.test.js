import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const appUrl = new URL('../app.js', import.meta.url);
const htmlUrl = new URL('../index.html', import.meta.url);
const prefetchUrl = new URL('../structure-prefetch.js', import.meta.url);

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
  const persistenceStart = html.indexOf('const persistenceReady = initPersistence();');
  const molstarLoad = html.indexOf("await loadScript('https://cdn.jsdelivr.net/npm/molstar");

  assert.match(html, /start\.textContent = 'Loading quiz…'/);
  assert.match(html, /participant-setup'\)\.style\.display = 'none'/);
  assert.match(html, /Do not invite name entry until the backend, round, and Mol\* are ready/);
  assert.ok(persistenceStart > 0 && persistenceStart < molstarLoad);
});

test('three future Grid pages are prefetched without navigation cancellation', async () => {
  const [app, html, prefetch] = await Promise.all([
    readFile(appUrl, 'utf8'),
    readFile(htmlUrl, 'utf8'),
    readFile(prefetchUrl, 'utf8'),
  ]);
  assert.match(html, /import\('\.\/structure-prefetch\.js'\)/);
  assert.match(html, /import\('\.\/viewer-performance\.js'\)/);
  assert.match(app, /async function prefetchQuestionAssets\(questionIndex, \{ priority = 0 \} = \{\}\)/);
  assert.match(app, /gridQuestionAssetPaths\(item, clusters/);
  assert.match(app, /pageSize: GRID_PAGE_SIZE/);
  assert.match(app, /initialQuestionAssetPaths\(item, initialChoice\)/);
  assert.match(app, /initialChoice = isClustered \? clusters\[0\]\?\.rep : clusters\[0\]\?\.members\?\.\[0\]/);
  assert.match(app, /WEEKLY_PREFETCHED_CLUSTERS\.set\(item\.id, clusters\)/);
  assert.match(app, /stage: 'first-question-prefetch'/);
  assert.match(app, /const QUESTION_PREFETCH_LOOKAHEAD = 3/);
  assert.match(app, /stage: 'setup-lookahead-prefetch'/);
  assert.doesNotMatch(app, /async function loadQuestion\(i\) \{\s*structurePrefetcher\.cancel\(\)/);
  assert.match(app, /structurePrefetcher\.textWhenReady\(requestUrl\)/);
  assert.match(app, /builders\.data\.rawData\(\{ data: prefetchedText/);
  assert.match(app, /pendingQuestionPrefetchIndexes = Array\.from\(/);
  assert.match(app, /priority: QUESTION_PREFETCH_LOOKAHEAD - distance/);
  assert.match(app, /view\.classList\.remove\('loading-grid'\); syncReviewState\(\);\s*startPendingQuestionPrefetch\(\)/);
  assert.match(prefetch, /const DEFAULT_CONCURRENCY = 4/);
  assert.match(prefetch, /cache: 'force-cache'/);
  assert.match(prefetch, /inFlightByUrl/);
});

test('Grid reveals ready cards progressively and defers the hidden canonical scene', async () => {
  const [app, html] = await Promise.all([
    readFile(appUrl, 'utf8'),
    readFile(htmlUrl, 'utf8'),
  ]);
  const buildLayer = app.slice(
    app.indexOf('async function buildLayer()'),
    app.indexOf('function requestQuestionCameraReset()'),
  );
  const gridBranch = buildLayer.slice(
    buildLayer.indexOf("if (displayMode === 'grid')"),
    buildLayer.indexOf("if ($('#gridview').classList.contains('on'))"),
  );

  assert.doesNotMatch(html, /#gridview\.loading-grid #gridcells\{opacity:0/);
  assert.match(html, /\.grid-card\.grid-card-loading\{visibility:hidden\}/);
  assert.match(app, /card\.className = 'grid-card grid-card-loading'/);
  assert.match(app, /cell\.card\.classList\.remove\('grid-card-loading'\)/);
  assert.match(gridBranch, /await buildGrid\(!resetCamera, false\)/);
  assert.doesNotMatch(gridBranch, /buildCanonicalLayer/);
});

test('Weekly reuses a bounded Grid viewer pool and permits explicit performance A/B opt-out', async () => {
  const [app, html] = await Promise.all([
    readFile(appUrl, 'utf8'),
    readFile(htmlUrl, 'utf8'),
  ]);

  assert.match(html, /window\.foldariumGridViewerPool = await import\('\.\/grid-viewer-pool\.js'\)/);
  assert.match(app, /WEEKLY_ONLY && APP_QUERY\.get\('viewer_pool'\) !== '0'/);
  assert.match(app, /WEEKLY_ONLY && APP_QUERY\.get\('fast_camera'\) !== '0'/);
  assert.match(app, /GRID_VIEWER_POOL_ENABLED\s*&& APP_QUERY\.get\('warm_viewers'\) !== '0'/);
  assert.match(app, /FIRST_GRID_PREBUILD_ENABLED = GRID_VIEWER_PREWARM_ENABLED\s*&& APP_QUERY\.get\('first_grid'\) !== '0'/);
  assert.match(app, /createGridViewerPool\?\.\(\{\s*enabled: GRID_VIEWER_POOL_ENABLED,\s*maxSize: GRID_PAGE_SIZE/);
  assert.match(app, /async function prewarmGridViewerPool\(\)/);
  assert.match(app, /await waitForViewerPrewarmIdle\(\)/);
  assert.match(app, /gridViewerPool\.add\(\{/);
  assert.match(app, /cancelGridViewerPrewarm\(\);\s*if \(DEV/);
  assert.match(app, /gridViewerPool\.release\(cell, \{/);
  assert.match(app, /'grid-viewer-reuse-clear'/);
  assert.match(app, /await gridViewerPool\.acquire\(\)/);
  assert.match(app, /cell\.host\.replaceWith\(pooled\.host\)/);
  assert.match(app, /cell\.reusable = true/);
  assert.match(app, /gridViewersReused:/);
  assert.match(app, /gridViewersCreated:/);
  assert.match(app, /gridViewerPoolSize: gridViewerPool\.size\(\)/);
  assert.match(app, /'grid-camera-finalize'/);
  assert.match(app, /if \(FAST_GRID_CAMERA_SYNC_ENABLED\)/);
  assert.match(app, /await nextAnimationFrame\(\)/);
  assert.match(app, /async function prebuildFirstWeeklyGrid\(\)/);
  assert.match(app, /'first-grid-scene-prebuild'/);
  assert.match(app, /consumePreparedFirstGrid\(cur\.item, cells\.map\(cell => cell\.entry\)\)/);
  assert.match(app, /cell\.viewerSource = 'prebuilt'/);
  assert.match(app, /\[remoteSessionId\] = await Promise\.all\(\[sessionPromise, firstGridReady\]\)/);
  assert.match(app, /function beginStartPerformanceTiming\(\)/);
  assert.match(app, /'named-session-start'/);
  assert.match(app, /const performanceTiming = pendingQuestionPerformanceTiming\s*\|\| viewerPerformance\.beginQuestion/);
  assert.match(app, /includesStart: true/);
});

test('opt-in performance diagnostics use the named Weekly session but not its replay trace', async () => {
  const [app, html] = await Promise.all([
    readFile(appUrl, 'utf8'),
    readFile(htmlUrl, 'utf8'),
  ]);

  assert.match(html, /query\.get\('record_performance'\) === '1' \|\| deploymentPerformanceBeta/);
  assert.match(html, /window\.FOLDARIUM_SUPABASE\?\.performanceBetaEnabled === true/);
  assert.match(html, /import\('\.\/performance-diagnostics\.js'\)/);
  assert.match(html, /id="performance-consent-checkbox"/);
  assert.match(html, /does not record browser fingerprints, IP addresses, plugins, fonts/);
  assert.match(app, /PERFORMANCE_RECORDING_REQUESTED/);
  assert.match(app, /performanceDiagnosticsConsented\(\)/);
  assert.match(app, /performanceDiagnosticsCollector\.capture\(report/);
  assert.match(
    app,
    /submitWeeklyPerformanceReport\?\.\(\{/,
  );
  assert.doesNotMatch(app, /recordAppEvent\?\.\('viewer_performance_diagnostics'/);
  assert.match(app, /performance_diagnostics_opt_in: PERFORMANCE_RECORDING_REQUESTED/);
  assert.match(app, /PERFORMANCE_RECORDING_REQUESTED \|\| isReadOnlyPreview\(\)/);
});
