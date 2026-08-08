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
  assert.match(app, /\$\{rawPoseCount\} blind predicted poses/);
  assert.match(app, /method-blind clusters/);
  assert.match(app, /The protein and pocket change with the pose/);
  assert.match(app, /method-blind pose cluster/);
  assert.match(html, /\.seg\{display:flex;flex:none;/);
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
    gridEntries: () => [
      { choice: { pose_file: 'pose-a.pdb' } },
      { choice: { pose_file: 'pose-b.pdb' } },
    ],
    buildCanonicalLayer: async shown => {
      calls.push(`canonical:${shown.map(choice => choice.pose_file).join(',')}`);
    },
    buildSingleLayer: async () => { calls.push('single'); },
    buildGrid: async () => { calls.push('grid'); },
    hideGrid: () => { calls.push('hideGrid'); },
    $: () => ({ classList: {
      contains: () => false,
      add: (...names) => calls.push(`cover:${names.join(',')}`),
    } }),
    console: { warn: (...args) => calls.push(`warn:${args[1]}`) },
    ...overrides,
  };
  return sandbox;
}

test('Grid rebuilds the hidden canonical scene before the Grid tiles so traces stay replayable', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox();
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, [
    'cover:on,loading-grid',
    'canonical:pose-a.pdb,pose-b.pdb',
    'grid',
  ]);
});

test('a failed canonical rebuild still leaves the Grid tiles to load', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox({
    buildCanonicalLayer: async () => { throw new Error('pose download failed'); },
  });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, ['cover:on,loading-grid', 'warn:pose download failed', 'grid']);
});

test('leaving Grid keeps rebuilding the single view before disposing Grid viewers', async () => {
  const app = await readApp();
  const sandbox = gridLayerSandbox({
    displayMode: 'all',
    $: () => ({ classList: { contains: () => true } }),
  });
  const buildLayer = evaluateDeclaration(app, 'async function buildLayer()', sandbox);

  await buildLayer();

  assert.deepEqual(sandbox.calls, ['single', 'hideGrid']);
  assert.equal(sandbox.gridBuildRevision, 1);
});

test('question finalization preserves Grid framing but resets other modes before recording', async () => {
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
});

test('switching display mode renders choices after the queued mutation', async () => {
  const app = await readApp();
  const calls = [];
  const sandbox = {
    displayMode: 'grid',
    shownOne: 4,
    cur: { revealed: false, selected: { label: 'A' }, selectionExact: true, answerChoices: [{}] },
    interactionBlocked: () => false,
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
  assert.equal(sandbox.cur.selected, null, 'leaving Grid clears the Grid-exact selection');
});

test('Grid page switching rebuilds once with the new page already applied', async () => {
  const app = await readApp();
  const calls = [];
  const registry = elementRegistry();
  const sandbox = {
    displayMode: 'grid',
    gridMethodIndex: 0,
    cur: { gridMethods: ['af3', 'boltz'], showAnswer: false },
    methodName: method => method.toUpperCase(),
    interactionBlocked: () => false,
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
    clusterForChoice: () => cluster,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
  })([representative]);

  assert.deepEqual(JSON.parse(JSON.stringify(layers)), [
    { choice: firstGhost, ghost: true },
    { choice: secondGhost, ghost: true },
    { choice: representative, ghost: false },
  ]);
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
    OPTS: {},
    gridBuildRevision: 4,
    configurePlugin: () => {},
    viewerTraceRecorder: null,
    gridProteinUrls: () => ({ prot: 'rep-protein.pdb', pocket: 'rep-pocket.pdb', color: 3 }),
    loadStruct: async url => ({ struct: { url } }),
    addRep: async () => {},
    addSticks: async () => {},
    addPose: async (_struct, _color, _plugin, options) => { poseCalls.push(options || null); },
    acceptedChoiceCorrect: choice => choice.correct === true,
    sameChoice: (left, right) => left.pose_file === right.pose_file,
    GHOST_PROTEIN_ALPHA: 0.12,
    GHOST_POSE_ALPHA: 0.18,
    GHOST_POSE_SIZE: 0.14,
    structureSphere: () => ({ radius: 1 }),
    buildInteractions: async (...args) => { interactionCalls.push(args); },
    cameraChanges: target => target.canvas3d.camera.changed,
    window: { waitForCameraSettled: async ({ requestReset }) => requestReset() },
    GOOD: 0x2BA84A,
    BAD: 0xE23B2E,
  };
  const buildGridCell = evaluateDeclaration(app, 'async function buildGridCell(cell, revision)', sandbox);
  const cell = {
    entry: { choice: representative, cluster: { members: [representative, ghost] } },
    card: fakeElement(), head: fakeElement('button'), host: fakeElement(),
    viewer: null, plugin: null, disposed: false,
    spec: {
      item: { source: 'weekly' }, proteinMode: 'crystal', answer: false,
      clustered: true, showHbonds: true, showProteinEnsemble: true,
    },
  };

  await buildGridCell(cell, 4);

  assert.deepEqual(JSON.parse(JSON.stringify(poseCalls)), [
    { alpha: 0.18, sizeFactor: 0.14 },
    null,
  ]);
  assert.equal(interactionCalls.length, 1);
  assert.equal(interactionCalls[0][0], 'rep-pocket.pdb');
  assert.deepEqual(JSON.parse(JSON.stringify(interactionCalls[0][1])), ['rep.pdb']);
  assert.equal(resetCount, 1);
  assert.equal(cell.failed, undefined);
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
  assert.deepEqual(sandbox.plugin.canvas3d.camera.getSnapshot(), { position: 11 },
    'expected the active Grid camera to be mirrored into the canonical viewer');

  stop();
});

test('documents the standalone leaderboard page and the benchmark upload prerequisite', async () => {
  const readme = await readReadme();

  assert.match(readme, /leaderboard\.html/);
  assert.match(readme, /npm run upload:benchmark/);
  assert.match(readme, /benchmark demo (?:structures|assets)[\s\S]{0,160}before\s+the\s+demo/i);
});
