import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

import {
  ALLOWED_ROUND_ID,
  WEEKLY_LEADERBOARD_FORMAT_VERSION,
  validateWeeklyLeaderboard,
} from '../weekly-private-review.js';
import { createDeferredBackend, createQuizBackend } from '../quiz-backend.js';
import { createViewerTraceRecorder } from '../viewer-trace.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

function block(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected ${signature} in app.js`);
  const open = source.indexOf('{', start + signature.length - 1);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return { start, end: index + 1 };
    }
  }
  throw new Error(`unbalanced braces after ${signature}`);
}

function evaluateDeclaration(source, signature, sandbox) {
  const { start, end } = block(source, signature);
  return vm.runInContext(`(${source.slice(start, end)})`, vm.createContext(sandbox));
}

function sampleLeaderboard(overrides = {}) {
  return {
    format_version: WEEKLY_LEADERBOARD_FORMAT_VERSION,
    round_id: ALLOWED_ROUND_ID,
    item_count: 29,
    participant_count: 3,
    complete_runs: [{
      display_name: 'Ada',
      correct: 20,
      answered: 29,
      total: 29,
      accuracy: 69,
      coverage: 100,
      rank: 1,
    }],
    partial_runs: [{
      display_name: 'Claude Opus',
      correct: 14,
      answered: 21,
      total: 29,
      accuracy: 67,
      coverage: 72,
    }, {
      display_name: 'Codex GPT-5.6',
      correct: 13,
      answered: 21,
      total: 29,
      accuracy: 62,
      coverage: 72,
    }],
    ...overrides,
  };
}

test('Grid constrains nine cards to the selected three-column layout', async () => {
  const app = await read('app.js');
  const properties = new Map();
  const box = {
    style: {
      maxWidth: '',
      setProperty(name, value) { properties.set(name, value); },
    },
  };
  const view = {
    clientWidth: 1024,
    clientHeight: 555,
    classList: { contains: value => value === 'on' },
  };
  const sandbox = {
    gridViewers: Array.from({ length: 9 }, () => ({ viewer: { handleResize() {} } })),
    $: selector => selector === '#gridview' ? view : box,
  };
  const layoutGrid = evaluateDeclaration(app, 'function layoutGrid()', sandbox);

  layoutGrid();

  assert.equal(properties.get('--grid-card-w'), '228.8px');
  assert.equal(properties.get('--grid-card-h'), '171.6px');
  assert.equal(box.style.maxWidth, '706.4px');
});

test('retrospective trace recorder supports every viewer rebuild hook', () => {
  const observable = { subscribe: () => ({ unsubscribe() {} }) };
  const plugin = {
    state: { getSnapshot: () => ({}) },
    canvas3d: {
      camera: {
        changed: observable,
        getSnapshot: () => ({}),
      },
    },
    managers: {
      structure: {
        focus: { behaviors: { current: observable } },
        selection: { events: { changed: observable } },
      },
    },
  };
  const recorder = createViewerTraceRecorder({
    plugin,
    now: () => 0,
    setTimer: () => 0,
    clearTimer() {},
  });
  for (const method of [
    'start', 'stop', 'captureState', 'captureCamera', 'recordAppEvent',
    'setActivePane', 'attachPane', 'snapshot', 'captureContext', 'dispose',
  ]) {
    assert.equal(typeof recorder[method], 'function', `${method} should be available`);
  }
  recorder.start();
  const detach = recorder.attachPane({ paneId: 'grid-1', plugin });
  assert.equal(typeof detach, 'function');
  assert.doesNotThrow(() => recorder.captureState());
  detach();
  recorder.dispose();
});

test('retrospective viewer title uses the released PDB identity and RCSB URL', async () => {
  const app = await read('app.js');
  const item = {
    ligand: 'A1DI6',
    released_crystal: {
      pdb_id: '12lb',
      structure_page_url: 'https://www.rcsb.org/structure/12LB',
    },
  };
  const archiveIdentity = evaluateDeclaration(app, 'function viewerQuestionIdentity()', {
    cur: { item },
    isRetrospectiveReview: () => true,
  });
  const weeklyIdentity = evaluateDeclaration(app, 'function viewerQuestionIdentity()', {
    cur: { item },
    isRetrospectiveReview: () => false,
  });

  assert.equal(archiveIdentity().label, '12LB');
  assert.equal(archiveIdentity().url, 'https://www.rcsb.org/structure/12LB');
  assert.equal(weeklyIdentity().label, 'A1DI6');
  assert.equal(weeklyIdentity().url, null);
});

test('bestRawCorrectPose picks the lowest finite-RMSD raw-correct choice', async () => {
  const app = await read('app.js');
  const bestRawCorrectPose = evaluateDeclaration(app, 'function bestRawCorrectPose(choices = allItemChoices())', {
    rawChoiceCorrect: choice => choice?.correct === true,
  });
  const best = bestRawCorrectPose([
    { correct: true, rmsd: 2.1, label: 'B' },
    { correct: true, rmsd: 0.9, label: 'A' },
    { correct: false, rmsd: 0.5, label: 'C' },
    { correct: true, rmsd: Number.NaN, label: 'D' },
  ]);
  assert.equal(best.label, 'A');
});

test('clustered answer sidebar expands accepted clusters and marks non-exact members yellow', async () => {
  const app = await read('app.js');
  const a2 = { id: 'a2', label: 'A-2', rmsd: 1.3, correct: true };
  const a1 = { id: 'a1', label: 'A-1', rmsd: 1.49, correct: true };
  const a3 = { id: 'a3', label: 'A-3', rmsd: 2.5, correct: false };
  const b1 = { id: 'b1', label: 'B-1', rmsd: 9.45, correct: false };
  const b2 = { id: 'b2', label: 'B-2', rmsd: 10.2, correct: false };
  const cur = {
    item: { source: 'weekly' },
    clusters: [
      { label: 'A', rep: a1, members: [a1, a2, a3] },
      { label: 'B', rep: b1, members: [b1, b2] },
    ],
  };
  const answerRevealEntries = evaluateDeclaration(app, 'function answerRevealEntries()', {
    cur,
    clustered: true,
    answerViewPoseCorrect: choice => choice.correct,
    sameChoice: (left, right) => left.id === right.id,
  });

  const entries = answerRevealEntries();

  assert.deepEqual(JSON.parse(JSON.stringify(entries.map(entry => ({
    label: entry.choice.label,
    cluster: entry.cluster.label,
    exact: entry.exact,
    clusterAccepted: entry.clusterAccepted,
    grouped: entry.grouped,
    count: entry.memberCount,
    members: entry.members.map(member => member.label),
  })))), [
    { label: 'A-1', cluster: 'A', exact: true, clusterAccepted: false,
      grouped: false, count: 1, members: ['A-1'] },
    { label: 'A-2', cluster: 'A', exact: true, clusterAccepted: false,
      grouped: false, count: 1, members: ['A-2'] },
    { label: 'A-3', cluster: 'A', exact: false, clusterAccepted: true,
      grouped: false, count: 1, members: ['A-3'] },
    { label: 'B-1', cluster: 'B', exact: false, clusterAccepted: false,
      grouped: true, count: 2,
      members: ['B-1', 'B-2'] },
  ]);
  const unclusteredEntries = evaluateDeclaration(app, 'function answerRevealEntries()', {
    cur,
    clustered: false,
    answerViewPoseCorrect: choice => choice.correct,
    sameChoice: (left, right) => left.id === right.id,
  })();
  assert.deepEqual(
    JSON.parse(JSON.stringify(unclusteredEntries.map(entry => ({
      label: entry.choice.label,
      grouped: entry.grouped,
      count: entry.memberCount,
    })))),
    [
      { label: 'A-1', grouped: false, count: 1 },
      { label: 'A-2', grouped: false, count: 1 },
      { label: 'A-3', grouped: false, count: 1 },
      { label: 'B-1', grouped: false, count: 1 },
      { label: 'B-2', grouped: false, count: 1 },
    ],
  );
  assert.match(app, /answer-choice-status/);
  const renderRevealListSource = app.slice(
    app.indexOf('function renderRevealList(picked, af3)'),
    app.indexOf('function answerRevealEntries()'),
  );
  assert.doesNotMatch(renderRevealListSource, /Exact correct|Incorrect/);
});

test('applyAnswerRevealView auto-focuses only One at a time and leaves Grid/Show all context-free', async () => {
  const app = await read('app.js');
  const calls = [];
  const sandbox = {
    cur: { contextChoice: null, poseFocusChoice: null, answerRevealBest: null },
    releasedCrystalMode: true,
    showXtal: true,
    releasedCrystalError: 'x',
    clustered: true,
    displayMode: 'grid',
    shownOne: 2,
    resetCameraOnNextBuild: false,
    syncXtalRow: () => calls.push('syncXtalRow'),
    sameChoice: (a, b) => a === b,
    bestRawCorrectPose: choices => choices.find(choice => choice.correct) || null,
    allItemChoices: () => sandbox.choices,
    visibleChoices: () => sandbox.choices,
    choices: [{ correct: true, rmsd: 1.1, label: 'A' }, { correct: false, rmsd: 4.0, label: 'B' }],
  };
  const applyAnswerRevealView = evaluateDeclaration(app, 'function applyAnswerRevealView()', sandbox);
  applyAnswerRevealView();
  assert.equal(sandbox.displayMode, 'grid');
  assert.equal(sandbox.clustered, true);
  assert.equal(sandbox.shownOne, 2);
  assert.equal(sandbox.cur.contextChoice, null);
  assert.equal(sandbox.cur.poseFocusChoice, null);
  assert.equal(sandbox.releasedCrystalMode, false);
  assert.equal(sandbox.resetCameraOnNextBuild, true);

  sandbox.displayMode = 'one';
  sandbox.choices = [{ correct: true, rmsd: 0.9, label: 'A' }, { correct: false, rmsd: 4.0, label: 'B' }];
  applyAnswerRevealView();
  assert.equal(sandbox.displayMode, 'one');
  assert.equal(sandbox.shownOne, 0);
  assert.equal(sandbox.cur.contextChoice.label, 'A');

  sandbox.displayMode = 'all';
  applyAnswerRevealView();
  assert.equal(sandbox.cur.contextChoice, null);
  assert.equal(sandbox.cur.poseFocusChoice, null);

  sandbox.displayMode = 'one';
  sandbox.choices = [{ correct: false, rmsd: 4.0, label: 'B' }];
  applyAnswerRevealView();
  assert.equal(sandbox.displayMode, 'one');
  assert.equal(sandbox.shownOne, 0);
  assert.equal(sandbox.cur.contextChoice, null);
});

test('weekly answer rendering keeps only the focused correct pose in one-at-a-time mode', async () => {
  const app = await read('app.js');
  const choices = [{ label: 'A' }, { label: 'B' }];
  let rendered = null;
  const buildSingleLayer = evaluateDeclaration(app, 'async function buildSingleLayer(preserveCamera = true)', {
    cur: { revealed: true, showAnswer: true, item: {} },
    visibleChoices: () => choices,
    retrospectiveNavChoices: () => choices,
    shownOne: 1,
    displayMode: 'one',
    weeklyResultsRevealActive: () => true,
    buildCanonicalLayer: async (shown, preserve) => { rendered = { shown, preserve }; },
  });
  await buildSingleLayer(false);
  assert.equal(rendered.preserve, false);
  assert.equal(rendered.shown.length, 1);
  assert.equal(rendered.shown[0].label, 'B');
});

test('private retrospective supports xtal and folded protein frames', async () => {
  const app = await read('app.js');
  assert.match(app, /cur\.item\.answer_overlay\?\.crystal_ligand_pdb/);
  assert.match(app, /await addCrystalPose\(crystal\.struct, targetPlugin\)/);
  assert.match(app, /{ name: 'model', params: {} }/);
  assert.match(app, /answer_overlay_pdb/);
  assert.match(app, /answer_crystal_pdb/);
  assert.match(app, /buildRetrospectiveCanonicalLayer/);
  assert.match(app, /buildRetrospectiveGridCell/);
  assert.match(app, /retrospectiveAnswerActive\(\)/);
  assert.match(app, /cell\.spec\.answer && crystalFrame/);
  assert.match(app, /answerViewPoseCorrect\(layer\.choice\) \? GOOD : BAD/);
  assert.match(app, /retrospectiveProteinFrame === 'folded'/);
  assert.match(app, /retrospectiveGridProteinFrames\.get\(retrospectiveChoiceKey\(entry\.choice\)\)/);
  assert.match(app, /setRetrospectiveGridProteinFrame\(entry\.choice, 'folded'\)/);
  assert.match(app, /retrospectiveGridProteinFrames\.set\(key, frame\)/);
  assert.match(app, /resetCameraOnNextBuild = true/);
  assert.match(app, /function refreshGridCameraSync\(\)[\s\S]*syncGridCameras\(active\)/);
  assert.match(app, /const canInspect = !locked\(\) \|\| \(answerActive && isRetrospectiveReview\(\)\)/);
  assert.match(app, /locked\(\) && !cell\.spec\.retrospectiveReview/);
  assert.match(app, /retrospectiveReview: isRetrospectiveReview\(\)/);
  assert.match(app, /select\.textContent = 'Xtal'/);
  assert.match(app, /reject\.textContent = 'Folded'/);
  assert.doesNotMatch(app, /textContent = '(?:Xtal|Folded) protein'/);
  assert.match(app, /addRetrospectiveCrystalPocketSticks/);
  assert.match(app, /rigidPdbTransform\(sourcePosePdb, choice\.answer_overlay_pdb\)/);
  assert.match(app, /buildRetrospectiveFoldedGridCell/);
  assert.match(app, /buildRetrospectiveFoldedCanonicalLayer/);
  assert.match(app, /urls\.pocket \? fetchPdbText\(urls\.pocket\)/);
  assert.match(app, /pocketPdb: pocketPdb \? transformPdbCoordinates\(pocketPdb, transform\)/);
  assert.match(app, /choice\.answer_crystal_pdb,\s+5,/);
  const xtalGridSource = app.slice(
    app.indexOf('async function buildRetrospectiveXtalGridCell(cell)'),
    app.indexOf('async function buildRetrospectiveGridCell(cell, c)'),
  );
  assert.equal((xtalGridSource.match(/molecular-surface/g) || []).length, 0,
    'the shared crystal context already owns the Xtal ligand surface');
});

test('retrospective Grid protein toggle rebuilds only its existing card viewer', async () => {
  const app = await read('app.js');
  const choice = { id: 'pose-a' };
  const calls = [];
  const classes = new Set();
  const cell = {
    entry: { choice },
    spec: { retrospectiveProteinFrame: 'xtal' },
    plugin: {
      clear: async () => { calls.push('clear'); },
    },
    card: {
      classList: {
        add: name => classes.add(name),
        remove: name => classes.delete(name),
      },
    },
    disposed: false,
    failed: false,
    sceneRevision: 0,
  };
  const frames = new Map();
  const setFrame = evaluateDeclaration(
    app,
    'async function setRetrospectiveGridProteinFrame(choice, frame)',
    {
      retrospectiveChoiceKey: () => 'item|pose-a',
      retrospectiveAnswerActive: () => true,
      displayMode: 'grid',
      isFixedReferenceChoice: () => false,
      viewerControlBlocked: () => false,
      gridViewers: [cell],
      sameChoice: (left, right) => left.id === right.id,
      retrospectiveGridProteinFrames: frames,
      gridBuildRevision: 7,
      viewerTransitionBusy: false,
      setViewerControlsBusy: busy => calls.push(`busy:${busy}`),
      stopGridCameraSync: null,
      syncGridFrameControls: () => calls.push('sync-controls'),
      populateGridCell: async current => {
        assert.equal(current, cell);
        calls.push('populate-one');
      },
      holdCameraSnapshot: () => () => {},
      pinCameraSnapshot: async () => {},
      refreshGridCameraSync: () => calls.push('camera-sync'),
      console,
    },
  );

  await setFrame(choice, 'folded');

  assert.equal(frames.get('item|pose-a'), 'folded');
  assert.equal(cell.spec.retrospectiveProteinFrame, 'folded');
  assert.deepEqual(calls, [
    'busy:true', 'sync-controls', 'clear', 'populate-one',
    'camera-sync', 'busy:false',
  ]);
  assert.equal(classes.has('loading-frame'), false);
});

test('retrospective One at a time protein toggle preserves camera and redraws immediately', async () => {
  const app = await read('app.js');
  const camera = { position: [1, 2, 3], target: [4, 5, 6] };
  const calls = [];
  const sandbox = {
    retrospectiveAnswerActive: () => true,
    retrospectiveProteinFrame: 'xtal',
    viewerControlBlocked: () => false,
    oneReviewChoice: () => ({ id: 'pose-a' }),
    isFixedReferenceChoice: () => false,
    plugin: {
      canvas3d: {
        camera: { getSnapshot: () => camera },
        requestDraw: () => calls.push('draw'),
      },
    },
    currentProteinKey: 'old',
    nextCanonicalCameraSnapshot: null,
    resetCameraOnNextBuild: true,
    syncButtons: () => calls.push('sync'),
    nextAnimationFrame: async () => { calls.push('frame'); },
    renderUI: () => calls.push('render'),
    viewerRebuild: {
      enqueue: async (mutate, finalize) => {
        await mutate();
        calls.push('rebuild');
        await finalize();
      },
    },
  };
  const setFrame = evaluateDeclaration(
    app,
    'async function setRetrospectiveProteinFrame(frame)',
    sandbox,
  );

  await setFrame('folded');

  assert.equal(sandbox.retrospectiveProteinFrame, 'folded');
  assert.equal(sandbox.nextCanonicalCameraSnapshot, camera);
  assert.equal(sandbox.resetCameraOnNextBuild, false);
  assert.deepEqual(calls, [
    'sync', 'rebuild', 'draw', 'frame', 'draw', 'render', 'sync',
  ]);
});

test('weeklyPoseLayers keeps cluster ghosts during retrospective answer view', async () => {
  const app = await read('app.js');
  const memberA = { id: 'a', label: 'A' };
  const memberB = { id: 'b', label: 'B' };
  const rep = memberA;
  const cluster = { members: [memberA, memberB], rep: memberA };
  const weeklyPoseLayers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', {
    cur: {
      item: { source: 'weekly' },
      revealed: true,
      showAnswer: true,
      clusters: [cluster],
    },
    quizSource: 'weekly',
    WEEKLY_ROUND: { public_status: 'revealed' },
    clustered: true,
    displayMode: 'one',
    clusterForChoice: choice => cluster,
    sameChoice: (left, right) => left === right,
    choiceRejected: () => false,
    weeklyResultsRevealActive: () => true,
    retrospectiveAnswerActive: () => true,
    isPrivatePrecloseReview: () => true,
    isFixedReferenceChoice: () => false,
  });
  const layers = weeklyPoseLayers([rep]);
  assert.equal(layers.length, 2);
  assert.equal(layers[0].ghost, true);
  assert.equal(layers[1].ghost, false);
  assert.equal(layers[1].choice, rep);

  cluster.rep = rep;
  const focused = memberB;
  const focusedContext = {
    item: { source: 'weekly' },
    revealed: true,
    showAnswer: true,
    clusters: [cluster],
    contextChoice: focused,
  };
  const focusedLayers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', {
    cur: focusedContext,
    clustered: true,
    displayMode: 'all',
    clusterForChoice: () => cluster,
    sameChoice: (left, right) => left === right,
    choiceRejected: () => false,
    retrospectiveAnswerActive: () => true,
    isFixedReferenceChoice: () => false,
  })([rep]);
  assert.equal(focusedLayers.length, 1);
  assert.equal(focusedLayers[0].choice, rep);
  assert.equal(focusedLayers[0].ghost, false);
});

test('finalizeReveal no longer auto-enables releasedCrystalMode', async () => {
  const app = await read('app.js');
  assert.doesNotMatch(app, /isPrivatePrecloseReview\(\) && itemHasReleasedCrystal\(cur\.item\)[\s\S]*releasedCrystalMode = true/);
  assert.match(app, /weeklyResultsRevealActive\(\)[\s\S]*applyAnswerRevealView\(\)/);
});

test('released crystal toggle returns to the focused answer reveal view when unchecked', async () => {
  const app = await read('app.js');
  assert.match(
    app,
    /if \(!checked && cur\?\.revealed && cur\?\.showAnswer && weeklyResultsRevealActive\(\)\) \{\s*applyAnswerRevealView\(\);/,
  );
});

test('weekly result navigation restores revealed state and local scoring is item-idempotent', async () => {
  const app = await read('app.js');
  const loadQuestionSource = app.slice(
    app.indexOf('async function loadQuestion(i)'),
    app.indexOf('function renderUI()'),
  );
  assert.match(loadQuestionSource, /restoreWeeklyResult = !!\(savedWeeklyState\?\.revealed/);
  assert.match(loadQuestionSource, /if \(!restoreWeeklyResult\) \{\s*cur\.revealed = false/);
  assert.match(loadQuestionSource, /if \(restoreWeeklyResult && cur\.showAnswer\) \{\s*applyAnswerRevealView\(\)/);
  assert.match(loadQuestionSource, /renderRevealedQuestionUi\(\)/);
  assert.match(loadQuestionSource, /wrap\.classList\.add\('question-loading'\)[\s\S]*\$\('#choices'\)\.replaceChildren\(\)/);
  assert.match(loadQuestionSource, /finally \{\s*wrap\.classList\.remove\('question-loading'\)/);
  const renderUiSource = app.slice(
    app.indexOf('function renderUI()'),
    app.indexOf('// dev-only chrome'),
  );
  assert.match(renderUiSource, /box\.style\.display = cur\.revealed && cur\.showAnswer \? 'none' : ''/);

  const localWeeklyScoredItems = new Set();
  const localWeeklyScore = { correct: 0, answered: 0 };
  const bumpLocalWeeklyScore = evaluateDeclaration(app, 'function bumpLocalWeeklyScore(youRight)', {
    weeklyResultsRevealActive: () => true,
    cur: { item: { id: '9XYZ' } },
    localWeeklyScoredItems,
    localWeeklyScore,
    renderWeeklyLeaderboard: () => {},
  });
  bumpLocalWeeklyScore(true);
  bumpLocalWeeklyScore(true);
  assert.deepEqual(localWeeklyScore, { correct: 1, answered: 1 });
});

test('weekly named sessions opt into leaderboard identity in initial app state', async () => {
  const app = await read('app.js');
  assert.match(app, /leaderboard_opt_in: true/);
  assert.match(app, /leaderboard_name_version: 1/);
  assert.match(app, /play_mode: postReveal \? 'for_fun' : 'blind_competitive'/);
  assert.match(app, /play_mode_version: 1/);
  assert.match(app, /initialAppState: quizSource === 'weekly'/);
});

test('index exposes leaderboard name copy and scorecard shell', async () => {
  const [html, app] = await Promise.all([read('index.html'), read('app.js')]);
  assert.match(html, /Player name/);
  assert.match(html, /Shown on the results leaderboard after release/);
  assert.match(html, /id="weekly-leaderboard"/);
  assert.match(html, /app\.js\?v=2026090107/);
  assert.match(html, /id="weekly-results-heading"/);
  assert.match(app, /fetch\('\/api\/weekly-retrospectives\?limit=50'\)/);
  assert.doesNotMatch(app, /void loadWeeklySelectorResults\(\)/);
  assert.match(app, /Published results are temporarily unavailable/);
  assert.match(html, /\.grid-head\{[^}]*width:calc\(100% - 16px\)[^}]*overflow:hidden[^}]*white-space:nowrap/);
  assert.match(html, /\.grid-meta\{[^}]*flex:0 1 auto[^}]*text-align:left[^}]*text-overflow:ellipsis/);
  assert.match(html, /\.pose-info\{[^}]*flex:none[^}]*margin-left:auto/);
  assert.match(html, /#answer-choices \.answer-choice-count\{[^}]*color:var\(--muted\)[^}]*font-weight:500/);
  assert.doesNotMatch(html, /#answer-choices \.answer-choice-count\{[^}]*background:/);
  assert.match(html, /#wrap\.question-loading #choices,[\s\S]*#wrap\.question-loading #answer-details\{display:none!important\}/);
  assert.match(html, /id="stage-topbar"[\s\S]*id="viewer-question"[\s\S]*id="badge"/);
  assert.match(html, /\.badge\{[^}]*max-width:min\(680px,60%\)[^}]*overflow:visible[^}]*white-space:normal[^}]*overflow-wrap:anywhere/);
  assert.match(html, /\.grid-review-actions\[hidden\]\{display:none\}/);
  assert.match(html, /\.weekly-question-result-answer\{[^}]*grid-template-columns:20px minmax\(0,1fr\) 52px 72px[^}]*height:42px[^}]*box-sizing:border-box/);
  assert.match(html, /\.weekly-question-result-answer>span:last-child\{[^}]*white-space:nowrap[^}]*text-align:right/);
  assert.match(html, /\.weekly-question-result-correct\.cluster-accepted\{color:var\(--gold\)\}/);
  assert.match(html, /\.weekly-question-result-correct\.wrong\{color:var\(--bad\)\}/);
  assert.doesNotMatch(html, /id="answer-summary-banner"/);
  assert.match(html, /#answer-details\[data-private-review="true"\] #answer-choices \.choice\{[^}]*min-height:50px[^}]*border-left:5px solid var\(--choice-color\)[^}]*background:#fff/);
  assert.match(html, /#answer-details\[data-private-review="true"\] #answer-choices \.choice\.correct\{[^}]*border:2px solid var\(--good\)[^}]*border-left-width:5px/);
  assert.match(html, /#answer-details\[data-private-review="true"\] #answer-choices \.choice\.cluster-accepted\{[^}]*border-color:var\(--line\)[^}]*border-left-color:var\(--gold\)[^}]*background:#fff/);
});

test('renderWeeklyLeaderboard renders complete and partial sections from API data', async () => {
  const app = await read('app.js');
  const host = { hidden: true, innerHTML: '', replaceChildren() {} };
  const elements = { '#weekly-leaderboard': host };
  const escapeLeaderboardText = evaluateDeclaration(
    app,
    'function escapeLeaderboardText(value)',
    {},
  );
  const formatWeeklyScoreLine = evaluateDeclaration(
    app,
    'function formatWeeklyScoreLine({ displayName, correct, answered, total, accuracy, coverage, rank = null })',
    { escapeLeaderboardText },
  );
  const sandbox = {
    WEEKLY_ONLY: true,
    WEEKLY_ROUND: { public_status: 'revealed' },
    WEEKLY_LEADERBOARD: sampleLeaderboard(),
    WEEKLY_FOR_FUN_LEADERBOARD: {
      complete_runs: [{
        display_name: 'Grace',
        correct: 2,
        answered: 2,
        total: 2,
        accuracy: 100,
        coverage: 100,
        participation_mode: 'for_fun',
        rank: 1,
      }],
      partial_runs: [],
    },
    WEEKLY_LEADERBOARD_ERROR: '',
    ITEMS: [{}, {}],
    participantDisplayName: 'Reviewer',
    localWeeklyScore: { correct: 2, answered: 3 },
    isPrivatePrecloseReview: () => false,
    isArchiveRetrospective: () => false,
    isRetrospectiveReview: () => false,
    formatWeeklyScoreLine,
    $: selector => elements[selector] || null,
  };
  const renderWeeklyLeaderboard = evaluateDeclaration(app, 'function renderWeeklyLeaderboard()', sandbox);
  renderWeeklyLeaderboard();
  assert.equal(host.hidden, false);
  assert.match(host.innerHTML, /Leaderboard/);
  assert.match(host.innerHTML, /Other players/);
  assert.match(host.innerHTML, /Claude Opus/);
  assert.match(host.innerHTML, /Codex GPT-5\.6/);
  assert.match(host.innerHTML, /Reviewer/);
  assert.match(host.innerHTML, /Grace · For fun/);
  assert.match(host.innerHTML, /Separate from the blind-week ranking/);
  assert.doesNotMatch(host.innerHTML, /% cov|local, not ranked/);
  assert.doesNotMatch(host.innerHTML, /#1 · <b>Claude Opus<\/b>/);
});

test('revealed Weekly derives human and automated scores from retrospective summaries', async () => {
  const app = await read('app.js');
  const publication = {
    round_id: 'weekly-test',
    item_count: 2,
    summary: {
      human_entries: [{
        participant: 'Ada',
        correct: 1,
        answered: 1,
        total: 2,
        accuracy: 100,
        coverage: 50,
        complete: false,
      }],
      automated_entries: [
        { participant: 'Claude Opus', correct: 1, total: 2, accuracy: 50 },
        { participant: 'Smina', correct: 0, total: 2, accuracy: 0 },
      ],
    },
  };
  const leaderboardFromSummary = evaluateDeclaration(
    app,
    'function weeklyLeaderboardFromRetrospectiveSummary(publication)',
    { WEEKLY_ROUND: { round_id: 'weekly-test' } },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(leaderboardFromSummary(publication))),
    {
      format_version: 'foldarium.weekly-leaderboard/v1',
      round_id: 'weekly-test',
      item_count: 2,
      participant_count: 1,
      complete_runs: [],
      partial_runs: [{
        display_name: 'Ada',
        correct: 1,
        answered: 1,
        total: 2,
        accuracy: 100,
        coverage: 50,
      }],
    },
  );

  const host = { hidden: true, innerHTML: '', replaceChildren() { this.innerHTML = ''; } };
  const escapeSelectorText = evaluateDeclaration(
    app,
    'function escapeSelectorText(value)',
    {},
  );
  const renderAutomated = evaluateDeclaration(
    app,
    'function renderWeeklySelectorLeaderboard()',
    {
      WEEKLY_ONLY: true,
      WEEKLY_ROUND: { public_status: 'revealed' },
      WEEKLY_RETROSPECTIVE_SUMMARY: publication.summary,
      escapeSelectorText,
      $: selector => selector === '#weekly-selector-leaderboard' ? host : null,
    },
  );
  renderAutomated();
  assert.equal(host.hidden, false);
  assert.match(host.innerHTML, /Automated methods/);
  assert.match(host.innerHTML, /Claude Opus<\/b> · 1\/2 correct/);
  assert.match(host.innerHTML, /Smina<\/b> · 0\/2 correct/);
  assert.doesNotMatch(host.innerHTML, /%/);
});

test('revealed Weekly login omits a zeroed local session and reports no human players', async () => {
  const app = await read('app.js');
  const host = { hidden: true, innerHTML: '', replaceChildren() {} };
  const wrap = { classList: { contains: value => value === 'intro' } };
  const escapeLeaderboardText = evaluateDeclaration(
    app,
    'function escapeLeaderboardText(value)',
    {},
  );
  const formatWeeklyScoreLine = evaluateDeclaration(
    app,
    'function formatWeeklyScoreLine({ displayName, correct, answered, total, accuracy, coverage, rank = null })',
    { escapeLeaderboardText },
  );
  const render = evaluateDeclaration(app, 'function renderWeeklyLeaderboard()', {
    WEEKLY_ONLY: true,
    WEEKLY_ROUND: { public_status: 'revealed' },
    WEEKLY_LEADERBOARD: {
      format_version: 'foldarium.weekly-leaderboard/v1',
      round_id: 'weekly-test',
      item_count: 39,
      participant_count: 0,
      complete_runs: [],
      partial_runs: [],
    },
    WEEKLY_FOR_FUN_LEADERBOARD: null,
    WEEKLY_LEADERBOARD_ERROR: '',
    ITEMS: Array(39).fill({}),
    participantDisplayName: '',
    localWeeklyScore: { correct: 0, answered: 0 },
    isPrivatePrecloseReview: () => false,
    isArchiveRetrospective: () => false,
    formatWeeklyScoreLine,
    $: selector => selector === '#weekly-leaderboard' ? host
      : selector === '#wrap' ? wrap : null,
  });
  render();
  assert.match(host.innerHTML, /No human players participated this week/);
  assert.doesNotMatch(host.innerHTML, /Your session|0\/0/);
});

test('revealed Weekly records answer-informed votes before showing results', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  const revealSource = app.slice(
    app.indexOf('async function reveal()'),
    app.indexOf('function renderRevealedQuestionUi()'),
  );
  assert.match(revealSource, /quizSource === 'weekly' && !isRetrospectiveReview\(\) && !isReadOnlyPreview\(\)/);
  assert.match(revealSource, /finalizeWeeklyVote\(\{ postReveal: postRevealVote \}\)/);
  assert.match(revealSource, /if \(!saved \|\| !postRevealVote\) return/);
  assert.match(app, /voteComment: cur\.voteCommentText,\s*postReveal,/);
  assert.match(app, /postReveal: WEEKLY_ROUND\.public_status === 'revealed'/);
  assert.match(app, /const postReveal = quizSource === 'weekly'[\s\S]*WEEKLY_ROUND\?\.public_status === 'revealed'/);
  assert.match(app, /postReveal,/);
  assert.match(app, /Submit for-fun answer/);
  assert.match(app, /Post-reveal vote recorded separately from blind-week results/);
  assert.match(app, /loadWeeklyPlayForFunLeaderboard\(\)\.then\(renderWeeklyLeaderboard\)/);
  assert.match(
    app,
    /revisableForFunSession = quizSource === 'weekly'[\s\S]*public_status === 'revealed'[\s\S]*if \(!revisableForFunSession\) researchBackend\(\)\?\.completeSession/,
  );
  assert.match(html, /\.post-reveal-vote-note\{/);
});

test('private retrospective renders compact per-question popularity with names behind info', async () => {
  const app = await read('app.js');
  const choices = [
    { id: 'choice-a', _weeklyChoiceId: 'choice-a', label: 'E-1', clusterAccepted: true },
    { id: 'choice-b', _weeklyChoiceId: 'choice-b', label: 'C-2', clusterAccepted: true },
  ];
  const privateQuestionAnswerChoice = evaluateDeclaration(
    app,
    'function privateQuestionAnswerChoice(answer)',
    { allItemChoices: () => choices },
  );
  const privateQuestionAnswerLabel = evaluateDeclaration(
    app,
    'function privateQuestionAnswerLabel(answer)',
    {
      privateQuestionAnswerChoice,
      displayedPoseLabel: choice => choice.label,
      clusterForChoice: choice => ({ label: choice.label.split('-')[0] }),
    },
  );
  const privateQuestionAnswerState = evaluateDeclaration(
    app,
    'function privateQuestionAnswerState(answer)',
    { privateQuestionAnswerChoice },
  );
  const escapeLeaderboardText = evaluateDeclaration(
    app,
    'function escapeLeaderboardText(value)',
    {},
  );
  assert.equal(
    privateQuestionAnswerLabel({
      choice_id: 'choice-a', picked_none: false, selection_kind: 'unknown',
    }),
    'Pose E-1',
  );
  const renderPrivateQuestionResult = evaluateDeclaration(
    app,
    'function renderPrivateQuestionResult(result)',
    { escapeLeaderboardText, privateQuestionAnswerLabel, privateQuestionAnswerState },
  );
  const result = {
    item_id: 'ITEM01',
    answered_count: 4,
    correct_count: 2,
    correct_display_names: ['Ada', '<Grace>'],
    answers: [{
      choice_id: 'choice-a',
      picked_none: false,
      selection_kind: 'cluster',
      correct: true,
      vote_count: 2,
      display_names: ['Ada', '<Grace>'],
    }, {
      choice_id: 'choice-b',
      picked_none: false,
      selection_kind: 'exact',
      correct: false,
      vote_count: 1,
      display_names: ['Lin'],
    }, {
      choice_id: null,
      picked_none: true,
      selection_kind: 'none',
      correct: false,
      vote_count: 1,
      display_names: ['Claude'],
    }],
  };
  const host = { hidden: true, innerHTML: '', replaceChildren() {} };
  const renderWeeklyLeaderboard = evaluateDeclaration(
    app,
    'function renderWeeklyLeaderboard()',
    {
      WEEKLY_ONLY: true,
      WEEKLY_ROUND: { public_status: 'open' },
      isPrivatePrecloseReview: () => true,
      isArchiveRetrospective: () => false,
      isRetrospectiveReview: () => true,
      privateQuestionResult: () => result,
      renderPrivateQuestionResult,
      $: selector => selector === '#weekly-leaderboard' ? host : null,
    },
  );
  renderWeeklyLeaderboard();
  assert.match(host.innerHTML, /2\/4/);
  assert.match(host.innerHTML, /players got this question right/);
  assert.match(host.innerHTML, /Most popular answers/);
  assert.match(host.innerHTML, /Cluster E/);
  assert.match(host.innerHTML, /Pose C-2/);
  assert.match(host.innerHTML, /None are correct/);
  assert.match(host.innerHTML, /weekly-question-result-correct cluster-accepted">correct/);
  assert.match(host.innerHTML, /weekly-question-result-correct wrong">wrong/);
  assert.match(host.innerHTML, /Show player names/);
  assert.match(host.innerHTML, />Players<\/summary>/);
  assert.match(host.innerHTML, /&lt;Grace&gt;/);
  assert.doesNotMatch(host.innerHTML, /Ensemble result|Complete runs|Signal for/);
});

test('archive question results label both correct and wrong automated answers', async () => {
  const app = await read('app.js');
  const renderArchiveQuestionResult = evaluateDeclaration(
    app,
    'function renderArchiveQuestionResult(result)',
    {
      privateQuestionAnswerState: answer => answer.correct ? 'correct' : '',
      privateQuestionAnswerLabel: answer => answer.label,
      escapeLeaderboardText: value => String(value),
    },
  );
  const rendered = renderArchiveQuestionResult({
    human_aggregate: { answered_count: 0, correct_count: 0, answers: [] },
    automated_entries: [
      { participant: 'Claude Opus', label: 'Pose I', correct: true },
      { participant: 'GPT-5.6 Sol', label: 'Pose J', correct: false },
    ],
  });

  assert.match(rendered, /Claude Opus[\s\S]*weekly-question-result-correct correct">correct/);
  assert.match(rendered, /GPT-5\.6 Sol[\s\S]*weekly-question-result-correct wrong">wrong/);
});

test('weekly leaderboard escapes participant names before rendering HTML', async () => {
  const app = await read('app.js');
  const escapeLeaderboardText = evaluateDeclaration(
    app,
    'function escapeLeaderboardText(value)',
    {},
  );
  const formatWeeklyScoreLine = evaluateDeclaration(
    app,
    'function formatWeeklyScoreLine({ displayName, correct, answered, total, accuracy, coverage, rank = null })',
    { escapeLeaderboardText },
  );
  const rendered = formatWeeklyScoreLine({
    displayName: '<img src=x onerror=alert(1)>',
    correct: 1,
    answered: 2,
    total: 29,
    accuracy: 50,
    coverage: 6.9,
  });
  assert.doesNotMatch(rendered, /<img/);
  assert.match(rendered, /&lt;img/);
  assert.match(rendered, /1\/2 correct/);
  assert.doesNotMatch(rendered, /%|cov/);
});

test('validateWeeklyLeaderboard rejects forbidden identity and answer fields recursively', () => {
  const valid = sampleLeaderboard();
  assert.doesNotThrow(() => validateWeeklyLeaderboard(valid));

  const withUserId = structuredClone(valid);
  withUserId.complete_runs[0].user_id = 'secret';
  assert.throws(
    () => validateWeeklyLeaderboard(withUserId),
    /forbidden field \(weekly_leaderboard\.complete_runs\[0\]\.user_id\)/,
  );

  const withChoice = structuredClone(valid);
  withChoice.partial_runs[0].choice_id = 'choice-a';
  assert.throws(
    () => validateWeeklyLeaderboard(withChoice),
    /forbidden field/,
  );

  const partialWithRank = structuredClone(valid);
  partialWithRank.partial_runs[0].rank = 9;
  assert.throws(
    () => validateWeeklyLeaderboard(partialWithRank),
    /unexpected field \(rank\)/,
  );
});

test('quiz-backend getWeeklyResults fetches the public weekly-results endpoint', async () => {
  const payload = sampleLeaderboard();
  const fetchCalls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    fetchCalls.push(String(url));
    return { ok: true, json: async () => payload };
  };
  const backend = createQuizBackend({
    client: {},
    getClient: async () => ({
      auth: {
        getSession: async () => ({ data: { session: null }, error: null }),
        signInAnonymously: async () => ({ data: { user: { id: 'u1' } }, error: null }),
      },
      rpc: async () => ({ data: null, error: null }),
      from: () => ({
        upsert: async () => ({ error: null }),
        update: () => ({ eq: () => ({ eq: async () => ({ error: null }) }) }),
      }),
    }),
    storage: {
      length: 0,
      key: () => null,
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    },
  });
  try {
    const rows = await backend.getWeeklyResults(ALLOWED_ROUND_ID);
    assert.deepEqual(rows, payload);
    assert.equal(fetchCalls.length, 1);
    assert.match(fetchCalls[0], /\/api\/weekly-results\?round_id=/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('deferred backend forwards getWeeklyResults after attach', async () => {
  const deferred = createDeferredBackend();
  const expected = sampleLeaderboard();
  const pending = deferred.getWeeklyResults(ALLOWED_ROUND_ID);
  deferred.attach({
    getWeeklyResults: async () => expected,
  });
  assert.deepEqual(await pending, expected);
});

test('answerViewPoseCorrect uses raw pose correctness during weekly answer view', async () => {
  const app = await read('app.js');
  const best = { correct: true, rmsd: 0.8, label: 'A' };
  const anotherCorrect = { correct: true, rmsd: 1.1, label: 'C' };
  const accepted = { correct: false, clusterAccepted: true, rmsd: 1.2, label: 'B' };
  const answerViewPoseCorrect = evaluateDeclaration(app, 'function answerViewPoseCorrect(choice)', {
    cur: {
      revealed: true,
      showAnswer: true,
      item: { source: 'weekly' },
      answerRevealBest: best,
    },
    acceptedChoiceCorrect: choice => choice.clusterAccepted === true,
    rawChoiceCorrect: choice => choice.correct === true,
  });
  assert.equal(answerViewPoseCorrect(best), true);
  assert.equal(answerViewPoseCorrect(anotherCorrect), true);
  assert.equal(answerViewPoseCorrect(accepted), false);
});

test('retrospective removes the answer banner and puts RMSD on sidebar buttons', async () => {
  const app = await read('app.js');
  const html = await read('index.html');
  assert.doesNotMatch(app, /syncAnswerSummaryBanner|answer-summary-banner/);
  assert.doesNotMatch(html, /answer-summary-banner/);
  assert.match(app, /<span class="rmsd answer-rmsd">RMSD \$\{c\.rmsd\.toFixed\(2\)\} Å<\/span>/);
  assert.match(html, /#answer-details\[data-private-review="true"\] #answer-choices \.rmsd\{[^}]*padding:5px 8px[^}]*background:var\(--choice-color\)[^}]*color:#fff[^}]*font-size:13px/);
});

test('retrospective keeps deployed non-pose-specific badges hidden outside One at a time', async () => {
  const app = await read('app.js');
  const badge = { style: {}, textContent: '' };
  const sandbox = {
    DEV: false,
    showXtal: false,
    displayMode: 'grid',
    $: selector => selector === '#badge' ? badge : null,
    isPrivatePrecloseReview: () => true,
    itemHasReleasedCrystal: () => true,
    retrospectiveAnswerActive: () => true,
    viewingReleasedCrystal: () => false,
  };
  const syncStageBadge = evaluateDeclaration(app, 'function syncStageBadge()', sandbox);

  syncStageBadge();
  assert.equal(badge.style.display, 'none');
  sandbox.displayMode = 'all';
  syncStageBadge();
  assert.equal(badge.style.display, 'none');
});

test('retrospective One-at-a-time badge omits the protein-frame label', async () => {
  const app = await read('app.js');
  const badge = { style: {}, textContent: '' };
  const choice = { label: 'D', rmsd: 1.49 };
  const sandbox = {
    DEV: false,
    showXtal: false,
    displayMode: 'one',
    shownOne: 0,
    $: selector => selector === '#badge' ? badge : null,
    isPrivatePrecloseReview: () => true,
    itemHasReleasedCrystal: () => true,
    retrospectiveAnswerActive: () => true,
    viewingReleasedCrystal: () => false,
    retrospectiveNavChoices: () => [choice],
    isXtalReferenceChoice: () => false,
    isTrainingReferenceChoice: () => false,
    displayedPoseLabel: current => current.label,
    answerPoseStatus: () => 'Exact correct ✓',
    weeklyLigandPlddt: () => 'ligand pLDDT 72.0',
    weeklyHbondCount: () => 'H-bonds 4',
  };
  const syncStageBadge = evaluateDeclaration(app, 'function syncStageBadge()', sandbox);

  syncStageBadge();

  assert.equal(
    badge.textContent,
    'Pose D · 1.49 Å · Exact correct ✓ · ligand pLDDT 72.0 · H-bonds 4',
  );
  assert.doesNotMatch(badge.textContent, /folded protein|xtal protein/);
});

test('viewerControlBlocked keeps ordinary reveals locked but permits retrospective controls', async () => {
  const app = await read('app.js');
  assert.match(app, /const viewerControlBlocked = \(\) => viewerTransitionBusy \|\| revealRequested[\s\S]*retrospectiveAnswerActive\(\)/);
  assert.match(app, /if \(viewerControlBlocked\(\)\) return;/);
  assert.match(app, /if \(!choice \|\| interactionBlocked\(\)\) return;/);

  const match = app.match(/const viewerControlBlocked = \(\) => ([\s\S]+?);/);
  assert.ok(match, 'expected viewerControlBlocked declaration');
  const runGuard = sandbox => vm.runInContext(
    `(() => ${match[1]})()`,
    vm.createContext(sandbox),
  );

  assert.equal(runGuard({
    viewerTransitionBusy: false,
    revealRequested: false,
    locked: () => true,
    retrospectiveAnswerActive: () => false,
  }), true);
  assert.equal(runGuard({
    viewerTransitionBusy: false,
    revealRequested: false,
    locked: () => true,
    retrospectiveAnswerActive: () => true,
  }), false);
});

test('retrospective canonical and grid paths render surfaces and artifact-backed H-bonds', async () => {
  const app = await read('app.js');
  assert.match(app, /async function buildRetrospectiveHbonds\(/);
  assert.match(app, /async function buildRetrospectiveInteractions\(/);
  assert.match(app, /function mergeRetrospectiveInteractionPdb\(/);
  assert.match(app, /function relabelPdbRecords\(/);
  assert.match(app, /answer_crystal_pocket_pdb/);
  assert.match(app, /async function buildRetrospectiveXtalLayer\(/);
  assert.match(app, /function buildXtalReferenceChoice\(/);
  assert.match(app, /const hbondChoices = displayMode === 'all'[\s\S]*await buildRetrospectiveHbonds\(hbondChoices\)/);
  assert.match(app, /await addRetrospectiveCrystalPocketSticks\(xtal, plugin/);
  assert.match(app, /await addRetrospectiveCrystalPocketSticks\(c, cell\.plugin\)/);
  assert.match(app, /await addRep\(crystal\.struct, 'all', 'molecular-surface', XTAL/);
  assert.match(app, /buildRetrospectiveCanonicalLayer[\s\S]*if \(showSurface && !layer\.ghost\)/);
  assert.match(app, /cell\.spec\.showHbonds && itemHasReleasedCrystal\(cell\.spec\.item\)/);
  assert.doesNotMatch(app, /fetchRcsbPdbText/);
  assert.match(app, /Xtal reference/);
  assert.match(app, /experimental · not scored/);
  assert.doesNotMatch(app, /H-bonds compare prediction/);
});

test('Xtal reference uses the closest pose-specific crystal pocket', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  const buildXtalReferenceChoice = evaluateDeclaration(
    app,
    'function buildXtalReferenceChoice(item)',
    { XTAL: 0x8B5CF6 },
  );
  const choice = buildXtalReferenceChoice({
    answer_overlay: {
      crystal_ligand_pdb: 'authoritative crystal',
      poses: [
        { id: 'B', rmsd: 2.0, crystal_pocket_pdb: 'wrong pocket' },
        { id: 'A', rmsd: 0.5, crystal_pocket_pdb: 'closest pocket' },
      ],
    },
  });
  assert.equal(choice.answer_crystal_pdb, 'authoritative crystal');
  assert.equal(choice.answer_crystal_pocket_pdb, 'closest pocket');
  assert.equal(choice.rmsd, 0);
  assert.match(app, /XTAL = 0x8B5CF6/);
  assert.match(app, /const XTAL_POSE_SIZE = 0\.32/);
  assert.match(app, /const addCrystalPose = \(struct, targetPlugin = plugin\) => addPose\([\s\S]*sizeFactor: XTAL_POSE_SIZE/);
  assert.match(html, /color:#8B5CF6[^>]*>\(true pose, violet\)/);
  assert.doesNotMatch(html, /grid-answer-status/);
  assert.doesNotMatch(app, /grid-answer-status|answerStatus/);
  assert.doesNotMatch(app, /magenta|C026D3/);
  assert.match(app, /else if \(xtalReference\) \{\s*nm = 'Xtal reference';\s*\} else if \(clustered\)/);
});

test('retrospective interaction ligands retain valid PDB columns after relabeling', async () => {
  const app = await read('app.js');
  const relabelPdbRecords = evaluateDeclaration(
    app,
    'function relabelPdbRecords(text, options = {})',
    {},
  );
  const source = 'HETATM    1 N1   LIG X   1      12.345 -67.890   0.125  1.00 20.00           N';
  const { text, nextSerial } = relabelPdbRecords(source, {
    chain: 'Q',
    residueName: 'XTL',
    residueNumber: 7,
    startSerial: 42,
  });
  assert.equal(text.slice(6, 11), '   42');
  assert.equal(text.slice(12, 16), 'N1  ');
  assert.equal(text.slice(17, 20), 'XTL');
  assert.equal(text[21], 'Q');
  assert.equal(text.slice(22, 26), '   7');
  assert.equal(Number.parseFloat(text.slice(30, 38)), 12.345);
  assert.equal(Number.parseFloat(text.slice(38, 46)), -67.89);
  assert.equal(Number.parseFloat(text.slice(46, 54)), 0.125);
  assert.equal(text.slice(76, 78), ' N');
  assert.equal(nextSerial, 43);
});

test('folded proteins recover the evaluator rigid transform from aligned pose atoms', async () => {
  const app = await read('app.js');
  const pdbCoordinateRecords = evaluateDeclaration(app, 'function pdbCoordinateRecords(text)', {});
  const rigidPdbTransform = evaluateDeclaration(
    app,
    'function rigidPdbTransform(sourcePdb, targetPdb)',
    { pdbCoordinateRecords },
  );
  const transformPdbCoordinates = evaluateDeclaration(
    app,
    'function transformPdbCoordinates(text, transform)',
    {},
  );
  const line = (serial, name, [x, y, z], record = 'HETATM') => (
    `${record}${String(serial).padStart(5)} ${name.padEnd(4)} LIG X   1    `
    + `${x.toFixed(3).padStart(8)}${y.toFixed(3).padStart(8)}${z.toFixed(3).padStart(8)}`
    + '  1.00 20.00           C'
  );
  const rotate = ([x, y, z]) => [-y + 10, x + 20, z + 30];
  const sourcePoints = [[0, 0, 0], [4, 0, 0], [0, 3, 0], [0, 0, 2]];
  const source = sourcePoints.map((point, index) => line(index + 1, `C${index + 1}`, point)).join('\n');
  const target = sourcePoints.map(
    (point, index) => line(index + 1, `C${index + 1}`, rotate(point)),
  ).join('\n');
  const transform = rigidPdbTransform(source, target);
  const transformed = transformPdbCoordinates(line(1, 'CA', [2, 1, 1], 'ATOM  '), transform);

  assert.ok(transform.rmsd < 0.002);
  assert.deepEqual([
    Number.parseFloat(transformed.slice(30, 38)),
    Number.parseFloat(transformed.slice(38, 46)),
    Number.parseFloat(transformed.slice(46, 54)),
  ], [9, 22, 31]);
});

test('Xtal display sticks use the deployed residue-complete 5 A pocket policy', async () => {
  const app = await read('app.js');
  const pdbCoordinateRecords = evaluateDeclaration(app, 'function pdbCoordinateRecords(text)', {});
  const extractAlignedPocketPdb = evaluateDeclaration(
    app,
    'function extractAlignedPocketPdb(proteinPdb, ligandPdb, radiusAngstrom = 8)',
    { pdbCoordinateRecords },
  );
  const atom = ({ serial, name, residue, chain, residueNumber, xyz, element }) => (
    `ATOM  ${String(serial).padStart(5)} ${name.padEnd(4)} ${residue.padStart(3)} ${chain}`
    + `${String(residueNumber).padStart(4)}    `
    + `${xyz[0].toFixed(3).padStart(8)}${xyz[1].toFixed(3).padStart(8)}${xyz[2].toFixed(3).padStart(8)}`
    + `  1.00 20.00          ${element.padStart(2)}`
  );
  const ligand = 'HETATM    1 C1   LIG X   1       0.000   0.000   0.000  1.00  0.00           C';
  const protein = [
    atom({ serial: 1, name: 'CA', residue: 'ALA', chain: 'Y', residueNumber: 10,
      xyz: [4.9, 0, 0], element: 'C' }),
    atom({ serial: 2, name: 'CB', residue: 'ALA', chain: 'Y', residueNumber: 10,
      xyz: [12, 0, 0], element: 'C' }),
    atom({ serial: 3, name: 'H1', residue: 'ALA', chain: 'Y', residueNumber: 10,
      xyz: [4.8, 0, 0], element: 'H' }),
    atom({ serial: 4, name: 'CA', residue: 'GLY', chain: 'Y', residueNumber: 11,
      xyz: [5.1, 0, 0], element: 'C' }),
    atom({ serial: 5, name: 'N', residue: 'SER', chain: 'Z', residueNumber: 4,
      xyz: [0, 4, 0], element: 'N' }),
  ].join('\n');

  const pocket = extractAlignedPocketPdb(protein, ligand, 5);
  const lines = pocket.split('\n').filter(line => line.startsWith('ATOM'));

  assert.equal(lines.length, 3);
  assert.deepEqual(lines.map(line => line.slice(12, 16).trim()), ['CA', 'CB', 'N']);
  assert.deepEqual(lines.map(line => line[21]), ['A', 'A', 'B']);
});

test('interaction overlays render only ligand-to-pocket contacts', async () => {
  const app = await read('app.js');
  const calls = [];
  const targetPlugin = {
    builders: {
      data: {
        rawData: async options => {
          calls.push(['data', options]);
          return { ref: 'data' };
        },
      },
      structure: {
        parseTrajectory: async data => ({ data }),
        createModel: async traj => ({ traj }),
        createStructure: async model => ({ model }),
        tryCreateComponentStatic: async (_struct, selector) => {
          calls.push(['component', selector]);
          return { ref: 'ligand' };
        },
        representation: {
          addRepresentation: async (component, params) => {
            calls.push(['representation', component, params]);
          },
        },
      },
    },
  };
  const renderLigandInteractions = evaluateDeclaration(
    app,
    'async function renderLigandInteractions(pdb, targetPlugin = plugin, onData = null)',
    {},
  );

  await renderLigandInteractions('ATOM DATA', targetPlugin);

  assert.deepEqual(calls[1], ['component', 'ligand']);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[2])), [
    'representation',
    { ref: 'ligand' },
    {
      type: 'interactions',
      typeParams: { includeParent: true, parentDisplay: 'between' },
    },
  ]);
  assert.match(
    app,
    /for \(const poseUrl of poseUrls\)[\s\S]*await renderLigandInteractions/,
    'each predicted pose must get an independent interaction structure',
  );
  assert.match(
    app,
    /for \(const ligand of ligands\)[\s\S]*mergeRetrospectiveInteractionPdb\(\{ pocketPdb, \.\.\.ligand \}\)/,
    'predicted and crystal interactions must be computed independently',
  );
});
