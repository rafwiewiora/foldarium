// Pose Quiz — binding pocket (ligand removed) + shuffled, CLUSTERED ligand poses. Player picks the cluster they think
// is correct (or, in Hard mode, "none of these are correct"). Two quizzes: CAMEO (AlphaFold3 only) and
// Runs-n-Poses (poses pooled from multiple co-folding methods, ANONYMISED — the method is never rendered).
// Reuses the viewer's Mol* setup + its proven delete-and-rebuild pattern. Pose carbons are coloured by a
// NON-semantic per-cluster palette (random each question) only for identification — never correctness.

const PALETTE = [0x5B8FF9, 0xF6BD16, 0x9270CA, 0x5AD8A6, 0xE8964A, 0x6DC8EC];
const LABELS = ['A', 'B', 'C', 'D', 'E', 'F'];
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
// HARD sessions are drawn to THIS bucket mix, NOT the raw data proportions. The raw data is ~78% all-wrong,
// which lets "always say none" score ~78% (base-rate gaming). Balancing toward ~40/45/15 makes constant
// strategies score near chance, so the score reflects real discrimination + keeps game-able (rare pick
// puzzles) well-populated. Applied automatically per session (see drawSession); tune here. Easy is unaffected
// (game-able only). If a bucket is short (e.g. novel-only demo), the shortfall is back-filled from the rest.
const HARD_MIX = { 'game-able': 0.40, 'all-wrong': 0.45, 'all-correct': 0.15 };

const OPTS = {
  layoutIsExpanded: false, layoutShowControls: false, layoutShowRemoteState: false,
  layoutShowSequence: false, layoutShowLog: false, layoutShowLeftPanel: false,
  viewportShowExpand: false, viewportShowControls: false, viewportShowSettings: false,
  viewportShowSelectionMode: false, viewportShowAnimation: false, viewportShowTrajectoryControls: false,
};

const DEV = new URLSearchParams(location.search).has('dev');   // no-vote inspection/browse mode (?dev=1)
let viewer, plugin, ITEMS = [], idx = 0, cur = null;
let POOLS = { cameo: [], rnp: [] }, quizSource = 'cameo', difficulty = 'easy';
let displayMode = 'all', clustered = true, shownOne = 0, showXtal = false, proteinMode = 'crystal';
let showHbonds = false;   // H-bond overlay toggle — persisted across questions like the other view choices
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
let sessionAnswers = [];
const $ = s => document.querySelector(s);
const CACHE_BUST = Date.now();
const hex = c => '#' + c.toString(16).padStart(6, '0');
// "locked" = the green/red answer is on screen; controls are inert only then. In "my view" (revealed but
// answer hidden) everything is interactive again, exactly as before voting.
const locked = () => cur && cur.revealed && cur.showAnswer;
const oppLabel = () => (quizSource === 'rnp' ? 'Best automated pick (ligand pLDDT)' : 'AlphaFold3 (pLDDT-ranked)');

async function loadStruct(url, format) {
  const data = await plugin.builders.data.download({ url: url + '?v=' + CACHE_BUST, isBinary: false });
  const traj = await plugin.builders.structure.parseTrajectory(data, format);
  const model = await plugin.builders.structure.createModel(traj);
  const struct = await plugin.builders.structure.createStructure(model);
  return { data, struct };
}
async function fetchPdbText(url) {   // raw PDB text (for merging pocket+pose into ONE structure for interactions)
  const r = await fetch(url + '?v=' + CACHE_BUST);
  return r.ok ? await r.text() : '';
}
// keep only ATOM/HETATM/TER records so concatenated files parse as a single model (drop END/CONECT/etc.)
const atomRecords = t => t.split('\n').filter(l => /^(ATOM|HETATM|TER)/.test(l)).join('\n');
async function addRep(struct, selector, type, color, alpha = 1) {
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, selector);
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type, typeParams: { alpha }, color: 'uniform', colorParams: { value: color },
  });
}
async function addPose(struct, carbon) {
  let comp = await plugin.builders.structure.tryCreateComponentStatic(struct, 'ligand');
  if (!comp) comp = await plugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor: 0.24 },
    color: 'element-symbol', colorParams: { carbonColor: { name: 'uniform', params: { value: carbon } } },
  });
}
async function addSticks(struct, sizeFactor, alpha) {        // pocket residues — default element colours
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor, alpha }, color: 'element-symbol',
  });
}
function shuffle(a) { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }

// ---- visible-choice logic --------------------------------------------------------------------
function visibleChoices() {
  return clustered ? cur.clusters.map(c => c.rep) : cur.clusters.flatMap(c => c.members);
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
async function buildLayer() {           // only the moving ligand poses (+ crystal truth on reveal)
  saveCam();
  await buildProtein();                 // swap protein only if it changed (AF3 one-at-a-time, or toggle)
  await clearLayer();
  const answer = cur.revealed && cur.showAnswer;        // green/red reveal vs the anonymised "my view"
  const vis = visibleChoices();
  const shown = answer || displayMode === 'all' ? vis : [vis[Math.min(shownOne, vis.length - 1)]];
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
}

async function loadQuestion(i) {
  idx = i;
  const item = ITEMS[i];
  // build cluster objects in shuffled order, colour per cluster
  const byCluster = {};
  item.choices.forEach(c => (byCluster[c.cluster] ??= []).push({ ...c }));
  const clusters = shuffle(Object.values(byCluster)).map((members, k) => {
    const color = PALETTE[k % PALETTE.length], label = LABELS[k % LABELS.length];
    members.forEach((m, j) => { m.color = color; m.label = label + (members.length > 1 ? '·' + (j + 1) : ''); });
    return { label, color, members, rep: members.find(m => m.is_rep) || members[0] };
  });
  cur = { item, clusters, selected: null, revealed: false, showAnswer: false };
  // PERSIST across questions: seed displayMode / clustered / proteinMode from the user's last choice
  // (userView), NOT from the live globals which reveal() may have overridden for its correctness list.
  // RESET per question: shownOne (pose index differs) + the fresh-vote/reveal state (cur.selected /
  // cur.revealed / cur.showAnswer, set above) so the answer starts hidden.
  applyUserView();
  shownOne = 0;
  $('#myview').style.display = 'none'; $('#start').style.display = 'none';
  $('#xtalrow').style.display = 'none'; $('#showXtal').checked = false;
  try { await plugin.clear(); } catch (e) {}
  proteinData = []; layerData = []; hbondData = []; savedCam = null; currentProtUrl = null;
  showXtal = false;
  syncButtons();
  await buildLayer();          // builds the protein (via buildProtein) + the poses
  try { plugin.canvas3d?.requestCameraReset(); } catch (e) {}  // frame only on a NEW question
  renderUI();
}

function renderUI() {
  $('#progress').textContent = DEV ? `item ${idx + 1} / ${ITEMS.length} · dev`
                                   : `question ${idx + 1} / ${ITEMS.length}`;
  $('#ligand').innerHTML = `${cur.item.ligand} <small>· ${cur.clusters.length} distinct pose clusters</small>`;
  const box = $('#choices'); box.innerHTML = '';
  visibleChoices().forEach((c, k) => {
    const b = document.createElement('button');
    b.className = 'choice'; b.dataset.k = k;
    let nm;
    if (clustered) {
      const cl = cur.clusters[k];
      nm = `Pose ${cl.label}` + (cl.members.length > 1
        ? ` <span style="color:var(--faint)">(${cl.members.length} poses)</span>` : '');
    } else nm = `Pose ${c.label}`;
    b.innerHTML = `<span class="sw" style="background:${hex(c.color)}"></span><span class="nm">${nm}</span><span class="tag" data-tag></span>`;
    b.onclick = () => onPick(k);
    box.appendChild(b);
  });
  if (difficulty === 'hard') {                          // the detect-game option
    const nb = document.createElement('button');
    nb.className = 'choice none'; nb.dataset.k = 'none';
    nb.innerHTML = `<span class="sw" style="background:#5a6675;border-style:dashed"></span><span class="nm">None of these are correct</span>`;
    nb.onclick = () => onPick('none');
    box.appendChild(nb);
  }
  if (cur.selected) {                                   // keep the player's pick highlighted
    if (cur.selected.none) box.querySelector('.choice.none')?.classList.add('sel');
    else { const vis = visibleChoices(); const k = vis.findIndex(c => c.cluster === cur.selected.cluster); if (k >= 0) document.querySelectorAll('.choice')[k]?.classList.add('sel'); }
  }
  if (DEV) { renderDevNav(); return; }                  // dev: free browse, no vote/lock/score
  $('#lock').disabled = cur.selected == null; $('#lock').style.display = cur.revealed ? 'none' : '';
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
  return POOLS[quizSource].filter(it => difficulty === 'hard' ? true : it.bucket === 'game-able');
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

const SESSION_SIZE = 30;   // a completable sitting; re-play draws a fresh random subset, leaderboard accumulates
// Draw a session. Easy = plain random from the (game-able) pool. Hard = stratified toward HARD_MIX so the
// score isn't gameable by base rate; shortfall in any bucket is back-filled from the rest. DEV = whole pool.
function drawSession() {
  const pool = filteredPool();
  if (DEV) return shuffle(pool.slice());
  if (difficulty !== 'hard') return shuffle(pool.slice()).slice(0, SESSION_SIZE);
  const by = { 'game-able': [], 'all-wrong': [], 'all-correct': [] };
  for (const it of shuffle(pool.slice())) if (by[it.bucket]) by[it.bucket].push(it);
  const picked = [], used = new Set();
  for (const b in HARD_MIX)
    for (const it of by[b].slice(0, Math.round(SESSION_SIZE * HARD_MIX[b]))) { picked.push(it); used.add(it); }
  if (picked.length < SESSION_SIZE)                          // back-fill from leftovers if a bucket was short
    for (const it of shuffle(pool.slice())) { if (picked.length >= SESSION_SIZE) break; if (!used.has(it)) { picked.push(it); used.add(it); } }
  return shuffle(picked).slice(0, SESSION_SIZE);
}
function startQuiz() {
  ITEMS = drawSession();
  if (quizSource === 'rnp') proteinMode = 'crystal';
  rememberView();   // snapshot the starting view as the persisted baseline for this session
  $('#setup').style.display = 'none'; $('#start').style.display = 'none'; $('#mode').style.display = '';
  $('#protmode').style.display = quizSource === 'rnp' ? 'none' : '';
  $('#lbl-af3').textContent = oppLabel();
  sessionAnswers = [];
  loadQuestion(0);
}

function onPick(k) {
  if (locked()) return;
  if (cur.revealed) { if (k !== 'none' && displayMode === 'one') { shownOne = k; buildLayer(); } return; }  // my-view: navigate, don't re-vote
  if (k === 'none') {
    cur.selected = { none: true, correct: !cur.item.has_correct, label: 'None of these' };
    document.querySelectorAll('.choice').forEach(el => el.classList.toggle('sel', el.dataset.k === 'none'));
    $('#lock').disabled = false;
    return;
  }
  cur.selected = visibleChoices()[k];
  document.querySelectorAll('.choice').forEach(el => el.classList.toggle('sel', el.dataset.k == k));
  $('#lock').disabled = false;
  if (displayMode === 'one') { shownOne = k; buildLayer(); }
}

async function reveal() {
  if (cur.selected == null || cur.revealed) return;
  cur.revealed = true; cur.showAnswer = true; displayMode = 'all'; clustered = false; syncButtons();
  await buildLayer();
  const picked = cur.selected;
  const af3 = cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
  const youRight = !!picked.correct, af3Right = !!(af3 && af3.correct);
  score.n++; score.you += youRight; score.af3 += af3Right;
  const nClu = cur.clusters.length, nCorrectClu = cur.clusters.filter(c => c.rep.correct).length;
  const opts = nClu + (difficulty === 'hard' ? 1 : 0);                 // include the "none" option
  score.randExp += (cur.item.has_correct ? nCorrectClu : (difficulty === 'hard' ? 1 : 0)) / opts;
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
  updateScore(); logAnswer(picked, af3);
}

// after reveal: flip between the green/red answer and the original anonymised "my view" to study it
async function toggleAnswer() {
  if (DEV) return toggleAnswerDev();
  if (!cur.revealed) return;
  cur.showAnswer = !cur.showAnswer;
  if (cur.showAnswer) { clustered = false; displayMode = 'all'; }  // correctness list: always all/unclustered
  else { applyUserView(); shownOne = 0; }                          // restore the user's remembered view
  syncButtons();
  await buildLayer();
  if (cur.showAnswer) {
    renderRevealList(cur.selected, cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null);
  } else { renderUI(); }
  $('#myview').textContent = cur.showAnswer ? '← Back to my view (hide answer)' : 'Show answer →';
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
  $('#sc-rand').textContent = score.n ? `${pct(score.randExp, score.n)}%`
    : `${Math.round(100 / (cur?.clusters.length || 3))}%`;
}
function logAnswer(picked, af3) {
  const rec = { item_id: cur.item.id, source: cur.item.source, ligand: cur.item.ligand,
    difficulty, picked_none: !!picked.none, picked_sample: picked.none ? -1 : picked.af3_sample,
    picked_correct: !!picked.correct, picked_rmsd: picked.none ? null : picked.rmsd,
    af3_pick_sample: af3 ? af3.af3_sample : -1, af3_correct: !!(af3 && af3.correct),
    has_correct: !!cur.item.has_correct, n_clusters: cur.clusters.length, ts: Date.now() / 1000 };
  sessionAnswers.push(rec);
  const log = JSON.parse(localStorage.getItem('poseQuizLog') || '[]');
  log.push(rec); localStorage.setItem('poseQuizLog', JSON.stringify(log));
}

function syncButtons() {
  document.querySelectorAll('#mode button').forEach(b => b.classList.toggle('on', b.dataset.m === displayMode));
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
  $('#modehint').style.display = (displayMode === 'one' || locked()) ? 'none' : '';
}

// dev reveal toggle: flip the green/red correctness + RMSD list on/off, reusing the showAnswer machinery.
// Sets cur.revealed alongside cur.showAnswer so buildLayer()/protUrls() colour by correctness and show the
// crystal reference, but never scores or logs (that lives in reveal(), which dev never calls).
async function toggleAnswerDev() {
  cur.showAnswer = !cur.showAnswer;
  cur.revealed = cur.showAnswer;
  if (cur.showAnswer) { clustered = false; displayMode = 'all'; }  // correctness list: always all/unclustered
  else { applyUserView(); shownOne = 0; showXtal = false; $('#showXtal').checked = false; }   // restore remembered view
  syncButtons();
  await buildLayer();
  if (cur.showAnswer) {
    const af3 = cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
    renderRevealList(null, af3);
  } else { renderUI(); }
  renderDevNav();
}
// dev free navigation — wraps at the ends, works on every item with no lock required.
function prevDev() { loadQuestion((idx - 1 + ITEMS.length) % ITEMS.length); }
function nextDev() { loadQuestion((idx + 1) % ITEMS.length); }

function next() { if (DEV) return nextDev(); (idx + 1 < ITEMS.length) ? loadQuestion(idx + 1) : finish(); }
function finish() {
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
    + `<div style="margin-top:12px;display:flex;gap:6px"><input id="uname" placeholder="username for leaderboard"`
    + ` style="flex:1;background:#0d1117;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:8px;font-size:13px"/>`
    + `<button class="primary" id="submit" style="padding:8px 12px">Save</button></div><div id="lbmsg" style="margin-top:10px"></div>`;
  const saved = localStorage.getItem('poseQuizUser'); if (saved) $('#uname').value = saved;
  $('#submit').onclick = submitSession;
}

// Aggregate this browser's localStorage sessions into a leaderboard (used when there's no backend, e.g.
// static GitHub Pages hosting). Latest answer per (user,item) so re-plays + a growing pool accumulate.
function localLeaderboard() {
  const sessions = JSON.parse(localStorage.getItem('poseQuizSessions') || '[]');
  const latest = {};   // user -> item -> {pc, ac, ts}
  for (const s of sessions) for (const a of (s.answers || [])) {
    const u = s.username || 'anon'; (latest[u] ??= {});
    const key = (a.source || 'cameo') + ':' + a.item_id;
    const cur = latest[u][key];
    if (!cur || (a.ts || 0) > cur.ts) latest[u][key] = { pc: a.picked_correct ? 1 : 0, ac: a.af3_correct ? 1 : 0, ts: a.ts || 0 };
  }
  const counts = {};
  for (const u in latest) counts[u] = Object.values(latest[u]).reduce((o, x) => ({ n: o.n + 1, c: o.c + x.pc, a: o.a + x.ac }), { n: 0, c: 0, a: 0 });
  return Object.entries(counts).map(([username, x]) => ({ username, items: x.n, correct: x.c,
    accuracy: x.n ? Math.round(100 * x.c / x.n) : 0, af3_accuracy: x.n ? Math.round(100 * x.a / x.n) : 0,
    beat_af3_by: x.n ? Math.round(100 * (x.c - x.a) / x.n) : 0,
    sessions: sessions.filter(s => (s.username || 'anon') === username).length }))
    .sort((p, q) => q.accuracy - p.accuracy || q.items - p.items);
}

async function submitSession() {
  const u = ($('#uname').value || 'anon').trim() || 'anon';
  localStorage.setItem('poseQuizUser', u);
  const sessions = JSON.parse(localStorage.getItem('poseQuizSessions') || '[]');
  sessions.push({ username: u, source: quizSource, difficulty, answers: sessionAnswers, ts: Date.now() / 1000 });
  localStorage.setItem('poseQuizSessions', JSON.stringify(sessions));
  $('#submit').disabled = true; $('#lbmsg').textContent = 'saving…';
  let backend = false;
  try {
    const r = await fetch('api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, source: quizSource, difficulty, answers: sessionAnswers, client_ts: Date.now() / 1000 }) });
    backend = r.ok;
  } catch (e) { backend = false; }
  await showLeaderboard(u, backend);
}

async function showLeaderboard(me, backend = true) {
  let rows = null;
  if (backend) { try { rows = await fetch('api/leaderboard?v=' + Date.now()).then(r => r.ok ? r.json() : null); } catch (e) {} }
  const local = !rows;
  if (local) rows = localLeaderboard();
  const head = `<div style="font-size:12px;color:var(--faint);letter-spacing:.1em;text-transform:uppercase;margin:6px 0">`
    + `leaderboard${local ? ' <span style="text-transform:none;color:var(--faint)">· this browser only (no shared server)</span>' : ''}</div>`;
  const body = rows.map((r, i) => {
    const mine = r.username === me;
    return `<div style="display:flex;justify-content:space-between;padding:5px 8px;border-radius:6px;`
      + `${mine ? 'background:#15212b;border:1px solid var(--accent)' : ''};font-size:13px">`
      + `<span>${i + 1}. <b>${r.username}</b> <span style="color:var(--muted)">· ${r.items} items</span></span>`
      + `<span><b>${r.accuracy}%</b> <span style="color:var(--muted)">(AI ${r.af3_accuracy}%, `
      + `<span style="color:${r.beat_af3_by >= 0 ? 'var(--good)' : 'var(--bad)'}">${r.beat_af3_by >= 0 ? '+' : ''}${r.beat_af3_by}</span>)</span></span></div>`;
  }).join('');
  $('#lbmsg').innerHTML = head + body;
}

async function init() {
  viewer = await molstar.Viewer.create('app', OPTS);
  plugin = viewer.plugin;
  if (DEV) {                                            // browse/inspection mode banner + page title
    document.title = 'Pose Quiz · DEV browse';
    const bd = $('#badge'); if (bd) bd.textContent = 'DEV browse · free Prev/Next · reveal answer + RMSDs on demand';
  }
  try { plugin.canvas3d?.setProps({ renderer: { backgroundColor: 0xffffff } }); } catch (e) {}
  const fetchItems = async (f) => { try { const d = await fetch(f + '?v=' + Date.now()).then(r => r.ok ? r.json() : null); return d ? (d.items || d) : []; } catch (e) { return []; } };
  const norm = (it, source) => {
    const ch = it.choices.map(c => ({ ...c, correct: c.rmsd < CORRECT_THRESH }));   // strict: correct only if rmsd<1.5
    const hasC = ch.some(c => c.correct), hasW = ch.some(c => c.rmsd > WRONG_THRESH);
    // buckets: game-able (a correct + a wrong) | all-wrong (every pose >3) | all-correct (every pose <1.5,
    // the positive control for Hard: model confidence can't tell it from all-wrong) | limbo (1.5-3 mix, dropped)
    const bucket = (hasC && hasW) ? 'game-able'
      : (ch.every(c => c.rmsd > WRONG_THRESH) ? 'all-wrong'
      : (ch.every(c => c.rmsd < CORRECT_THRESH) ? 'all-correct' : 'limbo'));
    return { ...it, source, choices: ch, has_correct: hasC, bucket };
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
    if (locked()) return;
    displayMode = b.dataset.m; if (displayMode === 'one') shownOne = 0;
    if (!cur.revealed) rememberView();       // record the user's choice (persist across questions)
    syncButtons(); await buildLayer();
  });
  document.querySelectorAll('#protmode button').forEach(b => b.onclick = async () => {
    proteinMode = b.dataset.p;
    if (!cur.revealed) rememberView();
    syncButtons(); await buildLayer();
  });
  $('#uncluster').onclick = async () => {
    if (locked()) return;
    clustered = !clustered; if (!cur.revealed) cur.selected = null; shownOne = 0;
    if (!cur.revealed) rememberView();
    syncButtons(); await buildLayer(); renderUI();
  };
  $('#hbonds').onclick = async () => {
    showHbonds = !showHbonds;
    if (!cur.revealed) rememberView();       // persist across questions like the other view choices
    syncButtons(); await buildLayer();
  };
  $('#lock').onclick = reveal;
  $('#next').onclick = next;
  $('#prev').onclick = prevDev;
  $('#start').onclick = startQuiz;
  $('#myview').onclick = toggleAnswer;
  $('#showXtal').onchange = async (e) => { showXtal = e.target.checked; await buildLayer(); };
  document.addEventListener('keydown', async e => {
    if (DEV && cur) {                                   // dev: Up/Down = prev/next item, any mode, no lock needed
      if (e.key === 'ArrowUp') { e.preventDefault(); prevDev(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); nextDev(); return; }
    }
    if (!cur || locked() || displayMode !== 'one') return;
    const n = visibleChoices().length;
    if (e.key === 'ArrowRight') { shownOne = (shownOne + 1) % n; await buildLayer(); }
    if (e.key === 'ArrowLeft') { shownOne = (shownOne - 1 + n) % n; await buildLayer(); }
  });
  if (!POOLS.cameo.length && !POOLS.rnp.length) { $('#ligand').textContent = 'no quiz items'; return; }
  showIntro();
}
init().catch(e => { $('#ligand').textContent = 'error: ' + e.message; console.error(e); });
