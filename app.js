// Pose Quiz — binding pocket (ligand removed) + shuffled, CLUSTERED ligand poses. Player picks the cluster they think
// is correct (or, in Hard mode, "none of these are correct"). Two quizzes: CAMEO (AlphaFold3 only) and
// Runs-n-Poses (poses pooled from multiple co-folding methods, ANONYMISED — the method is never rendered).
// Reuses the viewer's Mol* setup + its proven delete-and-rebuild pattern. Pose carbons are coloured by a
// NON-semantic per-cluster palette (random each question) only for identification — never correctness.

const PALETTE = [0x5B8FF9, 0xF6BD16, 0x9270CA, 0x5AD8A6, 0xE8964A, 0x6DC8EC,
  0xFF99C3, 0x8C6D31, 0xB5BD61, 0x17BECF, 0xBC80BD, 0xFDB462, 0x80B1D3, 0xFCCDE5, 0xB3DE69];
const LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
// Co-folding method display names — shown ONLY on the answer reveal (methods are anonymised during play).
const METHOD_NAMES = { af3: 'AF3', boltz: 'Boltz-1', boltz2: 'Boltz-2', chai: 'Chai-1', protenix: 'Protenix' };
const methodName = m => METHOD_NAMES[m] || m;
const GOOD = 0x2BA84A, BAD = 0xE23B2E, PROT = 0x9aa6b2, AF3PROT = 0x8FA8CC, XTAL = 0xC026D3;
// Convincing thresholds: a pose is CORRECT only if rmsd < 1.5 A; a clean WRONG distractor is > 3 A.
// game-able = has a <1.5 AND a >3 pose; all-wrong = EVERY pose > 3; 1.5-3 A limbo items are dropped.
const CORRECT_THRESH = 1.5, WRONG_THRESH = 3.0;
const HEAVY_MIN = 15;   // drop tiny-fragment ligands (< 15 heavy atoms) — keep substantial drug-like molecules
// all-correct ensembles are a Hard positive-control (catch over-"none"), NOT the main event — cap them so
// they stay a sprinkle instead of flooding Hard with easy wins. Tunable: max fraction of the rest of the pool.
const ALLCORRECT_MAX_FRAC = 0.2;
const HARD_MIX = { 'game-able': 0.40, 'all-wrong': 0.45, 'all-correct': 0.15 };

const OPTS = {
  layoutIsExpanded: false, layoutShowControls: false, layoutShowRemoteState: false,
  layoutShowSequence: false, layoutShowLog: false, layoutShowLeftPanel: false,
  viewportShowExpand: false, viewportShowControls: false, viewportShowSettings: false,
  viewportShowSelectionMode: false, viewportShowAnimation: false, viewportShowTrajectoryControls: false,
};

const DEV = new URLSearchParams(location.search).has('dev');   // no-vote inspection/browse mode (?dev=1)
const researchBackend = () => DEV ? null : window.foldariumBackend;
const assetUrl = path => window.foldariumAssetUrl?.(path) || path;
let viewer, plugin, ITEMS = [], idx = 0, cur = null;
let POOLS = { cameo: [], rnp: [] }, quizSource = 'cameo', difficulty = 'easy';
let remoteSessionId = null;
let viewerTraceRecorder = null;
let viewerRebuild = null, revealAfterIdle = null, revealRequested = false;
let viewerTransitionBusy = false;
let displayMode = 'all', clustered = true, shownOne = 0, showXtal = false, proteinMode = 'crystal';
let showHbonds = false;   // H-bond overlay toggle — persisted across questions like the other view choices
let gridViewers = [], gridBuildRevision = 0, gridMethodIndex = 0;
let stopGridCameraSync = null, stopGridLayout = null;
// The user's chosen "my view" display preferences, persisted ACROSS questions. reveal()/toggleAnswer()
// temporarily override the live globals to render the correctness list (always all/unclustered), so we
// remember the user's real choice here and restore/seed from it (loadQuestion, back-to-my-view).
let userView = { displayMode: 'all', clustered: true, proteinMode: 'crystal', showHbonds: false };
const rememberView = () => { userView = { displayMode, clustered, proteinMode, showHbonds }; };
const applyUserView = () => {
  ({ displayMode, clustered, proteinMode, showHbonds } = userView);
  if (quizSource === 'rnp') proteinMode = 'crystal';   // RnP has no per-pose AF3 protein
};
let score = { you: 0, af3: 0, n: 0, randExp: 0 };
const $ = s => document.querySelector(s);
const CACHE_BUST = Date.now();
const hex = c => '#' + c.toString(16).padStart(6, '0');
// "locked" = the green/red answer is on screen; controls are inert only then. In "my view" (revealed but
// answer hidden) everything is interactive again, exactly as before voting.
const locked = () => cur && cur.revealed && cur.showAnswer;
const interactionBlocked = () => viewerTransitionBusy || revealRequested || locked();
const oppLabel = () => (quizSource === 'rnp' ? 'Best automated pick (ligand pLDDT)' : 'AlphaFold3 (pLDDT-ranked)');

function setViewerControlsBusy(busy) {
  viewerTransitionBusy = busy;
  document.querySelectorAll(
    '#choices button, #mode button, #protmode button, #uncluster, #hbonds, #lock, '
    + '#next, #prev, #myview, #showXtal, #start, #gridpages button',
  ).forEach(control => { control.disabled = busy; });
  if (!busy && cur && !cur.revealed) {
    $('#lock').disabled = revealRequested || cur.selected == null;
  }
}

async function loadStruct(url, format, targetPlugin = plugin) {
  const data = await targetPlugin.builders.data.download({ url: assetUrl(url) + '?v=' + CACHE_BUST, isBinary: false });
  const traj = await targetPlugin.builders.structure.parseTrajectory(data, format);
  const model = await targetPlugin.builders.structure.createModel(traj);
  const struct = await targetPlugin.builders.structure.createStructure(model);
  return { data, struct };
}
async function fetchPdbText(url) {   // raw PDB text (for merging pocket+pose into ONE structure for interactions)
  const r = await fetch(assetUrl(url) + '?v=' + CACHE_BUST);
  return r.ok ? await r.text() : '';
}
// keep only ATOM/HETATM/TER records so concatenated files parse as a single model (drop END/CONECT/etc.)
const atomRecords = t => t.split('\n').filter(l => /^(ATOM|HETATM|TER)/.test(l)).join('\n');
async function addRep(struct, selector, type, color, alpha = 1, targetPlugin = plugin) {
  const comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, selector);
  if (!comp) return null;
  return targetPlugin.builders.structure.representation.addRepresentation(comp, {
    type, typeParams: { alpha }, color: 'uniform', colorParams: { value: color },
  });
}
async function addPose(struct, carbon, targetPlugin = plugin) {
  let comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'ligand');
  if (!comp) comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return targetPlugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor: 0.24 },
    color: 'element-symbol', colorParams: { carbonColor: { name: 'uniform', params: { value: carbon } } },
  });
}
async function addSticks(struct, sizeFactor, alpha, targetPlugin = plugin) {
  const comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return targetPlugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor, alpha }, color: 'element-symbol',
  });
}
function shuffle(a) { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }

// ---- visible-choice logic --------------------------------------------------------------------
function visibleChoices() {
  return clustered ? cur.clusters.map(c => c.rep) : cur.clusters.flatMap(c => c.members);
}

function configurePlugin(targetPlugin) {
  try {
    targetPlugin.canvas3d?.setProps({
      camera: { manualReset: true }, cameraResetDurationMs: 0,
      renderer: { backgroundColor: 0xffffff },
    });
  } catch (e) {}
}
const structureSphere = selector => selector?.obj?.data?.boundary?.sphere;
function focusLigandSpheres(targetPlugin, spheres) {
  const valid = spheres.filter(Boolean);
  if (!valid.length || !targetPlugin?.canvas3d) return false;
  targetPlugin.managers.camera.focusSpheres(valid, sphere => sphere,
    { minRadius: 8, extraRadius: 4, durationMs: 0 });
  return true;
}
function sameChoice(a, b) { return !!(a && b && (a === b || a.pose_file === b.pose_file)); }
function gridPageMethod() {
  const methods = cur?.gridMethods || [];
  if (!methods.length) return null;
  gridMethodIndex = Math.min(gridMethodIndex, methods.length - 1);
  return methods[gridMethodIndex];
}
function gridEntriesFor(method) {
  const vis = visibleChoices();
  if (!method) return vis.map((choice, choiceIndex) => {
    const cluster = cur.clusters.find(c => c.members.includes(choice));
    return { choice, choiceIndex, cluster, memberCount: clustered ? cluster.members.length : 1 };
  });
  if (!clustered) return vis.map((choice, choiceIndex) => ({ choice, choiceIndex,
    cluster: cur.clusters.find(c => c.members.includes(choice)), memberCount: 1 }))
    .filter(x => x.choice._method === method);
  return cur.clusters.map((cluster, choiceIndex) => {
    const members = cluster.members.filter(c => c._method === method);
    const choice = members.find(c => c.is_rep) || members[0];
    return choice ? { choice, choiceIndex, cluster, memberCount: members.length } : null;
  }).filter(Boolean);
}
function gridEntries() { return gridEntriesFor(gridPageMethod()); }
function allGridEntries() {
  const methods = cur?.gridMethods || [];
  return methods.length ? methods.flatMap(gridEntriesFor) : gridEntriesFor(null);
}
function gridChoiceSelected(choice) {
  if (!cur?.selected || cur.selected.none) return false;
  return cur.selectionExact ? sameChoice(choice, cur.selected) : choice.cluster === cur.selected.cluster;
}
function syncGridSelection() {
  for (const cell of gridViewers) {
    cell.card?.classList.toggle('selected', !cell.failed && gridChoiceSelected(cell.entry.choice));
  }
}
function renderGridPages() {
  const nav = $('#gridpages'), methods = cur?.gridMethods || [];
  if (!cur || displayMode !== 'grid' || methods.length < 2) { nav.style.display = 'none'; nav.innerHTML = ''; return; }
  nav.style.display = ''; nav.innerHTML = '';
  methods.forEach((method, i) => {
    const b = document.createElement('button');
    b.classList.toggle('on', i === gridMethodIndex);
    b.textContent = cur.showAnswer ? methodName(method) : `Set ${i + 1}`;
    b.onclick = async () => {
      if (i === gridMethodIndex || interactionBlocked()) return;
      await viewerRebuild.enqueue(
        () => { gridMethodIndex = i; },
        () => { renderGridPages(); renderUI(); },
      );
    };
    nav.appendChild(b);
  });
}
function gridProteinUrls(choice, spec) {
  const item = spec.item;
  if (spec.proteinMode === 'af3' && item.afprotein_ref) {
    return { prot: choice.afprotein_file || item.afprotein_ref,
      pocket: choice.afpocket_file || item.afpocket_union, color: AF3PROT };
  }
  return { prot: item.protein_file, pocket: item.pocket_file, color: PROT };
}
function gridHeader(entry) {
  const c = entry.choice, answer = cur.revealed && cur.showAnswer;
  const bits = [];
  if (clustered && entry.memberCount > 1) bits.push(`${entry.memberCount} poses`);
  if (answer) {
    bits.push(`${c.rmsd.toFixed(2)} Å`);
    if (cur.item.source === 'rnp' && c._method) bits.push(methodName(c._method));
    if (gridChoiceSelected(c)) bits.push('YOU');
    if (c.af3_sample === cur.item.plddt_pick_sample) bits.push('AI');
  }
  const color = answer ? (c.correct ? GOOD : BAD) : c.color;
  return `<span class="grid-dot" style="background:${hex(color)}"></span><span>Pose ${c.label}</span>`
    + (bits.length ? `<span class="grid-meta">· ${bits.join(' · ')}</span>` : '');
}
function disposeGridViewers() {
  if (stopGridCameraSync) { stopGridCameraSync(); stopGridCameraSync = null; }
  if (stopGridLayout) { stopGridLayout(); stopGridLayout = null; }
  for (const cell of gridViewers) { cell.disposed = true; try { cell.viewer?.dispose(); } catch (e) {} }
  gridViewers = []; $('#gridcells').replaceChildren();
}
function layoutGrid() {
  const view = $('#gridview'), box = $('#gridcells'), n = gridViewers.length;
  if (!n || !view.classList.contains('on')) return;
  const width = view.clientWidth - 20, height = view.clientHeight - 20, gap = 10, aspect = 4 / 3;
  let best = null;
  for (let columns = 1; columns <= n; columns++) {
    const rows = Math.ceil(n / columns);
    const tileWidth = Math.min((width - gap * (columns - 1)) / columns, (height - gap * (rows - 1)) / rows * aspect);
    if (!best || tileWidth > best.tileWidth) best = { tileWidth, tileHeight: tileWidth / aspect };
  }
  if (!best || best.tileWidth <= 0) return;
  box.style.setProperty('--grid-card-w', `${Math.floor(best.tileWidth * 10) / 10}px`);
  box.style.setProperty('--grid-card-h', `${Math.floor(best.tileHeight * 10) / 10}px`);
  for (const cell of gridViewers) cell.viewer?.handleResize?.();
}
function startGridLayout() {
  const observer = new ResizeObserver(layoutGrid);
  observer.observe($('#gridview')); layoutGrid();
  stopGridLayout = () => observer.disconnect();
}
function hideGrid() {
  gridBuildRevision++; disposeGridViewers(); $('#gridview').classList.remove('on', 'loading-grid'); renderGridPages();
}
function syncGridCameras(cells) {
  const cameraSnapshot = cell => cell.plugin?.canvas3d?.camera?.getSnapshot?.();
  let enabled = true, raf = 0, last = cells.map(cell => JSON.stringify(cameraSnapshot(cell)));
  const tick = () => {
    if (!enabled) return;
    const snapshots = cells.map(cameraSnapshot);
    const source = snapshots.findIndex((snapshot, i) => snapshot && JSON.stringify(snapshot) !== last[i]);
    if (source >= 0) {
      const snapshot = snapshots[source];
      for (let i = 0; i < cells.length; i++) {
        if (i !== source) cells[i].plugin?.canvas3d?.camera?.setState(snapshot, 0);
      }
      // mirror into the hidden canonical viewer so the trace recorder still sees Grid camera movement
      try { plugin?.canvas3d?.camera?.setState(snapshot, 0); } catch (e) {}
      last = cells.map(cell => JSON.stringify(cameraSnapshot(cell)));
    }
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => { enabled = false; cancelAnimationFrame(raf); };
}
async function buildGridCell(cell, revision) {
  try {
    const gridViewer = await molstar.Viewer.create(cell.host, { ...OPTS, extensions: [] });
    cell.viewer = gridViewer; cell.plugin = gridViewer.plugin;
    if (revision !== gridBuildRevision || cell.disposed) return gridViewer.dispose();
    configurePlugin(cell.plugin);
    const c = cell.entry.choice, urls = gridProteinUrls(c, cell.spec);
    const pr = await loadStruct(urls.prot, 'pdb', cell.plugin);
    await addRep(pr.struct, 'polymer', 'cartoon', urls.color, 0.5, cell.plugin);
    if (urls.pocket) { const ps = await loadStruct(urls.pocket, 'pdb', cell.plugin); await addSticks(ps.struct, 0.16, 0.95, cell.plugin); }
    const pose = await loadStruct(c.pose_file, 'pdb', cell.plugin);
    await addPose(pose.struct, cell.spec.answer ? (c.correct ? GOOD : BAD) : c.color, cell.plugin);
    cell.poseSphere = structureSphere(pose.struct);
    if (revision === gridBuildRevision && !cell.disposed) { cell.viewer.handleResize?.(); cell.plugin.canvas3d.requestCameraReset(); }
  } catch (e) {
    try { cell.viewer?.dispose(); } catch (_) {}
    cell.viewer = null; cell.plugin = null;
    if (!cell.disposed && revision === gridBuildRevision) {
      cell.failed = true;
      cell.head.disabled = true;
      cell.head.onclick = null;
      cell.card.classList.add('failed');
      const message = document.createElement('div');
      message.className = 'grid-error';
      message.textContent = `Could not load this pose viewer. ${e.message}`;
      cell.host.replaceChildren(message);
    }
  }
}
async function buildGrid(preserveCamera = true) {
  const previousCamera = preserveCamera ? gridViewers.find(cell => cell.plugin?.canvas3d)?.plugin.canvas3d.camera.getSnapshot() : null;
  const revision = ++gridBuildRevision;
  disposeGridViewers();
  const view = $('#gridview'), cellsBox = $('#gridcells');
  view.classList.add('on', 'loading-grid'); renderGridPages();
  const cells = gridEntries().map(entry => {
    const card = document.createElement('div');
    card.className = 'grid-card' + ((cur.revealed && cur.showAnswer) ? (entry.choice.correct ? ' correct' : ' wrong') : '');
    const head = document.createElement('button');
    head.type = 'button'; head.className = 'grid-head'; head.innerHTML = gridHeader(entry); head.disabled = locked();
    head.onclick = () => { if (!locked()) onPick(entry.choiceIndex, entry.choice); };
    const host = document.createElement('div'); host.className = 'grid-host'; card.append(host, head); cellsBox.appendChild(card);
    return { entry, card, head, host, viewer: null, plugin: null, poseSphere: null, disposed: false,
      spec: { item: cur.item, proteinMode, answer: cur.revealed && cur.showAnswer } };
  });
  gridViewers = cells; startGridLayout(); syncGridSelection();
  await Promise.allSettled(cells.map(cell => buildGridCell(cell, revision)));
  if (revision !== gridBuildRevision) return;
  const active = cells.filter(cell => cell.plugin?.canvas3d);
  if (active.length) {
    const snapshot = previousCamera || active[0].plugin.canvas3d.camera.getSnapshot();
    for (const cell of active) cell.plugin.canvas3d.camera.setState(snapshot, 0);
    try { plugin.canvas3d?.camera?.setState(snapshot, 0); } catch (e) {}
    stopGridCameraSync = syncGridCameras(active);
  }
  view.classList.remove('loading-grid'); syncGridSelection();
}

// ---- camera persistence (viewer pattern): keep the view while scrolling poses --------------------
let savedCam = null;
function saveCam() { try { savedCam = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) { savedCam = null; } }
function restoreCam() { try { if (savedCam) plugin.canvas3d.camera.setState(savedCam, 0); } catch (e) {} }

// ---- two layers: a FIXED reference (crystal protein cartoon + crystal pocket sticks, built once per
//      question so the backbone never moves) and the rebuilt POSE layer (ligands + crystal-reveal). -----
let proteinData = [], layerData = [], hbondData = [], currentProtUrl = null;
function protUrls() {
  const answer = cur.revealed && cur.showAnswer;
  if (proteinMode === 'af3' && cur.item.afprotein_ref) {   // CAMEO only; RnP has no per-pose AF3 protein
    const vis = visibleChoices();
    const shown = vis[Math.min(shownOne, vis.length - 1)];
    if (displayMode === 'one' && !answer && shown && shown.afprotein_file)
      return { prot: shown.afprotein_file, pocket: shown.afpocket_file };
    return { prot: cur.item.afprotein_ref, pocket: cur.item.afpocket_union };
  }
  return { prot: cur.item.protein_file, pocket: cur.item.pocket_file };
}
async function buildProtein() {         // rebuilds ONLY when the target protein changes (no flicker)
  const { prot, pocket } = protUrls();
  if (prot === currentProtUrl) return;
  if (proteinData.length) { const b = plugin.build(); for (const x of proteinData) b.delete(x.ref || x); await b.commit(); proteinData = []; }
  const pr = await loadStruct(prot, 'pdb');
  proteinData.push(pr.data);
  await addRep(pr.struct, 'polymer', 'cartoon', proteinMode === 'af3' ? AF3PROT : PROT, 0.5);
  if (pocket) {
    const ps = await loadStruct(pocket, 'pdb');
    proteinData.push(ps.data);
    await addSticks(ps.struct, 0.16, 0.95);
  }
  currentProtUrl = prot;
}
async function clearLayer() {
  if (!layerData.length && !hbondData.length) return;
  const b = plugin.build();
  for (const d of layerData) b.delete(d.ref || d);
  for (const d of hbondData) b.delete(d.ref || d);
  await b.commit();
  layerData = []; hbondData = [];
}
// H-bond overlay: interactions are computed WITHIN a single structure, but our pocket and each pose are
// separate structures, so we merge the pocket PDB + the shown pose PDB(s) into ONE combined structure and
// render Mol*'s built-in 'interactions' representation over it (dashed cylinders). This is treated as an
// "H-bonds" affordance; Mol*'s default provider set is H-bond-dominated (see report note). Poses stay
// anonymised (geometry only) and correctness is never revealed — all shown poses are treated equally.
async function buildHbonds(poseUrls) {
  if (!showHbonds || !poseUrls.length) return;
  const { pocket } = protUrls();
  if (!pocket) return;
  const parts = [atomRecords(await fetchPdbText(pocket))];
  for (const u of poseUrls) parts.push(atomRecords(await fetchPdbText(u)));
  const pdb = parts.filter(Boolean).join('\nTER\n') + '\nEND\n';
  const data = await plugin.builders.data.rawData({ data: pdb });
  hbondData.push(data);
  const traj = await plugin.builders.structure.parseTrajectory(data, 'pdb');
  const model = await plugin.builders.structure.createModel(traj);
  const struct = await plugin.builders.structure.createStructure(model);
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return;
  await plugin.builders.structure.representation.addRepresentation(comp, { type: 'interactions' });
}
async function buildCanonicalLayer(shown) {
  saveCam();
  await buildProtein();                 // swap protein only if it changed (AF3 one-at-a-time, or toggle)
  await clearLayer();
  const answer = cur.revealed && cur.showAnswer;        // green/red reveal vs the anonymised "my view"
  for (const c of shown) {
    const s = await loadStruct(c.pose_file, 'pdb');
    layerData.push(s.data);
    await addPose(s.struct, answer ? (c.correct ? GOOD : BAD) : c.color);
  }
  // crystal reference (true pose) — only after reveal, when toggled on
  const hbondPoses = shown.map(c => c.pose_file);
  if (cur.revealed && showXtal && cur.item.xtal_lig_file) {
    const xl = await loadStruct(cur.item.xtal_lig_file, 'pdb');
    layerData.push(xl.data);
    await addPose(xl.struct, XTAL);
    hbondPoses.push(cur.item.xtal_lig_file);   // also show the crystal reference's H-bonds when it's visible
  }
  await buildHbonds(hbondPoses);        // H-bond overlay for whatever pose(s) are currently shown
  restoreCam();
  viewerTraceRecorder?.captureState();
}
async function buildSingleLayer() {
  const answer = cur.revealed && cur.showAnswer;
  const vis = visibleChoices();
  const shown = answer || displayMode === 'all' ? vis : [vis[Math.min(shownOne, vis.length - 1)]];
  return buildCanonicalLayer(shown);
}
async function buildLayer() {
  if (displayMode === 'grid') {
    try {
      await buildCanonicalLayer(gridEntries().map(entry => entry.choice));
    } catch (error) {
      console.warn('Canonical Grid scene could not be built:', error.message);
    }
    return buildGrid();
  }
  if ($('#gridview').classList.contains('on')) {
    gridBuildRevision++;
    try { return await buildSingleLayer(); }
    finally { hideGrid(); }
  }
  hideGrid();
  return buildSingleLayer();
}

function requestQuestionCameraReset() {
  if (displayMode !== 'grid') plugin.canvas3d?.requestCameraReset?.();
}

async function loadQuestion(i) {
  const item = ITEMS[i];
  $('#stage').classList.add('loading-system');
  // build cluster objects in shuffled order, colour per cluster
  const byCluster = {};
  item.choices.forEach(c => (byCluster[c.cluster] ??= []).push({ ...c }));
  const clusters = shuffle(Object.values(byCluster)).map((members, k) => {
    const color = PALETTE[k % PALETTE.length], label = LABELS[k % LABELS.length];
    members.forEach((m, j) => { m.color = color; m.label = label + (members.length > 1 ? '·' + (j + 1) : ''); });
    return { label, color, members, rep: members.find(m => m.is_rep) || members[0] };
  });
  await viewerRebuild.enqueue(
    async () => {
      viewerTraceRecorder?.stop();
      idx = i;
      const gridMethods = item.source === 'rnp'
        ? shuffle([...new Set(item.choices.map(c => c._method).filter(Boolean))]) : [];
      cur = { item, clusters, gridMethods, selected: null, selectionExact: false, answerChoices: [], revealed: false, showAnswer: false };
      gridMethodIndex = 0;
      // Seed view preferences from the player's last choice, then reset question-specific navigation/reveal state.
      applyUserView();
      shownOne = 0;
      $('#myview').style.display = 'none'; $('#start').style.display = 'none';
      $('#xtalrow').style.display = 'none'; $('#showXtal').checked = false;
      try { await plugin.clear(); } catch (e) {}
      proteinData = []; layerData = []; hbondData = []; savedCam = null; currentProtUrl = null;
      showXtal = false;
      syncButtons();
    },
    async () => {
      await window.waitForCameraSettled({
        cameraChanged: plugin.canvas3d?.camera?.changed,
        requestReset: requestQuestionCameraReset,
      });
      viewerTraceRecorder?.start();
      renderUI();
      requestAnimationFrame(() => requestAnimationFrame(() => $('#stage').classList.remove('loading-system')));
    },
  );
}

function renderUI() {
  $('#progress').textContent = DEV ? `item ${idx + 1} / ${ITEMS.length} · dev`
                                   : `question ${idx + 1} / ${ITEMS.length}`;
  $('#ligand').innerHTML = `${cur.item.ligand} <small>· ${cur.clusters.length} distinct pose clusters</small>`;
  const box = $('#choices'); box.innerHTML = '';
  const uiEntries = displayMode === 'grid'
    ? gridEntries()
    : visibleChoices().map((choice, choiceIndex) => ({ choice, choiceIndex,
      cluster: cur.clusters.find(c => c.members.includes(choice)), memberCount: 1 }));
  uiEntries.forEach(entry => {
    const c = entry.choice, k = entry.choiceIndex;
    const b = document.createElement('button');
    b.className = 'choice'; b.dataset.k = k; b.disabled = viewerTransitionBusy;
    let nm;
    if (clustered) {
      const cl = entry.cluster;
      const label = displayMode === 'grid' ? c.label : cl.label;
      const count = displayMode === 'grid' ? entry.memberCount : cl.members.length;
      nm = `Pose ${label}` + (count > 1
        ? ` <span style="color:var(--faint)">(${count} poses)</span>` : '');
    } else nm = `Pose ${c.label}`;
    b.innerHTML = `<span class="sw" style="background:${hex(c.color)}"></span><span class="nm">${nm}</span><span class="tag" data-tag></span>`;
    b.onclick = () => onPick(k, displayMode === 'grid' ? c : null);
    box.appendChild(b);
  });
  if (difficulty === 'hard') {                          // the detect-game option
    const nb = document.createElement('button');
    nb.className = 'choice none'; nb.dataset.k = 'none'; nb.disabled = viewerTransitionBusy;
    nb.innerHTML = `<span class="sw" style="background:#5a6675;border-style:dashed"></span><span class="nm">None of these are correct</span>`;
    nb.onclick = () => onPick('none');
    box.appendChild(nb);
  }
  if (cur.selected) {                                   // keep the player's pick highlighted
    if (cur.selected.none) box.querySelector('.choice.none')?.classList.add('sel');
    else {
      const k = uiEntries.findIndex(entry => cur.selectionExact
        ? sameChoice(entry.choice, cur.selected) : entry.choice.cluster === cur.selected.cluster);
      if (k >= 0) box.querySelectorAll('.choice')[k]?.classList.add('sel');
    }
  }
  if (DEV) { renderDevNav(); return; }                  // dev: free browse, no vote/lock/score
  $('#lock').disabled = viewerTransitionBusy || cur.selected == null; $('#lock').style.display = cur.revealed ? 'none' : '';
  $('#verdict').style.display = cur.revealed ? '' : 'none';
  $('#next').style.display = cur.revealed ? '' : 'none';
  updateScore();
}

// dev-only chrome: Prev/Next that work on every item (no lock), + the reveal-answer toggle. The score panel
// and the verdict box stay hidden; nothing is logged.
function renderDevNav() {
  $('#lock').style.display = 'none';
  $('#verdict').style.display = 'none';
  $('#prev').style.display = ''; $('#next').style.display = '';
  $('#next').textContent = 'Next →';
  $('#myview').style.display = '';
  $('#myview').textContent = cur.showAnswer ? '← Hide answer (my view)' : 'Reveal answer →';
  $('#xtalrow').style.display = (cur.showAnswer && cur.item.xtal_lig_file) ? '' : 'none';
}

// items for the current (source, difficulty) selection. Easy = only game-able (a real pick puzzle).
// Hard = everything: game-able + all-wrong + all-correct (all-correct excluded from Easy — a pick with
// no wrong answer is no puzzle; it belongs in Hard as the positive control for the "none of these" call).
function filteredPool() {
  return POOLS[quizSource].filter(it => difficulty === 'hard' ? true : it.bucket === 'game-able' && it.easyPlayable);
}

// Easy needs a real pick puzzle in the choices a clustered view can reach: a pose under the correct
// threshold and a clearly wrong one. Lifted out of norm() unchanged so the ported eligibility rule (global
// cluster representatives, plus the pooled Runs-n-Poses per-method representatives) keeps the same pool.
function easyPlayable(choices, source) {
  const byCluster = {};
  for (const choice of choices) (byCluster[choice.cluster] ??= []).push(choice);
  const clusters = Object.values(byCluster);
  const globalReps = clusters.map(members => members.find(choice => choice.is_rep) || members[0]);
  const candidateSets = [globalReps];
  if (source === 'rnp') {
    const methods = [...new Set(choices.map(choice => choice._method).filter(Boolean))];
    candidateSets.push(methods.flatMap(method => clusters.map(members => {
      const fromMethod = members.filter(choice => choice._method === method);
      return fromMethod.find(choice => choice.is_rep) || fromMethod[0];
    }).filter(Boolean)));
  }
  const isPickPuzzle = candidates => candidates.some(choice => choice.rmsd < CORRECT_THRESH)
    && candidates.some(choice => choice.rmsd > WRONG_THRESH);
  return candidateSets.every(isPickPuzzle);
}

function showIntro() {
  cur = null;                                  // leaving play: protmode/uncluster gate on cur in syncButtons
  const pool = filteredPool();
  $('#setup').style.display = '';
  $('#mode').style.display = 'none'; $('#protmode').style.display = 'none'; $('#modehint').style.display = 'none';
  $('#choices').innerHTML = ''; $('#lock').style.display = 'none'; $('#uncluster').style.display = 'none';
  $('#hbonds').style.display = 'none';
  $('#myview').style.display = 'none'; $('#xtalrow').style.display = 'none';
  $('#progress').textContent = 'ready';
  $('#ligand').innerHTML = `${pool.length} single-pocket ensembles · ${quizSource === 'rnp' ? 'Runs-n-Poses' : 'CAMEO'}`;
  // AI baseline accuracy on this pool: pLDDT-pick correct (all-wrong -> always wrong; the model can't say "none")
  const aiCorrect = pool.filter(it => it.choices.find(c => c.af3_sample === it.plddt_pick_sample)?.correct).length;
  const pct = pool.length ? Math.round(100 * aiCorrect / pool.length) : 0;
  $('#setuphint').innerHTML = (difficulty === 'easy'
    ? 'Easy — every ensemble has a correct pose; pick it.'
    : 'Hard — some ensembles have a correct pose, some have <b>none</b> (answer “none of these”); you decide which.')
    + ' <b>Single pocket only</b> (multi-pocket coming later).';
  const v = $('#verdict'); v.style.display = '';
  v.innerHTML = `Each question: a binding pocket with the ligand removed + `
    + (quizSource === 'rnp' ? 'anonymised poses pooled from <b>multiple co-folding methods</b>' : "<b>AlphaFold3</b>'s poses")
    + ` (clustered). Pick the correct binding pose`
    + (difficulty === 'hard' ? ', or <b>“none of these are correct.”</b>' : '.')
    + `<br><br>Opponent = ${oppLabel()}. <b>It scored ${aiCorrect}/${pool.length} (${pct}%)</b> here`
    + (difficulty === 'hard' ? ` — and it can never answer “none”, so the no-correct-pose items are yours to win.` : '.')
    + ` Can you beat it?`;
  $('#start').style.display = pool.length ? '' : 'none';
  if (!pool.length) v.innerHTML += '<br><span style="color:var(--bad)">No items for this selection.</span>';
}

const SESSION_SIZE = 30;   // a completable sitting; re-play draws a fresh random subset
function hardSessionQuotas(size, mix) {
  const entries = Object.entries(mix).map(([bucket, fraction], index) => {
    const exact = size * fraction;
    return { bucket, count: Math.floor(exact), remainder: exact % 1, index };
  });
  const quotas = Object.fromEntries(entries.map(entry => [entry.bucket, entry.count]));
  const remaining = size - entries.reduce((total, entry) => total + entry.count, 0);
  entries.sort((a, b) => b.remainder - a.remainder || a.index - b.index);
  for (const entry of entries.slice(0, remaining)) quotas[entry.bucket] += 1;
  return quotas;
}
function drawSession() {
  const pool = filteredPool();
  if (DEV) return shuffle(pool.slice());
  if (difficulty !== 'hard') return shuffle(pool.slice()).slice(0, SESSION_SIZE);
  const by = { 'game-able': [], 'all-wrong': [], 'all-correct': [] };
  for (const it of shuffle(pool.slice())) if (by[it.bucket]) by[it.bucket].push(it);
  const picked = [], used = new Set();
  const quotas = hardSessionQuotas(SESSION_SIZE, HARD_MIX);
  for (const bucket in quotas)
    for (const item of by[bucket].slice(0, quotas[bucket])) { picked.push(item); used.add(item); }
  if (picked.length < SESSION_SIZE)
    for (const item of shuffle(pool.slice())) { if (picked.length >= SESSION_SIZE) break; if (!used.has(item)) { picked.push(item); used.add(item); } }
  return shuffle(picked).slice(0, SESSION_SIZE);
}
function startQuiz() {
  remoteSessionId = researchBackend()?.startSession({
    source: quizSource,
    difficulty,
  }) ?? null;
  ITEMS = drawSession();
  if (quizSource === 'rnp') proteinMode = 'crystal';
  rememberView();   // snapshot the starting view as the persisted baseline for this session
  $('#setup').style.display = 'none'; $('#start').style.display = 'none'; $('#mode').style.display = '';
  $('#protmode').style.display = quizSource === 'rnp' ? 'none' : '';
  $('#lbl-af3').textContent = oppLabel();
  loadQuestion(0);
}

async function onPick(k, exactChoice = null) {
  if (interactionBlocked()) return;
  const answerChoices = displayMode === 'grid' ? allGridEntries().map(entry => entry.choice) : visibleChoices();
  if (k !== 'none' && displayMode === 'one') {
    const selected = cur.revealed ? null : visibleChoices()[k];
    await viewerRebuild.enqueue(() => {
      shownOne = k;
      if (!cur.revealed) {
        cur.selected = selected;
        document.querySelectorAll('.choice').forEach(el => el.classList.toggle('sel', el.dataset.k == k));
      }
    });
    return;
  }
  if (cur.revealed) return;  // my-view navigation is meaningful only in one-at-a-time mode
  if (k === 'none') {
    cur.selected = { none: true, correct: !answerChoices.some(c => c.correct), label: 'None of these' };
    cur.selectionExact = displayMode === 'grid' || !clustered;
    cur.answerChoices = answerChoices;
    document.querySelectorAll('.choice').forEach(el => el.classList.toggle('sel', el.dataset.k === 'none'));
    $('#lock').disabled = false;
    return;
  }
  cur.selected = exactChoice || visibleChoices()[k];
  cur.selectionExact = !!exactChoice || !clustered;
  cur.answerChoices = answerChoices;
  document.querySelectorAll('.choice').forEach(el => el.classList.toggle('sel', el.dataset.k == k));
  syncGridSelection();
  $('#lock').disabled = false;
}

async function reveal() {
  if (cur.selected == null || cur.revealed || revealRequested) return;
  revealRequested = true;
  $('#lock').disabled = true;
  try {
    await revealAfterIdle();
  } finally {
    revealRequested = false;
    if (cur && !cur.revealed) $('#lock').disabled = cur.selected == null;
  }
}

async function finalizeReveal() {
  if (cur.selected == null || cur.revealed) return;
  const viewerTrace = viewerTraceRecorder?.stop() ?? null;
  await viewerRebuild.enqueue(() => {
    const keepGrid = displayMode === 'grid';
    cur.revealed = true; cur.showAnswer = true;
    if (!keepGrid) { displayMode = 'all'; clustered = false; }
    syncButtons();
  });
  const picked = cur.selected;
  const af3 = cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
  const youRight = !!picked.correct, af3Right = !!(af3 && af3.correct);
  score.n++; score.you += youRight; score.af3 += af3Right;
  const answerChoices = cur.answerChoices.length ? cur.answerChoices : cur.clusters.map(c => c.rep);
  const nCorrect = answerChoices.filter(c => c.correct).length;
  const opts = answerChoices.length + (difficulty === 'hard' ? 1 : 0);
  score.randExp += (nCorrect || (difficulty === 'hard' ? 1 : 0)) / opts;
  renderRevealList(picked, af3);
  $('#lock').style.display = 'none';
  const youMsg = picked.none
    ? (youRight ? `<b style="color:var(--good)">Correct — none of these were right.</b>`
                : `<b style="color:var(--bad)">Wrong</b> — a correct pose was present.`)
    : (youRight ? `<b style="color:var(--good)">Correct.</b> Pose ${picked.label} is ${picked.rmsd.toFixed(2)} Å from crystal.`
                : `<b style="color:var(--bad)">Wrong.</b> Pose ${picked.label} is ${picked.rmsd.toFixed(2)} Å off.`);
  const afMethod = (cur.item.source === 'rnp' && af3 && af3._method) ? ` (${methodName(af3._method)})` : '';
  const afMsg = af3
    ? `${oppLabel()} picked Pose ${af3.label}${afMethod} — <b style="color:${af3Right ? 'var(--good)' : 'var(--bad)'}">${af3Right ? 'right' : 'wrong'}</b>`
      + (!cur.item.has_correct ? ` (can’t answer “none”)` : '')
    : '';
  const v = $('#verdict'); v.style.display = '';
  v.innerHTML = youMsg + (afMsg ? '<br>' + afMsg : '') + (youRight && !af3Right ? ` — <b>you beat it.</b>` : '.');
  $('#next').style.display = ''; $('#next').textContent = idx + 1 < ITEMS.length ? 'Next →' : 'Final score →';
  $('#myview').style.display = ''; $('#myview').textContent = '← Back to my view (hide answer)';
  if (cur.item.xtal_lig_file) $('#xtalrow').style.display = '';
  updateScore(); logAnswer(picked, af3, viewerTrace);
}

// after reveal: flip between the green/red answer and the original anonymised "my view" to study it
async function toggleAnswer() {
  if (DEV) return toggleAnswerDev();
  if (!cur.revealed || viewerTransitionBusy) return;
  await viewerRebuild.enqueue(
    () => {
      cur.showAnswer = !cur.showAnswer;
      if (cur.showAnswer) {
        if (userView.displayMode === 'grid') { displayMode = 'grid'; clustered = userView.clustered; }
        else { clustered = false; displayMode = 'all'; }
      }
      else { applyUserView(); shownOne = 0; }                          // restore the user's remembered view
      syncButtons();
    },
    () => {
      if (cur.showAnswer) {
        renderRevealList(cur.selected, cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null);
      } else { renderUI(); }
      $('#myview').textContent = cur.showAnswer ? '← Back to my view (hide answer)' : 'Show answer →';
    },
  );
}

function renderRevealList(picked, af3) {
  const box = $('#choices'); box.innerHTML = '';
  if (picked && picked.none) {
    const el = document.createElement('div');
    el.className = 'choice ' + (picked.correct ? 'correct' : 'wrong');
    el.innerHTML = `<span class="sw" style="background:#5a6675;border-style:dashed"></span><span class="nm">You: “None of these” ${picked.correct ? '✓' : '✗'}</span>`;
    box.appendChild(el);
  }
  cur.clusters.flatMap(c => c.members).sort((a, b) => a.rmsd - b.rmsd).forEach(c => {
    const el = document.createElement('div');
    el.className = 'choice ' + (c.correct ? 'correct' : 'wrong');
    // RnP only: reveal which co-folding method this pose came from (anonymised during play). Rendered as a
    // small muted, monospace-ish metadata tag so it reads as provenance, not a choice label. CAMEO = all AF3.
    const methodTag = (cur.item.source === 'rnp' && c._method)
      ? ` <span class="method" style="color:var(--faint);font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">· ${methodName(c._method)}</span>`
      : '';
    el.innerHTML = `<span class="sw" style="background:${hex(c === picked ? (c.correct ? GOOD : BAD) : c.color)}"></span>`
      + `<span class="nm">Pose ${c.label}${c === picked ? ' ← you' : ''}${c === af3 ? ' ⟨AI⟩' : ''}${methodTag}</span>`
      + `<span class="rmsd" style="color:${c.correct ? 'var(--good)' : 'var(--bad)'}">${c.rmsd.toFixed(2)} Å</span>`;
    box.appendChild(el);
  });
}

function updateScore() {
  const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
  $('#sc-you').textContent = `${score.you} / ${score.n}  (${pct(score.you, score.n)}%)`;
  $('#sc-af3').textContent = `${score.af3} / ${score.n}  (${pct(score.af3, score.n)}%)`;
  const initialOptions = cur
    ? (displayMode === 'grid' ? allGridEntries().length : visibleChoices().length) + (difficulty === 'hard' ? 1 : 0)
    : 3;
  $('#sc-rand').textContent = score.n ? `${pct(score.randExp, score.n)}%`
    : `${Math.round(100 / initialOptions)}%`;
}
function logAnswer(picked, af3, viewerTrace) {
  const rec = { item_id: cur.item.id, source: cur.item.source, ligand: cur.item.ligand,
    difficulty, picked_none: !!picked.none, picked_sample: picked.none ? -1 : picked.af3_sample,
    picked_correct: !!picked.correct, picked_rmsd: picked.none ? null : picked.rmsd,
    af3_pick_sample: af3 ? af3.af3_sample : -1, af3_correct: !!(af3 && af3.correct),
    has_correct: !!cur.item.has_correct, n_clusters: cur.clusters.length, ts: Date.now() / 1000 };
  const log = JSON.parse(localStorage.getItem('poseQuizLog') || '[]');
  log.push(rec); localStorage.setItem('poseQuizLog', JSON.stringify(log));
  researchBackend()?.recordAnswer(remoteSessionId, idx, { ...rec, viewer_trace: viewerTrace });
}

function syncButtons() {
  document.querySelectorAll('#mode button').forEach(b => b.classList.toggle('on', b.dataset.m === displayMode));
  renderGridPages();
  // Crystal↔AF3 protein toggle: only meaningful for CAMEO (RnP items carry no per-pose AF3 protein).
  // Centralised here so every redraw path keeps it correct regardless of how we got into play.
  const inPlay = !!cur;
  if (quizSource === 'rnp') proteinMode = 'crystal';
  $('#protmode').style.display = (inPlay && quizSource !== 'rnp') ? '' : 'none';
  document.querySelectorAll('#protmode button').forEach(b => b.classList.toggle('on', b.dataset.p === proteinMode));
  const uc = $('#uncluster');
  uc.textContent = clustered ? 'Uncluster poses' : 'Re-cluster';
  uc.classList.toggle('on', !clustered);
  uc.style.display = cur && cur.clusters.some(c => c.members.length > 1) ? '' : 'none';
  const hb = $('#hbonds');                       // H-bond overlay toggle (mirrors #uncluster styling/gating)
  hb.classList.toggle('on', showHbonds);
  hb.style.display = inPlay ? '' : 'none';
  $('#modehint').textContent = displayMode === 'grid'
    ? (clustered ? 'One linked viewer per distinct cluster. Uncluster to inspect every raw pose on this page.'
                 : 'One linked viewer per raw pose on this page. Drag or zoom any tile to move them together.')
    : 'Near-identical poses are grouped into clusters (one colour each) — pick the cluster you believe is the correct predicted pose. Nearby pocket residues are shown as sticks. The crystal answer is hidden.';
  $('#modehint').style.display = (displayMode === 'one' || locked()) ? 'none' : '';
}

// dev reveal toggle: flip the green/red correctness + RMSD list on/off, reusing the showAnswer machinery.
// Sets cur.revealed alongside cur.showAnswer so buildLayer()/protUrls() colour by correctness and show the
// crystal reference, but never scores or logs (that lives in reveal(), which dev never calls).
async function toggleAnswerDev() {
  if (viewerTransitionBusy) return;
  await viewerRebuild.enqueue(
    () => {
      cur.showAnswer = !cur.showAnswer;
      cur.revealed = cur.showAnswer;
      if (cur.showAnswer) {
        if (userView.displayMode === 'grid') { displayMode = 'grid'; clustered = userView.clustered; }
        else { clustered = false; displayMode = 'all'; }
      }
      else { applyUserView(); shownOne = 0; showXtal = false; $('#showXtal').checked = false; }   // restore remembered view
      syncButtons();
    },
    () => {
      if (cur.showAnswer) {
        const af3 = cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
        renderRevealList(null, af3);
      } else { renderUI(); }
      renderDevNav();
    },
  );
}
// dev free navigation — wraps at the ends, works on every item with no lock required.
function prevDev() { if (!viewerTransitionBusy) void loadQuestion((idx - 1 + ITEMS.length) % ITEMS.length); }
function nextDev() { if (!viewerTransitionBusy) void loadQuestion((idx + 1) % ITEMS.length); }

function next() {
  if (viewerTransitionBusy) return;
  if (DEV) return nextDev();
  (idx + 1 < ITEMS.length) ? void loadQuestion(idx + 1) : finish();
}
function finish() {
  hideGrid();
  researchBackend()?.completeSession(remoteSessionId);
  const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
  $('#ligand').textContent = 'Quiz complete';
  $('#choices').innerHTML = ''; $('#lock').style.display = 'none'; $('#next').style.display = 'none';
  $('#uncluster').style.display = 'none'; $('#mode').style.display = 'none'; $('#protmode').style.display = 'none';
  $('#hbonds').style.display = 'none';
  $('#xtalrow').style.display = 'none'; $('#myview').style.display = 'none';
  $('#verdict').style.display = '';
  $('#verdict').innerHTML =
    `<b>You: ${pct(score.you, score.n)}%</b> · ${oppLabel()}: ${pct(score.af3, score.n)}% · random: ${pct(score.randExp, score.n)}%`
    + `<br><span style="color:var(--muted)">over ${score.n} ${quizSource === 'rnp' ? 'Runs-n-Poses' : 'CAMEO'} single-pocket ensembles (${difficulty})</span>`
    + `<div style="margin-top:12px;display:flex;gap:6px"><input id="uname" aria-label="Leaderboard username"`
    + ` placeholder="username for leaderboard" minlength="3" maxlength="24" pattern="[A-Za-z0-9_\\-]+"`
    + ` style="flex:1;background:#0d1117;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:8px;font-size:13px"/>`
    + `<button class="primary" id="submit" style="padding:8px 12px">Save</button></div>`
    + `<div id="lbmsg" role="status" aria-live="polite" style="margin-top:10px"></div>`;
  $('#submit').onclick = submitSession;
}

async function submitSession() {
  const input = $('#uname');
  const button = $('#submit');
  const message = $('#lbmsg');
  const username = input.value.trim();
  input.value = username;
  if (!input.checkValidity() || username.length < 3) {
    message.textContent = 'Use 3-24 letters, numbers, underscores, or hyphens.';
    input.focus();
    return;
  }

  button.disabled = true;
  message.textContent = 'Saving completed session…';
  let stage = 'persistence';
  try {
    const backend = researchBackend();
    if (!backend) throw new Error('Quiz persistence is unavailable.');
    researchBackend()?.completeSession(remoteSessionId);
    await backend.flush({ strict: true });
    stage = 'username';
    const claimedUsername = await backend.claimUsername(username);
    stage = 'leaderboard';
    const rows = await backend.getLeaderboard();
    showLeaderboard(claimedUsername, rows);
  } catch (error) {
    button.disabled = false;
    if (stage === 'username'
      && (error.code === '23505' || /already taken|unique/i.test(error.message || ''))) {
      message.textContent = 'That username is already taken. Choose another.';
      input.focus();
      input.select();
      return;
    }
    if (stage === 'username' && error.code === '22023') {
      message.textContent = error.message;
      input.focus();
      return;
    }
    if (stage === 'persistence') {
      console.warn('Quiz completion persistence failed:', error.message);
      message.textContent = 'Quiz results could not be saved, so rankings were not loaded. Check browser storage and your connection, then try again.';
    } else if (stage === 'username') {
      console.warn('Leaderboard username claim failed:', error.message);
      message.textContent = 'Your quiz was saved, but the username could not be claimed. Try again.';
    } else {
      console.warn('Shared leaderboard read failed:', error.message);
      message.textContent = 'Your quiz and username were saved, but the leaderboard could not be loaded. Try again.';
    }
  }
}

function showLeaderboard(me, rows) {
  const normalizedMe = String(me).toLowerCase();
  const head = `<div style="font-size:12px;color:var(--faint);letter-spacing:.1em;text-transform:uppercase;margin:6px 0">shared leaderboard</div>`;
  const body = rows.map((row, index) => {
    const mine = String(row.username).toLowerCase() === normalizedMe;
    const difference = Number(row.beat_af3_by);
    return `<div style="display:flex;justify-content:space-between;padding:5px 8px;border-radius:6px;`
      + `${mine ? 'background:#15212b;border:1px solid var(--accent)' : ''};font-size:13px">`
      + `<span>${index + 1}. <b>${row.username}</b> <span style="color:var(--muted)">· ${row.items} items</span></span>`
      + `<span><b>${row.accuracy}%</b> <span style="color:var(--muted)">(AI ${row.af3_accuracy}%, `
      + `<span style="color:${difference >= 0 ? 'var(--good)' : 'var(--bad)'}">${difference >= 0 ? '+' : ''}${difference}</span>)</span></span></div>`;
  }).join('');
  $('#lbmsg').innerHTML = head + (body || '<span style="color:var(--muted)">No completed sessions yet.</span>');
}

async function init() {
  viewer = await molstar.Viewer.create('app', OPTS);
  plugin = viewer.plugin;
  configurePlugin(plugin);
  viewerRebuild = window.createViewerRebuildCoordinator({
    rebuild: buildLayer,
    setBusy: setViewerControlsBusy,
  });
  revealAfterIdle = window.createRevealAfterIdle({
    coordinator: viewerRebuild,
    reveal: finalizeReveal,
  });
  if (!DEV && typeof window.createViewerTraceRecorder === 'function') {
    try {
      viewerTraceRecorder = window.createViewerTraceRecorder({ plugin });
    } catch (error) {
      console.warn('Viewer recording disabled:', error.message);
    }
  }
  if (DEV) {                                            // browse/inspection mode banner + page title
    document.title = 'Pose Quiz · DEV browse';
    const bd = $('#badge'); if (bd) bd.textContent = 'DEV browse · free Prev/Next · reveal answer + RMSDs on demand';
  }
  try {
    plugin.canvas3d?.setProps({
      renderer: { backgroundColor: 0xffffff },
      camera: { helper: { axes: { name: 'off', params: {} } } },
    });
  } catch (e) {}
  const fetchItems = async (f) => { try { const d = await fetch(f + '?v=' + Date.now()).then(r => r.ok ? r.json() : null); return d ? (d.items || d) : []; } catch (e) { return []; } };
  const norm = (it, source) => {
    const ch = it.choices.map(c => ({ ...c, correct: c.rmsd < CORRECT_THRESH }));   // strict: correct only if rmsd<1.5
    const hasC = ch.some(c => c.correct), hasW = ch.some(c => c.rmsd > WRONG_THRESH);
    // buckets: game-able (a correct + a wrong) | all-wrong (every pose >3) | all-correct (every pose <1.5,
    // the positive control for Hard: model confidence can't tell it from all-wrong) | limbo (1.5-3 mix, dropped)
    const bucket = (hasC && hasW) ? 'game-able'
      : (ch.every(c => c.rmsd > WRONG_THRESH) ? 'all-wrong'
      : (ch.every(c => c.rmsd < CORRECT_THRESH) ? 'all-correct' : 'limbo'));
    return { ...it, source, choices: ch, has_correct: hasC, bucket,
      easyPlayable: easyPlayable(ch, source) };
  };
  // CAMEO: game-able + all-wrong + all-correct(positive control).  RnP: single file already carries all three buckets.
  const [cg, ca, cx, rn] = await Promise.all([fetchItems('quiz_items.json'), fetchItems('quiz_items_allwrong.json'),
    fetchItems('quiz_items_allcorrect.json'), fetchItems('quiz_items_rnp.json')]);
  const keep = it => it.bucket !== 'limbo' && (it.n_heavy ?? 0) >= HEAVY_MIN;   // drop 1.5-3A limbo + tiny ligands
  const capAllCorrect = (pool) => {                     // keep all-correct a positive-control sprinkle
    const ac = pool.filter(it => it.bucket === 'all-correct');
    const rest = pool.filter(it => it.bucket !== 'all-correct');
    const cap = Math.round(ALLCORRECT_MAX_FRAC * rest.length);
    return ac.length <= cap ? pool : [...rest, ...shuffle(ac.slice()).slice(0, cap)];
  };
  POOLS.cameo = capAllCorrect([...cg, ...ca, ...cx].map(it => norm(it, 'cameo')).filter(keep));
  POOLS.rnp = capAllCorrect(rn.map(it => norm(it, 'rnp')).filter(keep));
  document.querySelectorAll('#quizsrc button').forEach(b => b.onclick = () => {
    quizSource = b.dataset.q; document.querySelectorAll('#quizsrc button').forEach(x => x.classList.toggle('on', x === b)); showIntro();
  });
  document.querySelectorAll('#diff button').forEach(b => b.onclick = () => {
    difficulty = b.dataset.d; document.querySelectorAll('#diff button').forEach(x => x.classList.toggle('on', x === b)); showIntro();
  });
  document.querySelectorAll('#mode button').forEach(b => b.onclick = async () => {
    if (interactionBlocked()) return;
    const mode = b.dataset.m;
    const wasGrid = displayMode === 'grid';
    await viewerRebuild.enqueue(() => {
      displayMode = mode; if (displayMode === 'one') shownOne = 0;
      if (!cur.revealed && wasGrid !== (displayMode === 'grid')) {
        cur.selected = null; cur.selectionExact = false; cur.answerChoices = [];
      }
      if (!cur.revealed) rememberView();       // record the user's choice (persist across questions)
      syncButtons();
    }, renderUI);
  });
  document.querySelectorAll('#protmode button').forEach(b => b.onclick = async () => {
    if (interactionBlocked()) return;
    const mode = b.dataset.p;
    await viewerRebuild.enqueue(() => {
      proteinMode = mode;
      if (!cur.revealed) rememberView();
      syncButtons();
    });
  });
  $('#uncluster').onclick = async () => {
    if (interactionBlocked()) return;
    await viewerRebuild.enqueue(() => {
      clustered = !clustered; if (!cur.revealed) cur.selected = null; shownOne = 0;
      if (!cur.revealed) rememberView();
      syncButtons();
      renderUI();
    });
  };
  $('#hbonds').onclick = async () => {
    if (interactionBlocked()) return;
    await viewerRebuild.enqueue(() => {
      showHbonds = !showHbonds;
      if (!cur.revealed) rememberView();       // persist across questions like the other view choices
      syncButtons();
    });
  };
  $('#lock').onclick = reveal;
  $('#next').onclick = next;
  $('#prev').onclick = prevDev;
  $('#start').onclick = startQuiz;
  $('#myview').onclick = toggleAnswer;
  $('#showXtal').onchange = async (e) => {
    if (viewerTransitionBusy) return;
    const checked = e.target.checked;
    await viewerRebuild.enqueue(() => { showXtal = checked; });
  };
  document.addEventListener('keydown', async e => {
    if (DEV && cur) {                                   // dev: Up/Down = prev/next item, any mode, no lock needed
      if (e.key === 'ArrowUp') { e.preventDefault(); prevDev(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); nextDev(); return; }
    }
    if (!cur || interactionBlocked() || displayMode !== 'one') return;
    if (e.key === 'ArrowRight') {
      await viewerRebuild.enqueue(() => {
        shownOne = (shownOne + 1) % visibleChoices().length;
      });
    }
    if (e.key === 'ArrowLeft') {
      await viewerRebuild.enqueue(() => {
        const n = visibleChoices().length;
        shownOne = (shownOne - 1 + n) % n;
      });
    }
  });
  if (!POOLS.cameo.length && !POOLS.rnp.length) { $('#ligand').textContent = 'no quiz items'; return; }
  showIntro();
}
init().catch(e => { $('#ligand').textContent = 'error: ' + e.message; console.error(e); });
