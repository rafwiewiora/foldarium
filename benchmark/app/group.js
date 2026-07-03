// Within-target (fragment-screen) overlay mode.
//
// One shared reference protein (pristine RCSB cif -> Mol* default CARTOON, the
// PDB-website look), with EVERY fragment's AF3-predicted ligand overlaid at once,
// all pre-aligned into the reference crystal's coordinate frame by build_group.py.
// Each fragment is colored by correctness (teal correct <2A, red wrong >=2A).
//
// Toggles: top-pose-only vs all-5-poses, show/hide truth ligands, show/hide
// correct/wrong fragments, reference opacity. Drives the SAME Mol* plugin as the
// single-system view (app.js) — only one viewer exists.

(function () {
  const $ = s => document.querySelector(s);

  // Colors match app.js / index.html legend.
  const GCOL = { truth: 0x2BA84A, correct: 0x2E9BD6, wrong: 0xE23B2E };

  // `frag` is 'all' (overlay every fragment) or a fragment id string (step through one).
  const GSTATE = { group: 0, frag: 'all', poses: 'top', opacity: 0.55, truth: false, correct: true, wrong: true };

  let GROUPS = null;          // list of {name,label,file} group manifests (lazy-loaded)
  let MAN = null;             // active group manifest (fragments etc.)
  let refRepr = null;         // reference protein representation handle (opacity)
  let refRoots = [], ligRoots = [];   // structure roots for teardown
  let built = false;          // controls wired?
  let framed = false;         // auto-framed this group yet?
  let stepping = false;       // guard against overlapping arrow renders

  // Which group manifests exist. We only built A2A; allow easy extension by listing
  // group_*.json names here. Probe each and keep the ones that load.
  const CANDIDATE_GROUPS = [{ name: 'a2a', file: '/group_a2a.json' }];

  function plugin() { return window.COFOLD.plugin; }

  // ---- low-level builders (same patterns as app.js) -------------------------
  async function loadStruct(url, format) {
    const p = plugin();
    const data = await p.builders.data.download({ url, isBinary: false });
    const traj = await p.builders.structure.parseTrajectory(data, format);
    const model = await p.builders.structure.createModel(traj);
    const struct = await p.builders.structure.createStructure(model);
    return { data, struct };
  }

  async function addDefaultColoredRep(struct, selector, type, alpha) {
    const p = plugin();
    const comp = await p.builders.structure.tryCreateComponentStatic(struct, selector);
    if (!comp) return null;
    return p.builders.structure.representation.addRepresentation(comp, {
      type, typeParams: { alpha },
    });
  }

  // Thin ball-and-stick, element-symbol theme with only carbons tinted — same as
  // app.js addLigand, so O=red/N=blue and the carbon tint encodes correctness.
  async function addLigand(struct, selector, carbonColor, sizeFactor = 0.15, alpha = 1) {
    const p = plugin();
    const comp = await p.builders.structure.tryCreateComponentStatic(struct, selector);
    if (!comp) return null;
    return p.builders.structure.representation.addRepresentation(comp, {
      type: 'ball-and-stick',
      typeParams: { sizeFactor, alpha },
      color: 'element-symbol',
      colorParams: { carbonColor: { name: 'uniform', params: { value: carbonColor } } },
    });
  }

  // ---- controls -------------------------------------------------------------
  function on(sel, ev, fn) { const el = $(sel); if (el) el[ev] = fn; }

  async function loadGroupList() {
    if (GROUPS) return GROUPS;
    GROUPS = [];
    for (const c of CANDIDATE_GROUPS) {
      try {
        const m = await fetch(c.file).then(r => r.ok ? r.json() : null);
        if (m) GROUPS.push({ ...c, manifest: m });
      } catch (e) { /* skip missing group */ }
    }
    return GROUPS;
  }

  function fillGroups() {
    const sel = $('#group'); if (!sel) return;
    sel.innerHTML = GROUPS.map((g, i) =>
      `<option value="${i}">${g.manifest.label} — ${g.manifest.n_included} fragments</option>`).join('');
    sel.value = GSTATE.group;
  }

  // Fragment dropdown: "All fragments (overlay)" + one option per fragment,
  // labelled <pdb> · <het> · <rmsd>Å ✓/✗. Order matches manifest.fragments.
  function fillFragments() {
    const sel = $('#gfrag'); if (!sel || !MAN) return;
    const opts = ['<option value="all">All fragments (overlay)</option>'];
    for (const f of MAN.fragments) {
      const tag = f.correct ? '✓' : '✗';
      const rmsd = (typeof f.pose1_rmsd === 'number') ? f.pose1_rmsd.toFixed(2) : '?';
      opts.push(`<option value="${f.id}">${f.id} · ${f.ligand} · ${rmsd}Å ${tag}</option>`);
    }
    sel.innerHTML = opts.join('');
    sel.value = GSTATE.frag;
  }

  function wireControls() {
    if (built) return;
    built = true;
    fillGroups();
    on('#group', 'onchange', e => {
      GSTATE.group = +e.target.value; GSTATE.frag = 'all'; framed = false; render();
    });
    on('#gfrag', 'onchange', e => { GSTATE.frag = e.target.value; renderLigandsOnly(); });
    on('#gposes', 'onchange', e => { GSTATE.poses = e.target.value; renderLigandsOnly(); });
    on('#gopacity', 'oninput', e => { GSTATE.opacity = +e.target.value / 100; applyOpacity(); });
    on('#gtruth', 'onchange', e => { GSTATE.truth = e.target.checked; renderLigandsOnly(); });
    on('#gcorrect', 'onchange', e => { GSTATE.correct = e.target.checked; renderLigandsOnly(); });
    on('#gwrong', 'onchange', e => { GSTATE.wrong = e.target.checked; renderLigandsOnly(); });
    on('#greset', 'onclick', () => { try { plugin().canvas3d?.requestCameraReset(); } catch (e) {} });
    // ←/→ (or ↑/↓) step through fragments — only in group mode, never while a
    // form control has focus. Mirrors app.js's single-mode pose stepping.
    document.addEventListener('keydown', e => {
      if (!document.getElementById('mode-group')?.classList.contains('on')) return;
      const t = e.target;
      if (t && (t.tagName === 'SELECT' || t.tagName === 'INPUT' || t.isContentEditable)) return;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); stepFragment(1); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); stepFragment(-1); }
    });
  }

  // Ordered fragment options for keyboard stepping: ['all', <id>, <id>, ...]. Wraps.
  function fragOrder() {
    return ['all', ...MAN.fragments.map(f => f.id)];
  }
  async function stepFragment(delta) {
    if (stepping) return;                 // ignore presses mid-render
    if (!MAN) return;
    stepping = true;
    const order = fragOrder();
    let i = order.indexOf(String(GSTATE.frag));
    if (i < 0) i = 0;
    i = (i + delta + order.length) % order.length;
    GSTATE.frag = order[i];
    const sel = $('#gfrag'); if (sel) sel.value = GSTATE.frag;
    try { await renderLigandsOnly(); } finally { stepping = false; }
  }

  // ---- render ---------------------------------------------------------------
  // When a single fragment is selected, show ONLY that one (ignore the
  // correct/wrong filters — the user explicitly picked it). In 'all' mode, the
  // correct/wrong toggles apply as before.
  function visibleFragments() {
    if (GSTATE.frag !== 'all') {
      return MAN.fragments.filter(f => f.id === GSTATE.frag);
    }
    return MAN.fragments.filter(f =>
      (f.correct && GSTATE.correct) || (!f.correct && GSTATE.wrong));
  }

  async function buildReference() {
    const { data, struct } = await loadStruct('/' + MAN.ref_file, 'mmcif');
    refRoots.push(data);
    refRepr = await addDefaultColoredRep(struct, 'polymer', 'cartoon', GSTATE.opacity);
  }

  async function buildLigands() {
    for (const f of visibleFragments()) {
      const carbon = f.correct ? GCOL.correct : GCOL.wrong;
      const poses = GSTATE.poses === 'top'
        ? f.poses.filter(p => p.sample === 1)
        : f.poses;
      for (const p of poses) {
        // In all-poses mode color each pose by its OWN rmsd; in top mode use the
        // fragment's pose-1 correctness (already == carbon).
        const c = GSTATE.poses === 'all' ? (p.correct ? GCOL.correct : GCOL.wrong) : carbon;
        const sf = (GSTATE.poses === 'all' && p.sample !== 1) ? 0.12 : 0.15;
        const { data, struct } = await loadStruct('/' + p.pose_file, 'pdb');
        ligRoots.push(data);
        let r = await addLigand(struct, 'ligand', c, sf);
        if (!r) await addLigand(struct, 'all', c, sf);
      }
      if (GSTATE.truth && f.truth_file) {
        const { data, struct } = await loadStruct('/' + f.truth_file, 'pdb');
        ligRoots.push(data);
        let r = await addLigand(struct, 'ligand', GCOL.truth, 0.16);
        if (!r) await addLigand(struct, 'all', GCOL.truth, 0.16);
      }
    }
  }

  async function clearLigands() {
    if (!ligRoots.length) return;
    const b = plugin().build();
    for (const d of ligRoots) b.delete(d.ref || d);
    await b.commit();
    ligRoots = [];
  }

  async function applyOpacity() {
    if (!refRepr) return;
    const b = plugin().build();
    b.to(refRepr.ref || refRepr).update(old => { old.type.params.alpha = GSTATE.opacity; });
    try { await b.commit(); } catch (e) { /* best-effort */ }
  }

  function updateInfo() {
    const el = $('#info');
    if (!el) return;
    if (GSTATE.frag !== 'all') {
      const f = MAN.fragments.find(x => x.id === GSTATE.frag);
      if (f) {
        const rmsd = (typeof f.pose1_rmsd === 'number') ? f.pose1_rmsd.toFixed(2) : '?';
        el.textContent =
          `${MAN.label} · ref ${MAN.reference_pdb} · ${f.id} · ${f.ligand} · ` +
          `${rmsd}Å · ${f.correct ? 'correct' : 'wrong'}`;
        return;
      }
    }
    const vis = visibleFragments();
    const nc = vis.filter(f => f.correct).length;
    el.textContent =
      `${MAN.label} · ref ${MAN.reference_pdb} · ${vis.length} fragments shown · ` +
      `${nc} correct / ${vis.length - nc} wrong (pose-1 <2Å)`;
  }

  // Camera persistence — same approach as app.js (save/restore so adding or
  // removing structures can't nudge the view while stepping fragments).
  let savedCam = null;
  function saveCam() {
    try {
      const cam = plugin()?.canvas3d?.camera;
      if (cam) savedCam = typeof cam.getSnapshot === 'function' ? cam.getSnapshot() : cam.snapshot;
    } catch (e) {}
  }
  function restoreCam() {
    if (!savedCam) return;
    try {
      const cam = plugin().canvas3d.camera;
      if (typeof cam.setState === 'function') cam.setState(savedCam, 0);
      setTimeout(() => { try { cam.setState(savedCam, 0); } catch (e) {} }, 150);
    } catch (e) {}
  }

  // Full rebuild of the group scene (reference + ligands). Used on group change.
  async function render() {
    MAN = GROUPS[GSTATE.group].manifest;
    fillFragments();
    let cam = null;
    if (framed) { try { cam = plugin().canvas3d?.camera?.getSnapshot?.(); } catch (e) {} }
    try { await plugin().clear(); } catch (e) {}
    refRoots = []; ligRoots = []; refRepr = null;
    if ($('#info')) $('#info').textContent = 'loading…';
    await buildReference();
    await buildLigands();
    updateInfo();
    if (!framed) {
      try { plugin().canvas3d?.requestCameraReset(); } catch (e) {}
      framed = true;
    } else if (cam) {
      try { const c = plugin().canvas3d.camera; c.setState(cam, 0);
        setTimeout(() => { try { c.setState(cam, 0); } catch (e) {} }, 150); } catch (e) {}
    }
  }

  // Swap ONLY the ligand layer — the shared reference protein stays loaded and the
  // camera is held fixed. Used for fragment stepping and the per-fragment toggles,
  // so stepping is smooth (no reference reflash, no view reset). Mirrors app.js's
  // renderPosesOnly + saveCameraState/restoreCameraState.
  async function renderLigandsOnly() {
    saveCam();
    await clearLigands();
    await buildLigands();
    updateInfo();
    restoreCam();
  }

  // Entry point called by the mode switch in app.js. Always start on the full
  // overlay so "All fragments" behaves exactly as before on (re)entry.
  async function enter() {
    await loadGroupList();
    if (!GROUPS.length) {
      if ($('#info')) $('#info').textContent = 'no group data found (run build_group.py)';
      return;
    }
    GSTATE.frag = 'all';
    framed = false;            // auto-frame the overlay each time we (re)enter group mode
    wireControls();
    await render();
  }

  window.GROUP = { enter };
})();
