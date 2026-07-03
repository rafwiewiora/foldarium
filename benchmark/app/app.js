// Co-folding pose viewer — RCSB-style Mol* rendering.
//
// Rendering model (why it looks like the PDB website):
//   * REFERENCE crystal = the PRISTINE RCSB mmCIF file. The protein polymer is
//     drawn as smooth CARTOON and the crystal ligand as ball-and-stick, using
//     Mol*'s own component selectors on an untouched RCSB file — the exact data
//     the PDB site renders. (Switching the rep dropdown re-skins the polymer to
//     surface / ball-and-stick.)
//   * AF3 POSES = ligand-only PDB files, already superposed into the crystal
//     coordinate frame, drawn as ball-and-stick and colored by correctness.
//     A ligand-only structure renders reliably regardless of how it was written.
//
// The crystal mmCIF is never modified, so cartoon recognition never breaks.

let viewer, plugin, DATA;
const $ = s => document.querySelector(s);

// Colors (RGB ints for Mol* uniform color).
const COL = {
  truth:   0x2BA84A,  // crystal ligand — green
  correct: 0x2E9BD6,  // predicted pose, RMSD < 2A — teal/blue
  wrong:   0xE23B2E,  // predicted pose, RMSD >= 2A — red
  modelProtein: 0xE8A33D,  // AF3 prediction protein cartoon — orange (off by default)
  train: 0x9B5DE5,         // closest pre-cutoff TRAINING ligand — purple
  cloud: 0xF2B705,         // pharmacophore CLOUD — all pocket ligands from Foldseek homologs — amber
};

const STATE = { sys: 0, pose: 'all', rep: 'cartoon', opacity: 0.55, showModelProtein: false, filter: 'all', showTrain: true, showTrainProtein: false,
                showTrainCloud: false, sortBy: 'default', minCryst: 0, minPred: 0, minGap: -1 };

// Handles for live updates: crystal protein + ligand reps (opacity slider affects both).
let refPolymerRepr = null;
let refLigandRepr = null;
// Camera persistence: keep the user's view across pose/representation changes;
// only auto-frame when the SYSTEM changes (matches the psistructure apps).
let savedCamera = null;
let renderedSys = -1;

const OPTS = {
  layoutIsExpanded: false, layoutShowControls: false, layoutShowRemoteState: false,
  layoutShowSequence: false, layoutShowLog: false, layoutShowLeftPanel: false,
  viewportShowExpand: false, viewportShowControls: false, viewportShowSettings: false,
  viewportShowSelectionMode: false, viewportShowAnimation: false,
  viewportShowTrajectoryControls: false,
};

async function init() {
  viewer = await molstar.Viewer.create('app', OPTS);
  plugin = viewer.plugin;
  // White background — the RCSB 3D-view look.
  try { plugin.canvas3d?.setProps({ renderer: { backgroundColor: 0xffffff } }); } catch (e) {}
  DATA = await fetch('/systems.json?v=' + Date.now()).then(r => r.json());
  buildControls();
  // Share the single viewer/plugin with the within-target overlay mode (group.js).
  window.COFOLD = { viewer, plugin, OPTS, COL };
  initModeSwitch();
  await render();
}

// ---- mode switch (single system  <->  within-target overlay) ----------------
// Both modes drive the SAME Mol* plugin; switching just clears the scene and
// rebuilds with the other mode's data. The single-system view is untouched.
let CURRENT_MODE = 'single';
function initModeSwitch() {
  document.querySelectorAll('.tab').forEach(t => {
    t.onclick = () => switchMode(t.dataset.mode);
  });
}
async function switchMode(mode) {
  if (mode === CURRENT_MODE) return;
  CURRENT_MODE = mode;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.mode === mode));
  document.getElementById('mode-single').classList.toggle('on', mode === 'single');
  document.getElementById('mode-group').classList.toggle('on', mode === 'group');
  const badge = document.getElementById('badge');
  // Reset the per-mode "have we framed once" memory so each mode auto-frames on entry.
  renderedSys = -1; savedCamera = null;
  try { await plugin.clear(); } catch (e) {}
  refRoots = []; poseRoots = []; refPolymerRepr = refLigandRepr = null;
  if (mode === 'single') {
    if (badge) badge.textContent = 'crystal: single pocket copy, target ligand only · cartoon + ball-and-stick';
    await render();
  } else {
    if (badge) badge.textContent = 'within-target overlay · shared reference protein';
    await window.GROUP.enter();
  }
}

// Bind an event handler only if the element exists — a missing control must
// never abort init (e.g. a stale cached index.html during dev).
function on(sel, event, fn) { const el = $(sel); if (el) el[event] = fn; }

function buildControls() {
  fillSystems();
  on('#filter', 'onchange', e => {
    STATE.filter = e.target.value;          // keep pose/rep/overlay choices across filtering
    fillSystems(); fillPoses(); render();
  });
  on('#sys', 'onchange', e => { STATE.sys = +e.target.value; fillPoses(); render(); });  // conserve view options
  on('#rep', 'onchange', e => { STATE.rep = e.target.value; render(); });
  on('#pose', 'onchange', e => { STATE.pose = e.target.value; renderPosesOnly(); });
  on('#reset', 'onclick', () => focusPocket(DATA.systems[STATE.sys].xtal_center));
  on('#opacity', 'oninput', e => { STATE.opacity = +e.target.value / 100; applyOpacity(); });
  on('#modelProtein', 'onchange', e => { STATE.showModelProtein = e.target.checked; renderPosesOnly(); });
  on('#showTrain', 'onchange', e => { STATE.showTrain = e.target.checked; render(); });
  on('#showTrainProtein', 'onchange', e => { STATE.showTrainProtein = e.target.checked; render(); });
  on('#showTrainCloud', 'onchange', e => { STATE.showTrainCloud = e.target.checked; render(); });
  // Sort/filter only reorder/trim the system dropdown — re-render the 3D ONLY if the current
  // system actually changed (otherwise dragging a slider would reset the camera every tick).
  const reflow = () => { const prev = STATE.sys; fillSystems(); if (STATE.sys !== prev) render(); };
  on('#sortBy', 'onchange', e => { STATE.sortBy = e.target.value; reflow(); });
  const slider = (id, key, lab) => on(id, 'oninput', e => {
    STATE[key] = +e.target.value; const o = document.getElementById(lab); if (o) o.textContent = e.target.value;
    reflow();
  });
  slider('#minCryst', 'minCryst', 'minCryst-v');
  slider('#minPred', 'minPred', 'minPred-v');
  slider('#minGap', 'minGap', 'minGap-v');
  // ←/→ (or ↑/↓) step through poses, unless focus is in a form control.
  document.addEventListener('keydown', e => {
    if (CURRENT_MODE !== 'single') return;          // arrows only step poses in single mode
    const t = e.target;
    if (t && (t.tagName === 'SELECT' || t.tagName === 'INPUT' || t.isContentEditable)) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); stepPose(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); stepPose(-1); }
  });
  fillPoses();
}

// Filter the SYSTEM dropdown by whether the ensemble contains a correct pose.
function hasCorrect(s) { return s.poses.some(p => p.correct); }
function methodFail(s) { return s.method_fail === true; }   // physics cutoff flags it as untrustworthy
function matchFilter(s) {
  const aw = !hasCorrect(s);
  switch (STATE.filter) {
    case 'hascorrect': return !aw;
    case 'allwrong': return aw;
    case 'missed': return aw && s.method_fail === false;        // all-wrong our rule LET THROUGH
    case 'caught': return aw && methodFail(s);                  // all-wrong our rule flagged
    case 'false_alarm': return !aw && methodFail(s);            // has-correct but flagged
    default: return true;
  }
}
// One-line verdict: what the physics cutoff said vs reality.
function methodVerdict(s) {
  if (s.method_fail == null) return '';
  const aw = !hasCorrect(s);
  const outcome = aw ? (s.method_fail ? 'CAUGHT ✓' : 'MISSED ✗')
                     : (s.method_fail ? 'false alarm ⚠' : 'ok');
  return ` · method ${s.method_fail ? 'FLAG' : 'pass'} (${s.mindist_min}Å) → ${outcome}`;
}
// Range filters on the training-similarity metrics (null metric fails a >0 / >-1 threshold).
function passRanges(s) {
  const c = s.train_shape_overlap, p = s.train_pred_overlap, g = s.train_memo_gap;
  if (STATE.minCryst > 0 && !(c >= STATE.minCryst)) return false;
  if (STATE.minPred > 0 && !(p >= STATE.minPred)) return false;
  if (STATE.minGap > -1 && !(g >= STATE.minGap)) return false;
  return true;
}
const SORT_KEY = { cryst: 'train_shape_overlap', pred: 'train_pred_overlap', gap: 'train_memo_gap' };
function fillSystems() {
  const sysSel = $('#sys'); if (!sysSel) return;
  let vis = DATA.systems.map((s, i) => ({ s, i })).filter(({ s }) => matchFilter(s) && passRanges(s));
  if (STATE.sortBy !== 'default') {
    const k = SORT_KEY[STATE.sortBy];
    vis.sort((a, b) => (b.s[k] ?? -Infinity) - (a.s[k] ?? -Infinity));   // descending
  }
  const label = ({ s }) => {
    const k = SORT_KEY[STATE.sortBy];
    const v = k ? s[k] : null;
    return `${s.id} — ${s.ligand}${v != null && STATE.sortBy !== 'default' ? `  [${v >= 0 ? '' : ''}${v}]` : ''}`;
  };
  sysSel.innerHTML = vis.map(({ s, i }) => `<option value="${i}">${label({ s })}</option>`).join('');
  if (!vis.some(({ i }) => i === STATE.sys)) STATE.sys = vis.length ? vis[0].i : 0;
  sysSel.value = STATE.sys;
  const tot = DATA.systems.length, n = vis.length;
  const lab = document.getElementById('filter-count');
  if (lab) lab.textContent = n === tot ? `${tot} systems` : `${n} of ${tot}`;
}

function fillPoses() {
  const s = DATA.systems[STATE.sys];
  const opts = ['<option value="all">All poses overlaid</option>',
                '<option value="none">None (crystal vs training)</option>'];
  for (const p of s.poses) {
    const tag = p.correct ? '✓' : '✗';
    opts.push(`<option value="${p.sample}">Pose ${p.sample}${p.sample === 1 ? ' (top)' : ''} — ${p.rmsd.toFixed(2)}Å ${tag}</option>`);
  }
  // conserved pose may not exist in this system (different pose count) → fall back to 'all'
  const valid = STATE.pose === 'all' || STATE.pose === 'none' || s.poses.some(p => String(p.sample) === String(STATE.pose));
  if (!valid) STATE.pose = 'all';
  $('#pose').innerHTML = opts.join('');
  $('#pose').value = STATE.pose;
}

// Ordered pose options for keyboard stepping: ['all', '1', '2', ...]. Wraps around.
function poseOrder() {
  return ['all', 'none', ...DATA.systems[STATE.sys].poses.map(p => String(p.sample))];
}
let stepping = false;
async function stepPose(delta) {
  if (stepping) return;                       // ignore presses mid-render (keeps Mol* state sane)
  stepping = true;
  const order = poseOrder();
  let i = order.indexOf(String(STATE.pose));
  if (i < 0) i = 0;
  i = (i + delta + order.length) % order.length;
  STATE.pose = order[i];
  const sel = $('#pose'); if (sel) sel.value = STATE.pose;
  try { await renderPosesOnly(); } finally { stepping = false; }
}

// ---- low-level builders -----------------------------------------------------

// Returns { data, struct }: `data` is the root node so we can delete a whole
// structure sub-tree later (used to swap only the prediction layer on arrowing).
// Cache-buster set once per page load: regenerated data files keep their names, so without this the
// browser serves stale geometry (e.g. old cholesterol poses) even after the pipeline re-runs. Reload
// the page after regenerating data to pick up fresh files.
const CACHE_BUST = Date.now();
const bust = (url) => url + (url.includes('?') ? '&' : '?') + 'v=' + CACHE_BUST;

async function loadStruct(url, format) {
  const data = await plugin.builders.data.download({ url: bust(url), isBinary: false });
  const traj = await plugin.builders.structure.parseTrajectory(data, format);
  const model = await plugin.builders.structure.createModel(traj);
  const struct = await plugin.builders.structure.createStructure(model);
  return { data, struct };
}

async function addRep(struct, selector, type, color, alpha, extra = {}) {
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, selector);
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type,
    typeParams: { alpha, ...extra },
    color: 'uniform',
    colorParams: { value: color },
  });
}

// The crystal protein keeps Mol*'s default element/chain coloring (the PDB look)
// for cartoon/surface; for ball-and-stick we also use the default chemical color.
async function addDefaultColoredRep(struct, selector, type, alpha, extra = {}) {
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, selector);
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type,
    typeParams: { alpha, ...extra },
  });
}

// Ligand rendering matched to the psistructure / PDB-website style: thin
// ball-and-stick (sizeFactor 0.15) with the `element-symbol` color theme so
// O is red, N blue, S yellow — and only the CARBONS are tinted, so we can still
// tell crystal (green C) from correct (teal C) / wrong (red C) poses.
async function addLigand(struct, selector, carbonColor, sizeFactor = 0.15, alpha = 1) {
  const comp = await plugin.builders.structure.tryCreateComponentStatic(struct, selector);
  if (!comp) return null;
  return plugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick',
    typeParams: { sizeFactor, alpha },
    color: 'element-symbol',
    colorParams: { carbonColor: { name: 'uniform', params: { value: carbonColor } } },
  });
}

// ---- main render ------------------------------------------------------------

const REP_TYPE = {
  cartoon: 'cartoon',
  surface: 'molecular-surface',
  'ball-and-stick': 'ball-and-stick',
};

// The scene has two layers tracked separately so arrowing through poses only
// swaps the PREDICTION layer — the reference crystal stays loaded (no flashing).
let refRoots = [], poseRoots = [];

// 1) Reference crystal — pristine RCSB mmCIF, built once per system.
async function buildReference(s) {
  // Multi-copy crystals: a pre-extracted single copy (the prediction's pocket) as PDB, so only one
  // copy renders. Single-copy: the pristine RCSB mmCIF (nicer default cartoon).
  const repType = REP_TYPE[STATE.rep] || 'cartoon';
  if (s.xtal_1copy_file) {
    const { data, struct } = await loadStruct('/' + s.xtal_1copy_file, 'pdb');
    refRoots.push(data);
    refPolymerRepr = await addRep(struct, 'polymer', repType === 'cartoon' ? 'cartoon' : REP_TYPE[STATE.rep], 0x808890, STATE.opacity);
    refLigandRepr = await addLigand(struct, 'ligand', COL.truth, 0.16, STATE.opacity);
    await afterReference(s); return;
  }
  const { data, struct } = await loadStruct('/' + s.xtal_file, 'mmcif');
  refRoots.push(data);
  refPolymerRepr = await addDefaultColoredRep(struct, 'polymer', repType, STATE.opacity);
  refLigandRepr = await addLigand(struct, 'ligand', COL.truth, 0.16, STATE.opacity);
  await afterReference(s);
}

// Training overlays + panel — shared by both crystal paths (single-copy cif / multi-copy 1copy PDB).
async function afterReference(s) {
  // closest pre-cutoff TRAINING ligand, aligned into this frame (purple).
  if (STATE.showTrain && s.train_ligand_file) {
    const t = await loadStruct('/' + s.train_ligand_file, 'pdb');
    refRoots.push(t.data);
    let r = await addLigand(t.struct, 'ligand', COL.train, 0.15);
    if (!r) await addLigand(t.struct, 'all', COL.train, 0.15);
  }
  // closest-training PROTEIN cartoon (purple, semi-transparent), off by default.
  if (STATE.showTrainProtein && s.train_protein_file) {
    const tp = await loadStruct('/' + s.train_protein_file, 'pdb');
    refRoots.push(tp.data);
    await addRep(tp.struct, 'polymer', 'cartoon', COL.train, 0.6);
  }
  // pharmacophore CLOUD — every drug-like ligand bound in this pocket across Foldseek homologs,
  // superposed in. Thin translucent amber sticks; the DENSITY is the signal (where the model has
  // seen chemical matter). Sparse cloud on a correct pose = genuine placement with little precedent.
  if (STATE.showTrainCloud && s.cloud_file) {
    const c = await loadStruct('/' + s.cloud_file, 'pdb');
    refRoots.push(c.data);
    let r = await addLigand(c.struct, 'ligand', COL.cloud, 0.10, 0.55);
    if (!r) await addLigand(c.struct, 'all', COL.cloud, 0.10, 0.55);
  }
  updateTrainPanel(s);
}

// Training-similarity readout panel.
function updateTrainPanel(s) {
  const el = document.getElementById('train-panel');
  if (!el) return;
  if (!('train_pdb' in s)) { el.innerHTML = '<span style="color:var(--faint)">not computed</span>'; return; }
  if (!s.train_pdb) {
    const id = s.train_max_protein_identity != null ? `${Math.round(100*s.train_max_protein_identity)}%` : '—';
    el.innerHTML = `<div>protein in PDB: <b>${id}</b> id</div>`
      + `<div style="color:var(--train)">no pre-cutoff drug-like ligand → <b>novel ligand</b></div>`;
    return;
  }
  const ov = s.train_shape_overlap, pv = s.train_pred_overlap, gap = s.train_memo_gap;
  const id = s.train_identity != null ? `${Math.round(100*s.train_identity)}%` : '—';
  const gapTag = gap >= 0.15 ? 'AF3 hugs training > crystal → memorising'
               : gap <= -0.15 ? 'similar training exists but AF3 missed'
               : 'consistent';
  el.innerHTML =
    `<div>closest pre-2021: <b>${s.train_pdb}</b> (${s.train_het})</div>`
  + `<div>protein identity: <b>${id}</b> · align ${s.train_align_rmsd}Å</div>`
  + `<div>crystal↔train overlap: <b style="color:var(--train)">${ov}</b></div>`
  + `<div>prediction↔train overlap: <b style="color:var(--train)">${pv != null ? pv : '—'}</b></div>`
  + (gap != null ? `<div>memorisation gap: <b style="color:${gap>=0.15?'var(--train)':'var(--ink)'}">${gap>=0?'+':''}${gap}</b> <span style="color:var(--muted)">(${gapTag})</span></div>` : '')
  + (s.train_offsite ? `<div style="color:var(--muted)">⚠ closest fold binds a <b>different pocket</b> (overlap 0) — training ligand hidden</div>` : '')
  + (s.display_approx ? `<div style="color:var(--muted)">⚠ ${s.n_copies}-copy crystal (free ligand) — pose matches a symmetry mate; display approximate</div>` : '')
  + (s.cloud_n ? `<div style="margin-top:4px">pocket cloud: <b style="color:#F2B705">${s.cloud_n}</b> ligands / ${s.cloud_n_pdbs} homolog PDBs `
      + `<span style="color:var(--muted)">(${Object.keys(s.cloud_hets||{}).slice(0,5).join(', ')}${Object.keys(s.cloud_hets||{}).length>5?'…':''})</span></div>` : '');
}

// 2) Prediction layer — predicted ligand(s) + optional model protein. Rebuilt on
//    pose change without touching the reference.
async function buildPoses(s) {
  const poses = STATE.pose === 'none' ? []
    : STATE.pose === 'all' ? s.poses
    : s.poses.filter(p => String(p.sample) === String(STATE.pose));
  for (const p of poses) {
    const { data, struct } = await loadStruct('/' + (p.pose_disp_file || p.pose_file), 'pdb');
    poseRoots.push(data);
    const carbon = p.correct ? COL.correct : COL.wrong;
    const sizeFactor = (STATE.pose === 'all' && p.sample !== 1) ? 0.13 : 0.15;
    let r = await addLigand(struct, 'ligand', carbon, sizeFactor);
    if (!r) await addLigand(struct, 'all', carbon, sizeFactor);
  }
  if (STATE.showModelProtein) {
    const protPose = STATE.pose === 'all' ? s.poses[0] : poses[0];
    if (protPose?.protein_file) {
      const { data, struct } = await loadStruct('/' + (protPose.protein_disp_file || protPose.protein_file), 'pdb');
      poseRoots.push(data);
      await addRep(struct, 'polymer', 'cartoon', COL.modelProtein, 0.85);
    }
  }
  updateInfo(s, poses);
}

async function clearPoses() {
  if (!poseRoots.length) return;
  const b = plugin.build();
  for (const d of poseRoots) b.delete(d.ref || d);
  await b.commit();
  poseRoots = [];
}

function updateInfo(s, poses) {
  const nc = s.poses.filter(p => p.correct).length;
  if (STATE.pose === 'none') {
    $('#info').textContent = `${s.id} · prediction hidden · crystal vs closest training`;
  } else if (STATE.pose === 'all') {
    $('#info').textContent = `${s.id} · ${s.poses.length} poses · ${nc}/${s.poses.length} correct${methodVerdict(s)}`;
  } else if (poses[0]) {
    const p = poses[0];
    $('#info').textContent = `${s.id} · pose ${p.sample} · ${p.rmsd.toFixed(2)}Å · ${p.correct ? 'correct' : 'wrong'}${methodVerdict(s)}`;
  }
}

// Full rebuild — used on system / representation change.
async function render() {
  const s = DATA.systems[STATE.sys];
  const sysChanged = STATE.sys !== renderedSys;
  if (!sysChanged) saveCameraState();
  await plugin.clear();
  refRoots = []; poseRoots = []; refPolymerRepr = refLigandRepr = null;
  $('#info').textContent = 'loading…';
  await buildReference(s);
  await buildPoses(s);
  applyOpacity();
  if (sysChanged) { focusPocket(s.xtal_center); renderedSys = STATE.sys; }
  else { restoreCameraState(); }
}

// Swap only the prediction layer — reference crystal stays put. Explicitly
// save/restore the camera so adding/removing structures can't nudge the view.
async function renderPosesOnly() {
  saveCameraState();
  await clearPoses();
  await buildPoses(DATA.systems[STATE.sys]);
  restoreCameraState();
}

// Reference-protein opacity follows the crossfade slider; for surface we keep it
// a touch more transparent so ligands stay visible.
function refAlphaForRep() {
  return STATE.opacity;
}

// Reference opacity affects BOTH the crystal protein and the crystal ligand.
async function applyOpacity() {
  const b = plugin.build();
  let any = false;
  for (const r of [refPolymerRepr, refLigandRepr]) {
    if (!r) continue;
    b.to(r.ref || r).update(old => { old.type.params.alpha = STATE.opacity; });
    any = true;
  }
  if (any) { try { await b.commit(); } catch (e) { /* best-effort */ } }
}

// Camera persistence — same approach as the psistructure apps.
function saveCameraState() {
  try {
    const cam = plugin?.canvas3d?.camera;
    if (cam) savedCamera = typeof cam.getSnapshot === 'function' ? cam.getSnapshot() : cam.snapshot;
  } catch (e) {}
}
function restoreCameraState() {
  if (!savedCamera) return;
  try {
    const cam = plugin.canvas3d.camera;
    if (typeof cam.setState === 'function') cam.setState(savedCamera, 0);
    setTimeout(() => { try { cam.setState(savedCamera, 0); } catch (e) {} }, 200);
  } catch (e) {}
}

// Frame the scene. requestCameraReset is the bulletproof public API: it fits the
// full loaded content (protein + all poses, including far-off wrong ones) — which
// is exactly what you want for a pose-vs-crystal benchmark. The pocket-centroid
// arg is accepted for API symmetry / future use.
function focusPocket(_center) {
  try { plugin.canvas3d?.requestCameraReset(); }
  catch (e) { /* no-op */ }
}

init().catch(e => { $('#info').textContent = 'error: ' + e.message; console.error(e); });
