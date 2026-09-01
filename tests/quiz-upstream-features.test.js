import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const readApp = () => readFile(new URL('../app.js', import.meta.url), 'utf8');
const readHtml = () => readFile(new URL('../index.html', import.meta.url), 'utf8');
const readReadme = () => readFile(new URL('../README.md', import.meta.url), 'utf8');

// app.js is a classic script with no exports, so focused behavioral tests lift a single declaration out of
// the source and evaluate it against stubbed collaborators.
function block(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected ${signature} in app.js`);
  const open = source.indexOf('{', start + signature.length - 1);
  assert.notEqual(open, -1, `expected a block after ${signature}`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return { start, open, end: index + 1 };
    }
  }
  throw new Error(`unbalanced braces after ${signature}`);
}

function evaluateDeclaration(source, signature, sandbox) {
  const { start, end } = block(source, signature);
  return vm.runInContext(`(${source.slice(start, end)})`, vm.createContext(sandbox));
}

// Click handlers are anonymous arrow functions inside init(); evaluate the handler body directly.
function evaluateHandler(source, signature, sandbox) {
  const { open, end } = block(source, signature);
  return vm.runInContext(`(async (b) => ${source.slice(open, end)})`, vm.createContext(sandbox));
}

function fakeElement(tag = 'div') {
  const classes = new Set();
  const element = {
    tag,
    className: '',
    textContent: '',
    disabled: false,
    onclick: null,
    children: [],
    htmlAssignments: [],
    style: { display: '', setProperty() {} },
    classList: {
      add: name => { classes.add(name); },
      remove: name => { classes.delete(name); },
      contains: name => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : !!force;
        if (on) classes.add(name); else classes.delete(name);
      },
    },
    append: (...nodes) => { element.children.push(...nodes); },
    appendChild: node => { element.children.push(node); return node; },
    replaceChildren: (...nodes) => { element.children = nodes; },
  };
  Object.defineProperty(element, 'innerHTML', {
    get: () => element.htmlAssignments.at(-1) ?? '',
    set: value => { element.htmlAssignments.push(String(value)); },
  });
  return element;
}

function renderedText(node) {
  if (typeof node === 'string') return node;
  return (node.textContent || '') + node.children.map(renderedText).join('');
}

function elementRegistry() {
  const elements = new Map();
  return {
    elements,
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, fakeElement());
      return elements.get(selector);
    },
  };
}

test('ports Grid UI and balanced session source contracts', async () => {
  const [app, html] = await Promise.all([readApp(), readHtml()]);

  assert.match(app, /const HARD_MIX = \{ 'game-able': 0\.40, 'all-wrong': 0\.45, 'all-correct': 0\.15 \};/);
  assert.match(app, /function drawSession\(\)/);
  assert.match(app, /function gridEntriesFor\(method\)/);
  assert.match(app, /const LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'\.split\(''\);/);
  assert.match(app, /\$\('#mode'\)\.style\.display = inPlay \? '' : 'none';/);
  assert.match(app, /\$\{rawPoseCount\} predicted poses/);
  assert.match(app, /pose clusters/);
  assert.match(app, /pose details on hover/);
  assert.doesNotMatch(app, /Choices stay method-anonymous/);
  assert.doesNotMatch(app, /The protein and pocket change with the pose/);
  assert.doesNotMatch(app, /linked viewer per pose cluster/);
  assert.match(html, /\.seg\{display:flex;/);
  assert.match(html, /data-m="grid"/);
});

test('retains local persistence, trace, and Storage integration points', async () => {
  const app = await readApp();

  assert.match(app, /researchBackend\(\)\?\.recordAnswer/);
  assert.match(app, /viewerTraceRecorder\?\.stop\(\)/);
  assert.match(app, /window\.foldariumAssetUrl/);
});

test('allocates exact hard-session quotas by deterministic largest remainder', async () => {
  const app = await readApp();
  const start = app.indexOf('function hardSessionQuotas');
  assert.notEqual(start, -1, 'hardSessionQuotas production helper must exist');
  const end = app.indexOf('\n}', start) + 2;
  const hardSessionQuotas = vm.runInNewContext(`(${app.slice(start, end)})`);

  const quotas = hardSessionQuotas(30, {
    'game-able': 0.40,
    'all-wrong': 0.45,
    'all-correct': 0.15,
  });

  assert.deepEqual({ ...quotas }, {
    'game-able': 12,
    'all-wrong': 14,
    'all-correct': 4,
  });
  assert.equal(Object.values(quotas).reduce((total, count) => total + count, 0), 30);
});

function gridLayerSandbox(overrides = {}) {
  const calls = [];
  const sandbox = {
    calls,
    displayMode: 'grid',
    gridBuildRevision: 0,
    resetCameraOnNextBuild: false,
    plugin: { canvas3d: { camera: { getSnapshot: () => ({ question: 'fresh' }) } } },
    proteinData: [],
    layerData: [],
    gridEntries: () => [
      { choice: { pose_file: 'pose-a.pdb' } },
      { choice: { pose_file: 'pose-b.pdb' } },
    ],
    buildCanonicalLayer: async (shown, preserve) => {
      calls.push(`canonical:${shown.map(choice => choice.pose_file).join(',')}:${preserve}`);
    },
    pinCameraSnapshot: async (_plugin, snapshot) => { calls.push(`pin:${snapshot.question}`); },
    buildSingleLayer: async preserve => { calls.push(`single:${preserve}`); },
    buildGrid: async (...args) => { calls.push(`grid:${args.join(',')}`); },
    isFixedReferenceChoice: () => false,
    viewingReleasedCrystal: () => false,
    buildReleasedCrystalScene: async preserve => { calls.push(`released:${preserve}`); },
    hideGrid: () => { calls.push('hideGrid'); },
    $: selector => ({ classList: {
      contains: () => false,
      add: (...names) => calls.push(`${selector}:${names.join(',')}`),
      remove: (...names) => calls.push(`${selector}:remove:${names.join(',')}`),
    } }),
    console: { warn: (...args) => calls.push(`warn:${args[1]}`) },
    ...overrides,
  };
  return sandbox;
}

test('Grid builds visible tiles before the hidden replay scene', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox();
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, [
    '#stage:grid-active',
    '#gridview:on,loading-grid',
    'grid:true,false',
    'canonical:pose-a.pdb,pose-b.pdb:true',
  ]);
});

test('released crystal mode replaces Grid with the separate experimental scene', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox({
    viewingReleasedCrystal: () => true,
  });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, [
    'hideGrid',
    '#stage:remove:grid-active',
    'released:true',
  ]);
});

test('initial Grid framing ignores the empty canonical camera', async () => {
  const app = await readApp();
  assert.match(app, /const hadCanonicalScene = proteinData\.length > 0 \|\| layerData\.length > 0/);
  assert.match(app, /await buildGrid\(!resetCamera, !resetCamera && hadCanonicalScene\)/);
  assert.match(app, /preserveCanonicalCamera \? plugin\?\.canvas3d\?\.camera\?\.getSnapshot\?\.\(\) : null/);
});

test('a failed canonical rebuild leaves the already-loaded Grid tiles intact', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox({
    buildCanonicalLayer: async () => { throw new Error('pose download failed'); },
  });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, [
    '#stage:grid-active',
    '#gridview:on,loading-grid',
    'grid:true,false',
    'warn:pose download failed',
  ]);
});

test('content-addressed weekly assets retain stable cache keys', async () => {
  const app = await readApp();
  assert.match(app, /url\.startsWith\('supabase:\/\/'\)\) return resolved/);
  assert.match(app, /const requestUrl = structureRequestUrl\(url\)/);
  assert.match(app, /structurePrefetcher\.text\(requestUrl\)/);
  assert.match(app, /builders\.data\.download\(\{ url: requestUrl/);
  assert.match(app, /fetch\(requestUrl\)/);
});

test('leaving Grid keeps rebuilding the single view before disposing Grid viewers', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox({
    displayMode: 'all',
    $: () => ({ classList: { contains: () => true } }),
  });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, ['single:true', 'hideGrid']);
  assert.equal(sandbox.gridBuildRevision, 1);
});

test('question transitions discard stale Grid and canonical cameras before recording', async () => {
  const app = await readApp();
  const resets = [];
  const sandbox = {
    displayMode: 'grid',
    plugin: { canvas3d: { requestCameraReset: () => resets.push('reset') } },
  };
  const requestQuestionCameraReset = evaluateDeclaration(
    app,
    'function requestQuestionCameraReset()',
    sandbox,
  );

  requestQuestionCameraReset();
  assert.deepEqual(resets, [], 'Grid must retain the framing mirrored from its active tile');
  sandbox.displayMode = 'all';
  requestQuestionCameraReset();
  assert.deepEqual(resets, ['reset'], 'single-view modes must keep the existing reset behavior');

  const { start, end } = block(app, 'async function loadQuestion(i)');
  const loadQuestion = app.slice(start, end);
  const stop = loadQuestion.indexOf('viewerTraceRecorder?.stop();');
  const settled = loadQuestion.indexOf('await window.waitForCameraSettled(');
  const started = loadQuestion.indexOf('viewerTraceRecorder?.start({ appState: currentReplayableAppState() });');

  assert.ok(stop >= 0, 'expected the recorder to stop in the queued mutation');
  assert.ok(stop < settled && settled < started, 'expected recording to start after the rebuild settles');
  assert.match(loadQuestion, /requestReset: requestQuestionCameraReset/);
  const resetBuild = loadQuestion.indexOf('resetCameraOnNextBuild = true;');
  const disposeGrid = loadQuestion.indexOf('disposeGridViewers();');
  assert.ok(stop < resetBuild && resetBuild < disposeGrid && disposeGrid < settled,
    'expected stale Grid camera publishers to be removed before the question rebuild');

  const gridSandbox = gridLayerSandbox({ resetCameraOnNextBuild: true });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', gridSandbox);
  await buildLayer();
  assert.equal(gridSandbox.resetCameraOnNextBuild, false, 'question reset must be one-shot');
  assert.equal(gridSandbox.calls[2], 'grid:false,false',
    'new Grid questions must frame their own structures');
  assert.deepEqual(gridSandbox.calls.slice(3), [
    'canonical:pose-a.pdb,pose-b.pdb:false',
    'pin:fresh',
  ], 'hidden replay viewer must rebuild without stale framing, then mirror the fresh Grid camera');

  const singleSandbox = gridLayerSandbox({
    displayMode: 'all',
    resetCameraOnNextBuild: true,
    $: () => ({ classList: { contains: () => false } }),
  });
  const buildSingleQuestion = evaluateDeclaration(app, 'async function buildLayer()', singleSandbox);
  await buildSingleQuestion();
  assert.deepEqual(singleSandbox.calls, ['hideGrid', 'single:false'],
    'new canonical questions must not pin the prior question camera during rebuild');
});

test('switching display mode renders choices after the queued mutation', async () => {
  const app = await readApp();
  const calls = [];
  const sandbox = {
    displayMode: 'grid',
    shownOne: 4,
    cur: { revealed: false, selected: { label: 'A' }, selectionExact: true, answerChoices: [{}] },
    viewerControlBlocked: () => false,
    retrospectiveAnswerActive: () => false,
    rememberView: () => { calls.push('rememberView'); },
    syncButtons: () => { calls.push('syncButtons'); },
    renderUI: () => { calls.push('renderUI'); },
    recordAppEvent: action => { calls.push(`trace:${action}`); },
    viewerRebuild: {
      enqueue: async (mutate, finalize = () => {}) => {
        await mutate();
        calls.push(`rebuild:${sandbox.displayMode}`);
        await finalize();
      },
    },
  };
  const onModeClick = evaluateHandler(
    app,
    "document.querySelectorAll('#mode button').forEach(b => b.onclick = async () => {",
    sandbox,
  );

  await onModeClick({ dataset: { m: 'all' } });

  assert.deepEqual(calls, ['rememberView', 'syncButtons', 'rebuild:all', 'renderUI', 'trace:display_mode_changed']);
  assert.equal(sandbox.displayMode, 'all');
  assert.deepEqual(sandbox.cur.selected, { label: 'A' },
    'layout changes preserve the explicit preference');
});

test('retrospective layout switches preserve camera intent and normalize Show-all focus', async () => {
  const app = await readApp();
  const raw = { id: 'raw' };
  const rep = { id: 'rep' };
  const sandbox = {
    displayMode: 'one',
    shownOne: 0,
    clustered: true,
    resetCameraOnNextBuild: false,
    cur: {
      revealed: true,
      selected: raw,
      contextChoice: raw,
      poseFocusChoice: raw,
    },
    viewerControlBlocked: () => false,
    retrospectiveAnswerActive: () => true,
    isFixedReferenceChoice: () => false,
    clusterForChoice: () => ({ rep }),
    visibleIndexForChoice: () => 0,
    syncButtons: () => {},
    renderUI: () => {},
    recordAppEvent: () => {},
    viewerRebuild: {
      enqueue: async (mutate, finalize = () => {}) => {
        await mutate();
        await finalize();
      },
    },
  };
  const onModeClick = evaluateHandler(
    app,
    "document.querySelectorAll('#mode button').forEach(b => b.onclick = async () => {",
    sandbox,
  );

  await onModeClick({ dataset: { m: 'all' } });

  assert.equal(sandbox.resetCameraOnNextBuild, false);
  assert.equal(sandbox.cur.contextChoice, rep);
  assert.equal(sandbox.cur.poseFocusChoice, rep);
});

test('cluster toggles preserve the exact One-at-a-time pose and Show-all context', async () => {
  const app = await readApp();
  const a1 = { pose_file: 'a1.pdb' };
  const a2 = { pose_file: 'a2.pdb' };
  const b1 = { pose_file: 'b1.pdb' };
  const clusters = [
    { rep: a1, members: [a1, a2] },
    { rep: b1, members: [b1] },
  ];
  const sandbox = {
    cur: { clusters, contextChoice: null, poseFocusChoice: null },
    displayMode: 'one',
    clustered: false,
    shownOne: 1,
    Math,
    sameChoice: (left, right) => left?.pose_file === right?.pose_file,
  };
  sandbox.visibleChoices = evaluateDeclaration(app, 'function visibleChoices()', sandbox);
  sandbox.clusterForChoice = evaluateDeclaration(app, 'function clusterForChoice(choice)', sandbox);
  const before = evaluateDeclaration(app, 'function poseFocusBeforeClusterToggle()', sandbox);
  const restore = evaluateDeclaration(app, 'function restorePoseFocusAfterClusterToggle(exactChoice)', sandbox);

  const exact = before();
  assert.equal(exact, a2);
  sandbox.clustered = true;
  restore(exact);
  assert.equal(sandbox.shownOne, 0, 'reclustering should show the cluster containing the raw pose');
  assert.equal(sandbox.cur.poseFocusChoice, a2, 'the exact raw member remains remembered');

  const remembered = before();
  sandbox.clustered = false;
  restore(remembered);
  assert.equal(sandbox.shownOne, 1, 'unclustering again should return to the same raw pose');

  sandbox.displayMode = 'all';
  sandbox.clustered = false;
  sandbox.cur.contextChoice = a2;
  const overlayFocus = before();
  sandbox.clustered = true;
  restore(overlayFocus);
  assert.equal(sandbox.cur.contextChoice, a1,
    'clustered Show all should keep focus on the representative of the clicked pose cluster');
  assert.equal(sandbox.cur.poseFocusChoice, a2,
    'Show all should retain the exact raw pose for a later uncluster');
});

test('Grid page switching rebuilds once with the new page already applied', async () => {
  const app = await readApp();
  const calls = [];
  const registry = elementRegistry();
  const sandbox = {
    displayMode: 'grid',
    gridMethodIndex: 0,
    cur: { item: { source: 'rnp' }, gridMethods: ['af3', 'boltz'], showAnswer: false },
    methodName: method => method.toUpperCase(),
    viewerControlBlocked: () => false,
    renderUI: () => { calls.push('renderUI'); },
    recordAppEvent: action => { calls.push(`trace:${action}`); },
    buildGrid: () => { calls.push('buildGrid'); },
    document: { createElement: tag => fakeElement(tag) },
    $: registry.$,
    viewerRebuild: {
      enqueue: async (mutate, finalize = () => {}) => {
        await mutate();
        calls.push(`rebuild:page=${sandbox.gridMethodIndex}`);
        await finalize();
      },
    },
  };
  const renderGridPages = evaluateDeclaration(app, 'function renderGridPages()', sandbox);
  sandbox.renderGridPages = renderGridPages;

  renderGridPages();
  const nav = registry.elements.get('#gridpages');
  assert.equal(nav.children.length, 2, 'expected one page button per anonymised method set');

  await nav.children[1].onclick();

  assert.equal(sandbox.gridMethodIndex, 1);
  assert.deepEqual(calls, ['rebuild:page=1', 'renderUI', 'trace:grid_page_changed'],
    'expected exactly one rebuild, with only page/UI state rendered afterwards');
});

test('a failed Grid tile is disabled, marked failed, and renders the error as text', async () => {
  const app = await readApp();
  const message = 'WebGL unavailable <img src=x onerror="alert(1)">';
  const sandbox = {
    document: { createElement: tag => fakeElement(tag) },
    molstar: { Viewer: { create: async () => { throw new Error(message); } } },
    OPTS: {},
    gridBuildRevision: 4,
    configurePlugin: () => {},
    gridProteinUrls: () => ({ prot: 'protein.pdb', pocket: null, color: 0 }),
    loadStruct: async () => ({ struct: {} }),
    addRep: async () => {},
    addSticks: async () => {},
    addPose: async () => {},
    structureSphere: () => null,
    GOOD: 0x2BA84A,
    BAD: 0xE23B2E,
  };
  const buildGridCell = evaluateDeclaration(app, 'async function buildGridCell(cell, revision)', sandbox);
  const cell = {
    entry: { choice: { pose_file: 'pose.pdb', color: 0x5B8FF9, label: 'A' }, choiceIndex: 0 },
    card: fakeElement(),
    head: fakeElement('button'),
    host: fakeElement(),
    viewer: null,
    plugin: null,
    disposed: false,
    spec: { item: {}, proteinMode: 'crystal', answer: false },
  };
  cell.head.onclick = () => { throw new Error('a failed tile must not be selectable'); };

  await buildGridCell(cell, 4);

  assert.equal(cell.head.disabled, true);
  assert.equal(cell.head.onclick, null);
  assert.equal(cell.card.classList.contains('failed'), true);
  const error = cell.host.children[0];
  assert.equal(error?.className, 'grid-error');
  assert.match(renderedText(cell.host), /Could not load this pose viewer/);
  assert.ok(renderedText(cell.host).includes(message), 'expected the exception text to be shown');
  assert.deepEqual(cell.host.htmlAssignments, [], 'exception text must not be injected as HTML');
});

test('Weekly clustered layers keep one selectable representative and ghost every other pose', async () => {
  const app = await readApp();
  const representative = { pose_file: 'rep.pdb' };
  const firstGhost = { pose_file: 'ghost-1.pdb' };
  const secondGhost = { pose_file: 'ghost-2.pdb' };
  const cluster = { rep: representative, members: [firstGhost, representative, secondGhost] };
  const layers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', {
    cur: { item: { source: 'weekly' }, clusters: [cluster], revealed: false, showAnswer: false },
    clustered: true,
    displayMode: 'one',
    retrospectiveAnswerActive: () => false,
    clusterForChoice: () => cluster,
    choiceRejected: () => false,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
  })([representative]);

  assert.deepEqual(JSON.parse(JSON.stringify(layers)), [
    { choice: firstGhost, ghost: true },
    { choice: secondGhost, ghost: true },
    { choice: representative, ghost: false },
  ]);
});

test('Weekly Show all renders cluster representatives without ghost members', async () => {
  const app = await readApp();
  const representative = { pose_file: 'rep.pdb' };
  const ghost = { pose_file: 'ghost.pdb' };
  const cluster = { rep: representative, members: [representative, ghost] };
  const layers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', {
    cur: { item: { source: 'weekly' }, clusters: [cluster], revealed: false, showAnswer: false },
    clustered: true,
    displayMode: 'all',
    retrospectiveAnswerActive: () => false,
    clusterForChoice: () => cluster,
    choiceRejected: () => false,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
  })([representative]);

  assert.deepEqual(JSON.parse(JSON.stringify(layers)), [
    { choice: representative, ghost: false },
  ]);
});

test('Weekly Show all fades every non-focused pose after a ligand click', async () => {
  const app = await readApp();
  const first = { pose_file: 'first.pdb' };
  const focused = { pose_file: 'focused.pdb' };
  const third = { pose_file: 'third.pdb' };
  const layers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', {
    cur: {
      item: { source: 'weekly' },
      contextChoice: focused,
      revealed: false,
      showAnswer: false,
    },
    clustered: true,
    displayMode: 'all',
    retrospectiveAnswerActive: () => false,
    choiceRejected: () => false,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
  })([first, focused, third]);

  assert.deepEqual(JSON.parse(JSON.stringify(layers)), [
    { choice: first, ghost: true },
    { choice: focused, ghost: false },
    { choice: third, ghost: true },
  ]);
});

test('Weekly rejected poses stay visible but muted in Show all and One at a time', async () => {
  const app = await readApp();
  const rejected = { pose_file: 'rejected.pdb' };
  const accepted = { pose_file: 'accepted.pdb' };
  const sandbox = {
    cur: { item: { source: 'weekly' }, contextChoice: null, revealed: false, showAnswer: false },
    clustered: false,
    displayMode: 'all',
    retrospectiveAnswerActive: () => false,
    choiceRejected: choice => choice === rejected,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
  };
  const weeklyPoseLayers = evaluateDeclaration(app, 'function weeklyPoseLayers(choices)', sandbox);

  assert.deepEqual(JSON.parse(JSON.stringify(weeklyPoseLayers([rejected, accepted]))), [
    { choice: rejected, rejected: true, ghost: true },
    { choice: accepted, ghost: false },
  ]);
  sandbox.displayMode = 'one';
  assert.deepEqual(JSON.parse(JSON.stringify(weeklyPoseLayers([rejected]))), [
    { choice: rejected, rejected: true, ghost: true },
  ]);
  assert.match(app, /const poseColor = layer\.rejected \? REJECTED_POSE/);
});

test('One-at-a-time rejection mutes and restores the whole molecular viewer', async () => {
  const app = await readApp();
  const choice = { pose_file: 'pose.pdb' };
  const classes = new Set();
  const viewer = {
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
    },
  };
  const actions = { hidden: false };
  const button = () => ({
    classList: { toggle() {}, add() {}, remove() {} },
    textContent: '',
    setAttribute() {},
  });
  const elements = {
    '#app': viewer,
    '#one-review-actions': actions,
    '#one-select': button(),
    '#one-reject': button(),
  };
  let displayedChoice = choice;
  let rejected = true;
  const syncOneReviewState = evaluateDeclaration(app, 'function syncOneReviewState()', {
    $: selector => elements[selector],
    cur: { item: { source: 'weekly' }, revealed: false },
    oneReviewChoice: () => displayedChoice,
    choiceRejected: () => rejected,
    gridChoiceSelected: () => false,
    retrospectiveAnswerActive: () => false,
    isArchiveRetrospective: () => false,
  });

  syncOneReviewState();
  assert.equal(classes.has('rejected'), true,
    'rejecting must mute protein, pocket, ligand, surfaces, and H-bonds through the viewer shell');
  assert.equal(elements['#one-reject'].textContent, 'Undo reject');

  rejected = false;
  syncOneReviewState();
  assert.equal(classes.has('rejected'), false, 'Undo reject must restore the molecular scene');
  assert.equal(elements['#one-reject'].textContent, 'Reject');

  rejected = true;
  displayedChoice = null;
  syncOneReviewState();
  assert.equal(classes.has('rejected'), false,
    'leaving One-at-a-time must not leak rejection styling to another layout');
  assert.equal(actions.hidden, true);
});

test('archive retrospective One at a time replaces ballot actions with protein-frame controls', async () => {
  const app = await readApp();
  const choice = { pose_file: 'pose.pdb' };
  const classes = new Set();
  const button = () => ({
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      add() {},
      remove(name) { classes.delete(name); },
    },
    textContent: '',
    disabled: false,
    setAttribute() {},
  });
  const elements = {
    '#app': { classList: { toggle() {}, remove() {} } },
    '#one-review-actions': { hidden: true },
    '#one-select': button(),
    '#one-reject': button(),
  };
  const syncOneReviewState = evaluateDeclaration(app, 'function syncOneReviewState()', {
    $: selector => elements[selector],
    cur: { item: { source: 'weekly' }, revealed: true },
    oneReviewChoice: () => choice,
    choiceRejected: () => false,
    gridChoiceSelected: () => false,
    retrospectiveAnswerActive: () => true,
    isArchiveRetrospective: () => true,
    retrospectiveProteinFrame: 'folded',
    isFixedReferenceChoice: () => false,
  });

  syncOneReviewState();

  assert.equal(elements['#one-review-actions'].hidden, false);
  assert.equal(elements['#one-select'].textContent, 'Xtal');
  assert.equal(elements['#one-reject'].textContent, 'Folded');
  assert.equal(elements['#one-reject'].disabled, false);
});

test('Weekly cluster acceptance applies to every raw member while labels stay unambiguous', async () => {
  const app = await readApp();
  const members = [
    { correct: false },
    { correct: true },
    { correct: false },
  ];
  const decorate = evaluateDeclaration(app, 'function decorateClusterMembers(members, label, source)', {});
  decorate(members, 'D', 'weekly');

  assert.deepEqual(JSON.parse(JSON.stringify(members)), [
    { correct: false, label: 'D-1', clusterAccepted: true },
    { correct: true, label: 'D-2', clusterAccepted: true },
    { correct: false, label: 'D-3', clusterAccepted: true },
  ]);
  assert.match(app, /const label = cl\.label;/,
    'clustered choices, including Grid, should render one letter without a member suffix');
});

test('Weekly exposes method-specific ligand confidence for every raw pose', async () => {
  const app = await readApp();
  const cur = { item: { source: 'weekly' } };
  const methodName = method => ({ openfold3: 'OpenFold3', boltz2: 'Boltz-2' })[method];
  const weeklyPoseEvidence = evaluateDeclaration(
    app,
    'function weeklyPoseEvidence(choice)',
    { methodName, Number },
  );
  const sandbox = { cur, clustered: true, weeklyPoseEvidence };
  const weeklyEntryEvidence = evaluateDeclaration(
    app,
    'function weeklyEntryEvidence(entry)',
    sandbox,
  );
  const openfold = {
    label: 'D-1',
    _method: 'openfold3',
    _confidence: { metric: 'ligand_plddt', value: 82.54, scale_max: 100 },
    _sminaScore: { metric: 'smina_affinity', value: -7.14 },
    _interactionCount: { metric: 'prolif_hbond_residue_count', value: 3 },
  };
  const boltz = {
    label: 'D-2',
    _method: 'boltz2',
    _confidence: { metric: 'ligand_plddt', value: 74.96, scale_max: 100 },
    _sminaScore: { metric: 'smina_affinity', value: -6.26 },
    _interactionCount: { metric: 'prolif_hbond_residue_count', value: 1 },
  };
  const entry = { choice: openfold, cluster: { members: [openfold, boltz] } };

  assert.equal(
    weeklyEntryEvidence(entry),
    'OpenFold3 · ligand pLDDT 82.5 · smina -7.1 kcal/mol · H-bonds 3\n'
      + 'Boltz-2 · ligand pLDDT 75.0 · smina -6.3 kcal/mol · H-bonds 1',
  );
  sandbox.clustered = false;
  assert.equal(
    weeklyEntryEvidence(entry),
    'OpenFold3 · ligand pLDDT 82.5 · smina -7.1 kcal/mol · H-bonds 3',
  );
  sandbox.clustered = true;
  assert.equal(
    weeklyEntryEvidence({ ...entry, members: [boltz] }),
    'Boltz-2 · ligand pLDDT 75.0 · smina -6.3 kcal/mol · H-bonds 1',
  );
  assert.match(app, /_method: choice\.method \|\| reveal\.method \|\| null/);
  assert.match(app, /_confidence: choice\.confidence \|\| null/);
  assert.match(app, /_sminaScore: choice\.smina_score \|\| null/);
  assert.match(app, /_interactionCount: choice\.interaction_count \|\| null/);
});

test('Weekly Show all starts on the shared receptor and adopts a clicked pose context', async () => {
  const app = await readApp();
  const exact = { afprotein_file: 'exact-protein.pdb', afpocket_file: 'exact-pocket.pdb' };
  const sandbox = {
    cur: {
      item: { source: 'weekly', protein_file: 'medoid.pdb', pocket_file: 'overlay-pocket.pdb' },
      contextChoice: null,
      revealed: false,
      showAnswer: false,
    },
    displayMode: 'all',
    visibleChoices: () => [exact],
    shownOne: 0,
    proteinMode: 'crystal',
  };
  const protUrls = evaluateDeclaration(app, 'function protUrls()', sandbox);

  assert.deepEqual(JSON.parse(JSON.stringify(protUrls())), {
    prot: 'medoid.pdb',
    pocket: null,
  });
  sandbox.cur.contextChoice = exact;
  assert.deepEqual(JSON.parse(JSON.stringify(protUrls())), {
    prot: 'exact-protein.pdb',
    pocket: 'exact-pocket.pdb',
  });
});

test('Weekly Show all maps a ligand representation click to its exact raw pose', async () => {
  const app = await readApp();
  const representation = {};
  const exact = { pose_file: 'exact.pdb' };
  const poseChoiceByRepresentation = new WeakMap();
  const register = evaluateDeclaration(
    app,
    'function registerPoseClickTarget(representationSelector, choice)',
    { poseChoiceByRepresentation },
  );
  const resolve = evaluateDeclaration(
    app,
    'function choiceFromPoseInteraction(event)',
    { poseChoiceByRepresentation },
  );

  register({ obj: { data: { repr: representation } } }, exact);

  assert.equal(resolve({ current: { repr: representation } }), exact);
  assert.equal(resolve({ current: { repr: {} } }), null);
});

test('camera stays pinned throughout a protein and pose rebuild', async () => {
  const app = await readApp();
  let current = { position: [1, 2, 3], target: [0, 0, 0] };
  let changed = null;
  let unsubscribed = false;
  const states = [];
  const camera = {
    getSnapshot: () => current,
    setState(snapshot, duration) {
      states.push([snapshot, duration]);
      current = snapshot;
    },
    changed: {
      subscribe(callback) {
        changed = callback;
        return { unsubscribe: () => { unsubscribed = true; } };
      },
    },
  };
  const plugin = { canvas3d: { camera } };
  const hold = evaluateDeclaration(app, 'function holdCameraSnapshot(targetPlugin, snapshot)', {
    cameraChanges: target => target.canvas3d.camera.changed,
  });
  const original = current;
  const release = hold(plugin, original);

  current = { position: [9, 9, 9], target: [4, 4, 4] };
  changed();
  assert.deepEqual(states, [[original, 0]],
    'an intermediate Mol* focus must be corrected before it can flash');

  release();
  current = { position: [8, 8, 8], target: [3, 3, 3] };
  changed();
  assert.equal(states.length, 1, 'the camera guard must detach after the rebuild');
  assert.equal(unsubscribed, true);
});

test('Weekly Show all routes a ligand click through the normal pose picker', async () => {
  const app = await readApp();
  const exact = { pose_file: 'exact.pdb' };
  const calls = [];
  const handler = evaluateDeclaration(app, 'function onCanonicalPoseInteraction(event)', {
    interactionBlocked: () => false,
    retrospectiveAnswerActive: () => false,
    cur: {
      item: { source: 'weekly' },
      revealed: false,
      contextChoice: null,
    },
    displayMode: 'all',
    choiceFromPoseInteraction: () => exact,
    canonicalInteractionIsEmpty: () => false,
    clearWeeklyShowAllContext: async () => {},
    sameChoice: () => false,
    isFixedReferenceChoice: () => false,
    visibleIndexForChoice: () => 3,
    clearTransientPoseSelection: () => {},
    plugin: {},
    activateCanonicalPoseChoice: async (...args) => { calls.push(args); },
    console,
  });

  handler({ current: { repr: {} } });
  await Promise.resolve();

  assert.deepEqual(calls, [[3, exact]]);
});

test('retrospective Show all switches protein context for predicted and Xtal ligands', async () => {
  const app = await readApp();
  const predicted = { pose_file: 'predicted.pdb' };
  const xtal = { _xtalReference: true, id: '__xtal_reference__' };
  let clicked = predicted;
  const calls = [];
  const handler = evaluateDeclaration(app, 'function onCanonicalPoseInteraction(event)', {
    interactionBlocked: () => true,
    viewerControlBlocked: () => false,
    retrospectiveAnswerActive: () => true,
    cur: { item: { source: 'weekly' }, revealed: true, contextChoice: null },
    displayMode: 'all',
    choiceFromPoseInteraction: () => clicked,
    canonicalInteractionIsEmpty: () => false,
    clearWeeklyShowAllContext: async () => {},
    sameChoice: () => false,
    isFixedReferenceChoice: choice => (
      choice._xtalReference === true || choice._trainingReference === true
    ),
    visibleIndexForChoice: () => 4,
    clearTransientPoseSelection: () => {},
    plugin: {},
    activateCanonicalPoseChoice: async (...args) => { calls.push(args); },
    console,
  });

  handler({ current: { repr: {} } });
  await Promise.resolve();
  clicked = xtal;
  handler({ current: { repr: {} } });
  await Promise.resolve();

  assert.deepEqual(calls, [[4, predicted], [-1, xtal]]);
  assert.match(app, /if \(displayMode !== 'all' \|\| contextChoice\)/);
  assert.match(app, /const pocketChoice = contextChoice\s+\|\| poseLayers/);
  assert.match(app, /const foldedShowAll = displayMode === 'all' && cur\.contextChoice/);
});

test('Weekly One at a time inspects without preferring the exact pose clicked in Molstar', async () => {
  const app = await readApp();
  const exact = { pose_file: 'exact.pdb' };
  const calls = [];
  const handler = evaluateDeclaration(app, 'function onCanonicalPoseInteraction(event)', {
    interactionBlocked: () => false,
    retrospectiveAnswerActive: () => false,
    cur: { item: { source: 'weekly' }, revealed: false, contextChoice: null },
    displayMode: 'one',
    choiceFromPoseInteraction: () => exact,
    canonicalInteractionIsEmpty: () => false,
    visibleIndexForChoice: () => 2,
    clearTransientPoseSelection: () => {},
    plugin: {},
    inspectCanonicalChoice: (...args) => { calls.push(args); },
    console,
  });

  handler({ current: { repr: {} } });
  await Promise.resolve();

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [[exact]]);
});

test('retrospective One at a time ligand clicks inspect the pose without rebuilding pocket sticks', async () => {
  const app = await readApp();
  const choice = {
    pose_file: 'aligned-pose.pdb',
    answer_crystal_pocket_pdb: 'ATOM crystal pocket',
  };
  const inspected = [];
  const handler = evaluateDeclaration(app, 'function onCanonicalPoseInteraction(event)', {
    interactionBlocked: () => true,
    viewerControlBlocked: () => false,
    retrospectiveAnswerActive: () => true,
    cur: { item: { source: 'weekly' }, revealed: true, contextChoice: null },
    displayMode: 'one',
    choiceFromPoseInteraction: () => choice,
    canonicalInteractionIsEmpty: () => false,
    clearTransientPoseSelection: () => {},
    plugin: {},
    inspectCanonicalChoice: current => { inspected.push(current); },
    console,
  });

  handler({ current: { repr: {} } });

  assert.deepEqual(inspected, [choice]);
  assert.match(app, /registerPoseClickTarget\(representation, xtalClickChoice \|\| crystalChoiceByPdb\.get\(crystalPdb\)\)/);
  assert.doesNotMatch(app, /click a ligand for pocket sticks/);
});

test('retrospective One at a time maps clustered ghost clicks to the visible representative', async () => {
  const app = await readApp();
  const ghost = { id: 'ghost' };
  const cur = { contextChoice: null, poseFocusChoice: null };
  const sandbox = {
    cur,
    displayMode: 'one',
    shownOne: 0,
    retrospectiveAnswerActive: () => true,
    viewerControlBlocked: () => false,
    interactionBlocked: () => true,
    isFixedReferenceChoice: () => false,
    visibleIndexForChoice: choice => choice === ghost ? 3 : -1,
    retrospectiveNavChoices: () => [],
    recordAppEvent: () => {},
    selectedPaneId: 'pane',
  };
  const inspectCanonicalChoice = evaluateDeclaration(
    app,
    'function inspectCanonicalChoice(choice)',
    sandbox,
  );

  inspectCanonicalChoice(ghost);

  assert.equal(sandbox.shownOne, 3);
  assert.equal(cur.contextChoice, ghost);
  assert.equal(cur.poseFocusChoice, ghost);
  assert.equal(sandbox.selectedPaneId, null);
});

test('pose clicks clear only Molstar selection marking after the native click', async () => {
  const app = await readApp();
  let scheduled = null;
  let deselections = 0;
  const clear = evaluateDeclaration(app, 'function clearTransientPoseSelection(targetPlugin)', {
    window: { requestAnimationFrame: callback => { scheduled = callback; } },
    globalThis: {},
    setTimeout,
  });
  const targetPlugin = {
    managers: { interactivity: { lociSelects: { deselectAll: () => { deselections += 1; } } } },
  };

  clear(targetPlugin);
  assert.equal(deselections, 0, 'the native Molstar click should finish first');
  scheduled();
  assert.equal(deselections, 1, 'the transient magenta selection marker should be removed');
  assert.doesNotMatch(app, /managers\?\.structure\?\.focus.*clear/,
    'ligand focus and its pocket interactions must remain intact');
});

test('Weekly Grid inspects without preferring the exact pose clicked inside its Molstar pane', async () => {
  const app = await readApp();
  const choice = { pose_file: 'grid-pose.pdb', color: 0x5B8FF9 };
  const calls = [];
  let clickHandler = null;
  const camera = { changed: {}, getSnapshot: () => ({}), setState: () => {} };
  const plugin = {
    canvas3d: { camera, requestCameraReset() {} },
    behaviors: {
      interaction: {
        click: {
          subscribe(handler) {
            clickHandler = handler;
            return { unsubscribe() {} };
          },
        },
      },
    },
  };
  const sandbox = {
    molstar: { Viewer: { create: async () => ({ plugin, handleResize() {}, dispose() {} }) } },
    OPTS: {}, gridBuildRevision: 5, configurePlugin: () => {}, viewerTraceRecorder: null,
    gridProteinUrls: () => ({ prot: 'protein.pdb', pocket: null, color: 0x9aa6b2 }),
    loadStruct: async url => ({ struct: { url } }), addRep: async () => {}, addSticks: async () => {},
    addPose: async () => ({ obj: { data: { repr: {} } } }),
    registerPoseClickTarget: () => {},
    choiceFromPoseInteraction: () => choice,
    clearTransientPoseSelection: () => {},
    locked: () => false,
    activatePane: (...args) => { calls.push(['pane', ...args]); },
    selectedPaneId: null,
    inspectGridChoice: (...args) => { calls.push(['inspect', ...args]); },
    acceptedChoiceCorrect: () => false,
    sameChoice: (left, right) => left?.pose_file === right?.pose_file,
    GHOST_PROTEIN_ALPHA: 0.12, GHOST_POSE_ALPHA: 0.18, GHOST_POSE_SIZE: 0.14,
    structureSphere: () => null, buildInteractions: async () => {},
    isFixedReferenceChoice: () => false,
    itemHasReleasedCrystal: () => false,
    focusLigandSpheres: () => false,
    cameraChanges: target => target.canvas3d.camera.changed,
    window: { waitForCameraSettled: async () => {} }, GOOD: 1, BAD: 2,
  };
  sandbox.populateGridCell = evaluateDeclaration(
    app, 'async function populateGridCell(cell, revision, { preserveCamera = null } = {})', sandbox,
  );
  const buildGridCell = evaluateDeclaration(app, 'async function buildGridCell(cell, revision)', sandbox);
  const cell = {
    entry: { choice, choiceIndex: 4, cluster: { members: [choice] } },
    paneId: 'pane-0-4', card: fakeElement(), head: fakeElement('button'), host: fakeElement(),
    viewer: null, plugin: null, disposed: false,
    spec: { item: { source: 'weekly' }, proteinMode: 'crystal', answer: false,
      clustered: true, showHbonds: false, showProteinEnsemble: false, showSurface: false },
  };

  await buildGridCell(cell, 5);
  assert.equal(typeof clickHandler, 'function');
  clickHandler({ current: { repr: {} } });
  await Promise.resolve();

  assert.deepEqual(calls, [
    ['inspect', cell.entry, 'pane-0-4', 'ligand-click'],
  ]);
});

test('Archive Grid builds the Xtal reference from retrospective crystal artifacts', async () => {
  const app = await readApp();
  const choice = { _xtalReference: true };
  const calls = [];
  const plugin = {
    canvas3d: { camera: { changed: {}, getSnapshot: () => ({}) } },
  };
  const sandbox = {
    gridBuildRevision: 7,
    gridProteinUrls: () => ({ prot: undefined, pocket: undefined, color: 0 }),
    isFixedReferenceChoice: candidate => (
      candidate?._xtalReference === true || candidate?._trainingReference === true
    ),
    buildRetrospectiveFoldedGridCell: async () => { calls.push('folded'); },
    buildRetrospectiveGridCell: async () => { calls.push('crystal'); },
    itemHasReleasedCrystal: () => true,
    loadStruct: async () => { throw new Error('archive Xtal must not request a pose protein URL'); },
    cameraChanges: target => target.canvas3d.camera.changed,
    window: { waitForCameraSettled: async () => {} },
  };
  const populateGridCell = evaluateDeclaration(
    app, 'async function populateGridCell(cell, revision, { preserveCamera = null } = {})', sandbox,
  );
  const cell = {
    entry: { choice },
    viewer: { handleResize() {} },
    plugin,
    disposed: false,
    spec: {
      item: { source: 'archive', released_crystal: { cif_url: 'crystal.cif' } },
      answer: true,
      retrospectiveReview: true,
      retrospectiveProteinFrame: 'xtal',
    },
  };

  await populateGridCell(cell, 7);

  assert.deepEqual(calls, ['crystal']);
});

test('Weekly Show all waits for the click zoom before activating pose context', async () => {
  const app = await readApp();
  const oldCamera = { position: [1, 1, 1], target: [0, 0, 0] };
  const zoomedCamera = { position: [2, 2, 2], target: [4, 4, 4] };
  let current = oldCamera;
  let frames = 0;
  const transition = { inTransition: true };
  const snapshotAfterInteraction = evaluateDeclaration(
    app,
    'async function cameraSnapshotAfterInteraction(targetPlugin)',
    {
      nextAnimationFrame: async () => {
        frames += 1;
        if (frames === 3) {
          current = zoomedCamera;
          transition.inTransition = false;
        }
      },
    },
  );
  const plugin = { canvas3d: { camera: { transition, getSnapshot: () => current } } };

  assert.equal(await snapshotAfterInteraction(plugin), zoomedCamera);
  assert.equal(frames, 3, 'the rebuild snapshot must wait for Molstar click-focus to finish');

  const choice = { pose_file: 'clicked.pdb' };
  const item = { source: 'weekly' };
  const calls = [];
  const activationSandbox = {
    canonicalPoseActivationRevision: 0,
    cur: { item, revealed: false },
    displayMode: 'all',
    plugin,
    cameraSnapshotAfterInteraction: async () => zoomedCamera,
    retrospectiveAnswerActive: () => false,
    shownOne: 0,
    selectedPaneId: 'pane-old',
    nextCanonicalCameraSnapshot: null,
    renderUI: () => { calls.push(['render']); },
    recordAppEvent: action => { calls.push(['trace', action]); },
    viewerRebuild: {
      enqueue: async (mutate, finalize) => { await mutate(); await finalize(); },
    },
  };
  const activate = evaluateDeclaration(
    app,
    'async function activateCanonicalPoseChoice(index, choice)',
    activationSandbox,
  );

  await activate(2, choice);

  assert.equal(activationSandbox.shownOne, 2);
  assert.equal(activationSandbox.cur.contextChoice, choice);
  assert.equal(activationSandbox.nextCanonicalCameraSnapshot, zoomedCamera);
  assert.deepEqual(calls, [['render'], ['trace', 'pose_inspected']]);
  assert.match(app,
    /let preservedCamera = preserveCamera \? nextCanonicalCameraSnapshot : null;\s*nextCanonicalCameraSnapshot = null;/);
  assert.match(app, /nextCanonicalCameraSnapshot = cameraSnapshot;/);
});

test('all Molstar viewers disable the axes helper', async () => {
  const app = await readApp();
  let props = null;
  const configure = evaluateDeclaration(app, 'function configurePlugin(targetPlugin)', {});

  configure({ canvas3d: { setProps: value => { props = value; } } });

  assert.equal(props.camera.manualReset, true);
  assert.deepEqual(JSON.parse(JSON.stringify(props.camera.helper)), {
    axes: { name: 'off', params: {} },
  });
});

test('pose information uses one viewport-level tooltip for every Grid card', async () => {
  const [app, html] = await Promise.all([readApp(), readHtml()]);
  const position = evaluateDeclaration(app, 'function poseInfoTooltipPosition(anchor, tooltip, viewport)', { Math });

  assert.match(html, /\.pose-tooltip\{position:fixed;z-index:100/);
  assert.match(html, /<div class="pose-tooltip" id="pose-tooltip" role="tooltip" hidden><\/div>/);
  assert.doesNotMatch(html, /\.pose-info::after/);
  assert.match(html, /\.pose-tooltip-method\{[\s\S]*?border-radius:999px/);
  assert.match(html, /\.pose-tooltip-metrics\{display:grid;grid-template-columns:repeat\(3,minmax\(0,1fr\)\)/);
  assert.deepEqual(
    JSON.parse(JSON.stringify(position(
      { left: 2, right: 22, top: 2, bottom: 22 },
      { width: 330, height: 70 },
      { width: 800, height: 600 },
    ))),
    { left: 8, top: 30 },
    'the first Grid card should open below and stay inside the left viewport edge',
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(position(
      { left: 778, right: 798, top: 590, bottom: 610 },
      { width: 330, height: 70 },
      { width: 800, height: 600 },
    ))),
    { left: 462, top: 512 },
    'rightmost and bottom-row Grid cards should open above and stay inside the viewport',
  );
});

test('pose information is split into structured method and metric rows', async () => {
  const app = await readApp();
  const rows = evaluateDeclaration(app, 'function poseInfoTooltipRows(evidence)', { String });

  assert.deepEqual(JSON.parse(JSON.stringify(rows(
    'Boltz-2 · ligand pLDDT 72.0 · smina -3.9 kcal/mol · H-bonds 1\n'
      + 'OpenFold3 · ligand pLDDT 76.6 · smina -5.1 kcal/mol · H-bonds 2',
  ))), [
    { method: 'Boltz-2', metrics: [
      { label: 'ligand pLDDT', value: '72.0' },
      { label: 'smina', value: '-3.9 kcal/mol' },
      { label: 'H-bonds', value: '1' },
    ] },
    { method: 'OpenFold3', metrics: [
      { label: 'ligand pLDDT', value: '76.6' },
      { label: 'smina', value: '-5.1 kcal/mol' },
      { label: 'H-bonds', value: '2' },
    ] },
  ]);
  assert.match(app, /tooltip\.replaceChildren\(\)/);
  assert.doesNotMatch(app, /tooltip\.innerHTML/);
});

test('Weekly Show all clears only visual pose context when empty Molstar space is clicked', async () => {
  const app = await readApp();
  const selected = { pose_file: 'selected.pdb' };
  const calls = [];
  const cur = {
    item: { source: 'weekly' },
    revealed: false,
    selected,
    contextChoice: selected,
  };
  const handler = evaluateDeclaration(app, 'function onCanonicalPoseInteraction(event)', {
    interactionBlocked: () => false,
    retrospectiveAnswerActive: () => false,
    cur,
    canonicalPoseActivationRevision: 0,
    displayMode: 'all',
    choiceFromPoseInteraction: () => null,
    canonicalInteractionIsEmpty: event => event.current.loci.kind === 'empty-loci',
    clearWeeklyShowAllContext: async () => {
      calls.push('reset');
      cur.contextChoice = null;
    },
    sameChoice: () => false,
    visibleIndexForChoice: () => -1,
    onPick: async () => { calls.push('pick'); },
    console,
  });

  handler({ current: { loci: { kind: 'empty-loci' } } });
  await Promise.resolve();

  assert.deepEqual(calls, ['reset']);
  assert.equal(cur.contextChoice, null);
  assert.equal(cur.selected, selected, 'resetting the visual context must preserve the pending vote');
});

test('Weekly Show all resets the ensemble camera on the first empty-space click', async () => {
  const app = await readApp();
  const selected = { pose_file: 'selected.pdb' };
  const calls = [];
  const sandbox = {
    canonicalPoseActivationRevision: 0,
    cur: {
      contextChoice: selected,
      poseFocusChoice: selected,
    },
    selectedPaneId: 'pane-1',
    resetCameraOnNextBuild: false,
    viewerRebuild: {
      enqueue: async (mutate, render) => {
        mutate();
        calls.push('rebuilt');
        render?.();
      },
    },
    plugin: {
      canvas3d: {
        requestCameraReset: () => calls.push('camera-reset'),
      },
    },
    recordAppEvent: event => calls.push(event),
  };
  const clearContext = evaluateDeclaration(
    app,
    'async function clearWeeklyShowAllContext()',
    sandbox,
  );

  await clearContext();

  assert.equal(sandbox.cur.contextChoice, null);
  assert.equal(sandbox.cur.poseFocusChoice, null);
  assert.equal(sandbox.selectedPaneId, null);
  assert.equal(sandbox.resetCameraOnNextBuild, true);
  assert.deepEqual(calls, ['rebuilt', 'camera-reset', 'pose_context_cleared']);
});

test('Weekly pose metrics are attached to a hover-only information affordance', async () => {
  const [app, html] = await Promise.all([readApp(), readHtml()]);

  assert.match(app, /attachPoseInfo\(b, weeklyEntryEvidence\(entry\)\)/);
  assert.match(app, /attachPoseInfo\(head, weeklyEntryEvidence\(entry\)\)/);
  assert.doesNotMatch(app, /<span class="tag" data-tag>\$\{evidence\}<\/span>/);
  assert.match(app, /event\.stopPropagation\(\)/);
  assert.match(app, /info\.addEventListener\('pointerenter', showTooltip\)/);
  assert.match(app, /info\.addEventListener\('focus', showTooltip\)/);
  assert.match(html, /\.pose-tooltip\{position:fixed/);
});

test('Weekly Grid shows compact confidence while retrospective Grid shows cluster votes', async () => {
  const app = await readApp();
  const choice = {
    label: 'C',
    color: 0x5B8FF9,
    _confidence: { metric: 'ligand_plddt', value: 72.54, scale_max: 100 },
    _interactionCount: { metric: 'prolif_hbond_residue_count', value: 3 },
  };
  const weeklyLigandPlddt = evaluateDeclaration(
    app,
    'function weeklyLigandPlddt(choice)',
    { Number },
  );
  const weeklyHbondCount = evaluateDeclaration(
    app,
    'function weeklyHbondCount(choice)',
    { Number },
  );
  const header = evaluateDeclaration(app, 'function gridHeader(entry)', {
    cur: { item: { source: 'weekly' }, revealed: false, showAnswer: false },
    clustered: true,
    weeklyLigandPlddt,
    hex: color => `#${color.toString(16)}`,
    displayedPoseLabel: current => current.label,
    acceptedChoiceCorrect: () => false,
    gridChoiceSelected: () => false,
    isXtalReferenceChoice: () => false,
    isTrainingReferenceChoice: () => false,
    GOOD: 1,
    BAD: 2,
  })({ choice, memberCount: 2 });

  assert.match(header, /Pose C/);
  assert.match(header, /2 poses/);
  assert.match(header, /pLDDT 72\.5/);
  assert.doesNotMatch(header, /ligand pLDDT/);
  assert.doesNotMatch(header, /OpenFold|Boltz|smina|ProLIF|\/100/);

  const answerHeader = evaluateDeclaration(app, 'function gridHeader(entry)', {
    cur: { item: { source: 'weekly' }, revealed: true, showAnswer: true },
    clustered: true,
    weeklyLigandPlddt,
    hex: color => `#${color.toString(16)}`,
    displayedPoseLabel: current => current.label,
    answerViewPoseCorrect: () => true,
    gridChoiceSelected: () => false,
    isXtalReferenceChoice: () => false,
    isTrainingReferenceChoice: () => false,
    GOOD: 1,
    BAD: 2,
  })({
    choice: { ...choice, rmsd: 1.24, _weeklyVoteCount: 2 },
    cluster: { members: [{ _weeklyVoteCount: 2 }, { _weeklyVoteCount: 3 }] },
    memberCount: 2,
  });
  assert.match(answerHeader, /class="grid-rmsd correct">RMSD 1\.24 Å/);
  assert.doesNotMatch(answerHeader, /Exact correct|Incorrect/);
  assert.doesNotMatch(answerHeader, /pLDDT/);
  assert.match(answerHeader, /2 poses/);
  assert.match(answerHeader, /5 votes/);

  const registry = elementRegistry();
  const badgeSandbox = {
    DEV: false,
    WEEKLY_ROUND: { public_status: 'open' },
    cur: { item: { source: 'weekly' } },
    displayMode: 'one',
    visibleChoices: () => [choice],
    shownOne: 0,
    displayedPoseLabel: current => current.label,
    weeklyLigandPlddt,
    weeklyHbondCount,
    viewingReleasedCrystal: () => false,
    retrospectiveAnswerActive: () => false,
    retrospectiveNavChoices: () => [choice],
    isXtalReferenceChoice: () => false,
    isTrainingReferenceChoice: () => false,
    $: registry.$,
  };
  const syncStageBadge = evaluateDeclaration(app, 'function syncStageBadge()', badgeSandbox);
  syncStageBadge();
  assert.equal(registry.elements.get('#badge').textContent, 'Pose C · ligand pLDDT 72.5 · H-bonds 3');
  assert.equal(registry.elements.get('#badge').style.display, '');

  badgeSandbox.displayMode = 'grid';
  syncStageBadge();
  assert.equal(registry.elements.get('#badge').style.display, 'none');

  badgeSandbox.displayMode = 'all';
  syncStageBadge();
  assert.equal(registry.elements.get('#badge').style.display, 'none',
    'Weekly Show all has no pose-specific badge information to display');
});

test('Weekly Show all rebuilds the exact clicked protein and pocket sticks', async () => {
  const app = await readApp();
  const loaded = [];
  const stickCalls = [];
  const sandbox = {
    protUrls: () => ({ prot: 'exact-protein.pdb', pocket: 'exact-pocket.pdb' }),
    weeklyGhostProteinUrls: () => [],
    currentProteinKey: null,
    proteinData: [],
    plugin: {},
    loadStruct: async url => {
      loaded.push(url);
      return { data: { ref: url }, struct: { url } };
    },
    addRep: async () => {},
    addSticks: async (...args) => { stickCalls.push(args); },
    showSurface: false,
    proteinMode: 'crystal',
    AF3PROT: 2,
    PROT: 1,
    GHOST_PROTEIN_ALPHA: 0.12,
  };
  const buildProtein = evaluateDeclaration(app, 'async function buildProtein(shown)', sandbox);

  await buildProtein([]);

  assert.deepEqual(loaded, ['exact-protein.pdb', 'exact-pocket.pdb']);
  assert.equal(stickCalls.length, 1);
  assert.equal(stickCalls[0][0].url, 'exact-pocket.pdb');
});

test('Weekly Grid renders ghost cluster members and representative H-bonds', async () => {
  const app = await readApp();
  const poseCalls = [];
  const interactionCalls = [];
  let resetCount = 0;
  const camera = { changed: {}, getSnapshot: () => ({ position: 1 }), setState: () => {} };
  const plugin = { canvas3d: { camera, requestCameraReset: () => { resetCount += 1; } } };
  const representative = { pose_file: 'rep.pdb', afprotein_file: 'rep-protein.pdb', color: 7 };
  const ghost = { pose_file: 'ghost.pdb', afprotein_file: 'ghost-protein.pdb', color: 7 };
  const sandbox = {
    molstar: { Viewer: { create: async () => ({ plugin, handleResize: () => {}, dispose: () => {} }) } },
    OPTS: {}, gridBuildRevision: 4, configurePlugin: () => {}, viewerTraceRecorder: null,
    gridProteinUrls: () => ({ prot: 'rep-protein.pdb', pocket: 'rep-pocket.pdb', color: 3 }),
    loadStruct: async url => ({ struct: { url } }), addRep: async () => {}, addSticks: async () => {},
    addPose: async (_struct, _color, _plugin, options) => { poseCalls.push(options || null); },
    registerPoseClickTarget: () => {},
    acceptedChoiceCorrect: choice => choice.correct === true,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
    GHOST_PROTEIN_ALPHA: 0.12, GHOST_POSE_ALPHA: 0.18, GHOST_POSE_SIZE: 0.14,
    structureSphere: () => ({ radius: 1 }),
    buildInteractions: async (...args) => { interactionCalls.push(args); },
    isFixedReferenceChoice: () => false,
    itemHasReleasedCrystal: () => false,
    focusLigandSpheres: () => false,
    cameraChanges: target => target.canvas3d.camera.changed,
    window: { waitForCameraSettled: async ({ requestReset }) => requestReset() },
    GOOD: 0x2BA84A, BAD: 0xE23B2E,
  };
  sandbox.populateGridCell = evaluateDeclaration(
    app, 'async function populateGridCell(cell, revision, { preserveCamera = null } = {})', sandbox,
  );
  const buildGridCell = evaluateDeclaration(app, 'async function buildGridCell(cell, revision)', sandbox);
  const cell = {
    entry: { choice: representative, cluster: { members: [representative, ghost] } },
    card: fakeElement(), head: fakeElement('button'), host: fakeElement(),
    viewer: null, plugin: null, disposed: false,
    spec: { item: { source: 'weekly' }, proteinMode: 'crystal', answer: false,
      clustered: true, showHbonds: true, showProteinEnsemble: true, showSurface: false },
  };

  await buildGridCell(cell, 4);

  assert.deepEqual(JSON.parse(JSON.stringify(poseCalls)), [
    { alpha: 0.18, sizeFactor: 0.14 }, null,
  ]);
  assert.equal(interactionCalls.length, 1);
  assert.equal(interactionCalls[0][0], 'rep-pocket.pdb');
  assert.deepEqual(JSON.parse(JSON.stringify(interactionCalls[0][1])), ['rep.pdb']);
  assert.equal(resetCount, 1);
  assert.equal(cell.failed, undefined);
});

test('Surface mode adds representative protein and ligand surfaces in Grid', async () => {
  const app = await readApp();
  const reps = [];
  const registered = [];
  const camera = { changed: {}, getSnapshot: () => ({}), setState: () => {} };
  const plugin = { canvas3d: { camera, requestCameraReset() {} } };
  const choice = { pose_file: 'pose.pdb', color: 0x5B8FF9 };
  const sandbox = {
    molstar: { Viewer: { create: async () => ({ plugin, handleResize() {}, dispose() {} }) } },
    OPTS: {}, gridBuildRevision: 2, configurePlugin: () => {}, viewerTraceRecorder: null,
    gridProteinUrls: () => ({ prot: 'protein.pdb', pocket: null, color: 0x9aa6b2 }),
    loadStruct: async url => ({ struct: { url } }),
    addRep: async (struct, selector, type, color, alpha) => {
      reps.push({ url: struct.url, selector, type, color, alpha });
      return type === 'molecular-surface' && selector === 'all'
        ? { obj: { data: { repr: { kind: 'ligand-surface' } } } }
        : null;
    },
    addSticks: async () => {},
    addPose: async () => ({ obj: { data: { repr: { kind: 'ligand-sticks' } } } }),
    registerPoseClickTarget: (selector, targetChoice) => {
      if (selector) registered.push([selector.obj.data.repr.kind, targetChoice]);
    },
    acceptedChoiceCorrect: () => false,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
    GHOST_PROTEIN_ALPHA: 0.12, GHOST_POSE_ALPHA: 0.18, GHOST_POSE_SIZE: 0.14,
    structureSphere: () => null, buildInteractions: async () => {},
    isFixedReferenceChoice: () => false,
    itemHasReleasedCrystal: () => false,
    focusLigandSpheres: () => false,
    cameraChanges: target => target.canvas3d.camera.changed,
    window: { waitForCameraSettled: async () => {} }, GOOD: 1, BAD: 2,
  };
  sandbox.populateGridCell = evaluateDeclaration(
    app, 'async function populateGridCell(cell, revision, { preserveCamera = null } = {})', sandbox,
  );
  const buildGridCell = evaluateDeclaration(app, 'async function buildGridCell(cell, revision)', sandbox);
  const cell = {
    entry: { choice, cluster: { members: [choice] } },
    card: fakeElement(), head: fakeElement('button'), host: fakeElement(),
    viewer: null, plugin: null, disposed: false,
    spec: { item: { source: 'weekly' }, proteinMode: 'crystal', answer: false,
      clustered: true, showHbonds: false, showProteinEnsemble: false, showSurface: true },
  };

  await buildGridCell(cell, 2);

  assert.deepEqual(reps, [
    { url: 'protein.pdb', selector: 'polymer', type: 'cartoon', color: 0x9aa6b2, alpha: 0.5 },
    { url: 'protein.pdb', selector: 'polymer', type: 'molecular-surface', color: 0x9aa6b2, alpha: 0.7 },
    { url: 'pose.pdb', selector: 'all', type: 'molecular-surface', color: 0x5B8FF9, alpha: 0.7 },
  ]);
  assert.deepEqual(registered, [
    ['ligand-sticks', choice],
    ['ligand-surface', choice],
  ]);
  assert.match(app, /registerPoseClickTarget\(surfaceRepresentation, c\)/);
});

test('Surface toggle rebuilds the canonical protein when disabled', async () => {
  const app = await readApp();
  const reps = [];
  const deleted = [];
  const sandbox = {
    showSurface: true, currentProteinKey: null, proteinData: [], proteinMode: 'crystal',
    PROT: 0x9aa6b2, AF3PROT: 0x8FA8CC,
    protUrls: () => ({ prot: 'protein.pdb', pocket: null }),
    weeklyGhostProteinUrls: () => [],
    loadStruct: async () => ({ data: { ref: 'protein-data' }, struct: {} }),
    addRep: async (_struct, selector, type, color, alpha) => reps.push({ selector, type, color, alpha }),
    addSticks: async () => {}, GHOST_PROTEIN_ALPHA: 0.12,
    plugin: { build: () => ({ delete(ref) { deleted.push(ref); }, async commit() {} }) },
    JSON,
  };
  const buildProtein = evaluateDeclaration(app, 'async function buildProtein(shown)', sandbox);

  await buildProtein([]);
  sandbox.showSurface = false;
  await buildProtein([]);

  assert.deepEqual(reps, [
    { selector: 'polymer', type: 'cartoon', color: 0x9aa6b2, alpha: 0.5 },
    { selector: 'polymer', type: 'molecular-surface', color: 0x9aa6b2, alpha: 0.7 },
    { selector: 'polymer', type: 'cartoon', color: 0x9aa6b2, alpha: 0.5 },
  ]);
  assert.deepEqual(deleted, ['protein-data']);
});

test('Weekly question changes preserve layout and H-bonds but reset expensive surfaces', async () => {
  const app = await readApp();
  assert.match(app, /Molecular surfaces are a deliberately question-local expensive opt-in/);
  assert.match(app, /userView\.showSurface = false;[\s\S]*?applyUserView\(\)/);
  assert.match(app, /question_load_ms: Math\.max\(0, Date\.now\(\) - loadStartedAt\)/);
  assert.match(app, /Vote saved\. Loading next question/);
});

test('Easy eligibility keeps reachable pick puzzles and drops sets whose clusters hide an option', async () => {
  const app = await readApp();
  const thresholds = /const CORRECT_THRESH = ([\d.]+), WRONG_THRESH = ([\d.]+);/.exec(app);
  assert.ok(thresholds, 'expected the correctness thresholds in app.js');
  const [, correctThresh, wrongThresh] = thresholds.map(Number);
  const easyPlayable = evaluateDeclaration(app, 'function easyPlayable(choices, source)', {
    CORRECT_THRESH: correctThresh,
    WRONG_THRESH: wrongThresh,
  });
  const pose = (cluster, rmsd, is_rep, _method) => ({
    cluster, rmsd, is_rep, _method, correct: rmsd < correctThresh,
  });

  // Representative poses still expose a correct and a clearly wrong option.
  assert.equal(easyPlayable([
    pose(1, 0.6, true), pose(1, 1.1, false), pose(2, 4.2, true),
  ], 'cameo'), true);

  // Clustering hides the only correct pose behind a wrong representative.
  assert.equal(easyPlayable([
    pose(1, 4.0, true), pose(1, 0.6, false), pose(2, 5.2, true),
  ], 'cameo'), false);

  // Clustering hides the only clearly wrong pose behind a borderline representative.
  assert.equal(easyPlayable([
    pose(1, 0.6, true), pose(2, 2.0, true), pose(2, 4.8, false),
  ], 'cameo'), false);

  // Runs-n-Poses items also weigh the pooled representatives reachable through the anonymised Grid pages.
  assert.equal(easyPlayable([
    pose(1, 0.6, true, 'af3'), pose(1, 0.9, false, 'boltz'),
    pose(2, 5.2, true, 'af3'), pose(2, 6.1, false, 'boltz'),
  ], 'rnp'), true);
  assert.equal(easyPlayable([
    pose(1, 4.0, true, 'af3'), pose(1, 0.6, false, 'boltz'),
    pose(2, 5.2, true, 'af3'), pose(2, 6.1, false, 'boltz'),
  ], 'rnp'), false);
});

test('finishing the quiz completes the session without waiting for a leaderboard name', async () => {
  const app = await readApp();
  const registry = elementRegistry();
  const completed = [];
  const sandbox = {
    $: registry.$,
    hideGrid: () => {},
    researchBackend: () => ({ completeSession: id => completed.push(id) }),
    remoteSessionId: 'remote-session-1',
    score: { you: 4, af3: 2, n: 5, randExp: 1.5 },
    oppLabel: () => 'AlphaFold3 (pLDDT-ranked)',
    quizSource: 'cameo',
    difficulty: 'hard',
    submitSession: () => {},
  };
  const finish = evaluateDeclaration(app, 'function finish()', sandbox);

  finish();

  assert.deepEqual(completed, ['remote-session-1']);
  assert.equal(registry.elements.get('#submit').onclick, sandbox.submitSession);
});

test('Grid camera sync mirrors the canonical viewer and tolerates lost canvases', async () => {
  const app = await readApp();
  const frames = [];
  const sandbox = {
    plugin: undefined,
    activePaneId: null,
    viewerTraceRecorder: null,
    activatePane: () => {},
    requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
    cancelAnimationFrame: () => {},
  };
  sandbox.cameraSnapshotForScene = evaluateDeclaration(
    app, 'function cameraSnapshotForScene(sharedSnapshot, sceneSnapshot)', sandbox);
  const syncGridCameras = evaluateDeclaration(app, 'function syncGridCameras(cells)', sandbox);
  const fakeCamera = position => {
    let snapshot = { position };
    return { getSnapshot: () => snapshot, setState: next => { snapshot = next; } };
  };
  const cells = [
    { plugin: { canvas3d: { camera: fakeCamera(1) } } },
    { plugin: { canvas3d: { camera: fakeCamera(2) } } },
    { plugin: null },
  ];

  let stop;
  assert.doesNotThrow(() => { stop = syncGridCameras(cells); },
    'a disposed cell must not break camera sync setup');

  cells[0].plugin.canvas3d.camera.setState({ position: 9 });
  assert.doesNotThrow(() => frames.pop()(), 'a missing canonical viewer must not break the loop');
  assert.deepEqual(cells[1].plugin.canvas3d.camera.getSnapshot(), { position: 9 });

  sandbox.plugin = { canvas3d: { camera: fakeCamera(0) } };
  cells[0].plugin.canvas3d.camera.setState({ position: 11 });
  cells[1].plugin.canvas3d = null;
  assert.doesNotThrow(() => frames.pop()(), 'a cell losing its canvas mid-loop must not break the loop');
  assert.equal(sandbox.plugin.canvas3d.camera.getSnapshot().position, 11,
    'expected the active Grid camera to be mirrored into the canonical viewer');

  stop();
});

test('Grid camera sharing preserves each molecular scene clipping envelope', async () => {
  const app = await readApp();
  const cameraSnapshotForScene = evaluateDeclaration(
    app, 'function cameraSnapshotForScene(sharedSnapshot, sceneSnapshot)', {});

  const larger = cameraSnapshotForScene(
      { position: [1, 2, 3], target: [0, 0, 0], radius: 12, radiusMax: 18 },
      { position: [9, 9, 9], target: [8, 8, 8], radius: 30, radiusMax: 48 },
    );
  assert.deepEqual(Array.from(larger.position), [1, 2, 3]);
  assert.deepEqual(Array.from(larger.target), [0, 0, 0]);
  assert.equal(larger.radius, 30);
  assert.equal(larger.radiusMax, 48,
    'the larger scene should keep safe near/far bounds');
  const smaller = cameraSnapshotForScene(
      { position: [1, 2, 3], radius: 30, radiusMax: 48 },
      { position: [9, 9, 9], radius: 12, radiusMax: 18 },
    );
  assert.equal(smaller.radius, 30);
  assert.equal(smaller.radiusMax, 48,
    'a smaller pane must not shrink the shared clipping envelope');
});

test('Weekly Grid pagination keeps the complete ballot in the sidebar', async () => {
  const app = await readApp();
  const all = Array.from({ length: 10 }, (_, choiceIndex) => ({ choiceIndex }));
  const pageTwo = [all[9]];
  const sandbox = {
    displayMode: 'grid',
    cur: { item: { source: 'weekly' } },
    allGridEntries: () => all,
    gridEntries: () => pageTwo,
    visibleChoices: () => [],
  };
  const choiceEntriesForSidebar = evaluateDeclaration(
    app, 'function choiceEntriesForSidebar()', sandbox);

  assert.deepEqual(choiceEntriesForSidebar(), all,
    'page 2 should change viewer tiles without hiding page-1 choices from the ballot');
  sandbox.cur.item.source = 'rnp';
  assert.deepEqual(choiceEntriesForSidebar(), pageTwo,
    'classic method pages retain their existing method-specific sidebar');
});

test('selecting an off-page Weekly sidebar pose opens its Grid page', async () => {
  const app = await readApp();
  const choices = Array.from({ length: 10 }, (_, choiceIndex) => ({ id: choiceIndex }));
  const entries = choices.map((choice, choiceIndex) => ({ choice, choiceIndex }));
  const calls = [];
  const sandbox = {
    displayMode: 'grid',
    cur: { item: { source: 'weekly' } },
    gridMethodIndex: 0,
    GRID_PAGE_SIZE: 9,
    allGridEntries: () => entries,
    sameChoice: (left, right) => left === right,
    viewerControlBlocked: () => false,
    onPick: async (choiceIndex, choice) => { calls.push(['pick', choiceIndex, choice.id]); },
    renderGridPages: () => { calls.push(['pages', sandbox.gridMethodIndex]); },
    renderUI: () => { calls.push(['ui', sandbox.gridMethodIndex]); },
    recordAppEvent: event => { calls.push(['trace', event]); },
    viewerRebuild: {
      enqueue: async (mutate, finalize) => {
        await mutate();
        calls.push(['rebuild', sandbox.gridMethodIndex]);
        await finalize();
      },
    },
  };
  sandbox.weeklyGridPageIndexForChoice = evaluateDeclaration(
    app, 'function weeklyGridPageIndexForChoice(choice)', sandbox);
  const pickSidebarEntry = evaluateDeclaration(app, 'async function pickSidebarEntry(entry)', sandbox);

  await pickSidebarEntry(entries[9]);

  assert.equal(sandbox.gridMethodIndex, 1);
  assert.deepEqual(calls, [
    ['pick', 9, 9],
    ['rebuild', 1],
    ['pages', 1],
    ['ui', 1],
    ['trace', 'grid_page_changed'],
  ]);
});

test('documents the standalone leaderboard page and the benchmark upload prerequisite', async () => {
  const readme = await readReadme();

  assert.match(readme, /leaderboard\.html/);
  assert.match(readme, /npm run upload:benchmark/);
  assert.match(readme, /benchmark demo (?:structures|assets)[\s\S]{0,160}before\s+the\s+demo/i);
});
