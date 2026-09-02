// Pose Quiz — binding pocket (ligand removed) + shuffled, CLUSTERED ligand poses. Player picks the cluster they think
// is correct (or, in Hard mode, "none of these are correct"). Two quizzes: CAMEO (AlphaFold3 only) and
// Runs-n-Poses (poses pooled from multiple co-folding methods, ANONYMISED during play). The weekly
// prospective quiz intentionally shows each method and its ligand confidence because calibration differs.
// Reuses the viewer's Mol* setup + its proven delete-and-rebuild pattern. Pose carbons are coloured by a
// NON-semantic per-cluster palette (random each question) only for identification — never correctness.

const PALETTE = [0x5B8FF9, 0xF6BD16, 0x9270CA, 0x5AD8A6, 0xE8964A, 0x6DC8EC,
  0xFF99C3, 0x8C6D31, 0xB5BD61, 0x17BECF, 0xBC80BD, 0xFDB462, 0x80B1D3, 0xFCCDE5, 0xB3DE69];
const LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
// RnP keeps these names hidden until reveal; Weekly shows them with per-pose ligand confidence during play.
const METHOD_NAMES = { af3: 'AF3', openfold3: 'OpenFold3', boltz: 'Boltz-1', boltz2: 'Boltz-2', chai: 'Chai-1', protenix: 'Protenix' };
const methodName = m => METHOD_NAMES[m] || m;
const DEV2_FEEDBACK = window.foldariumDev2Feedback || {};
const GRID_PAGE_SIZE = DEV2_FEEDBACK.GRID_PAGE_SIZE || 9;
const QUESTION_PREFETCH_LOOKAHEAD = 3;
const viewerPerformance = window.foldariumViewerPerformance || {
  current: () => null,
  beginQuestion: () => null,
  measure: (_report, _stage, operation) => operation(),
  milestone: () => {},
  finishQuestion: () => null,
  measureStartup: (_stage, operation) => operation(),
};
function weeklyPoseEvidence(choice) {
  if (!choice?._method) return '';
  const confidence = choice._confidence;
  const confidenceValue = confidence?.metric === 'ligand_plddt' && Number.isFinite(confidence.value)
    ? ` · ligand pLDDT ${confidence.value.toFixed(1)}`
    : '';
  const smina = choice._sminaScore;
  const sminaValue = smina?.metric === 'smina_affinity' && Number.isFinite(smina.value)
    ? ` · smina ${smina.value.toFixed(1)} kcal/mol`
    : '';
  const interactions = choice._interactionCount;
  const interactionValue = interactions?.metric === 'prolif_hbond_residue_count'
      && Number.isInteger(interactions.value) && interactions.value >= 0
    ? ` · H-bonds ${interactions.value}`
    : '';
  return `${methodName(choice._method)}${confidenceValue}${sminaValue}${interactionValue}`;
}
function weeklyLigandPlddt(choice) {
  const confidence = choice?._confidence;
  return confidence?.metric === 'ligand_plddt' && Number.isFinite(confidence.value)
    ? `ligand pLDDT ${confidence.value.toFixed(1)}`
    : '';
}
function weeklyHbondCount(choice) {
  const interactions = choice?._interactionCount;
  return interactions?.metric === 'prolif_hbond_residue_count'
      && Number.isInteger(interactions.value) && interactions.value >= 0
    ? `H-bonds ${interactions.value}`
    : '';
}
function weeklyEntryEvidence(entry) {
  if (cur?.item?.source !== 'weekly') return '';
  const members = Array.isArray(entry.members)
    ? entry.members
    : (clustered && entry.cluster ? entry.cluster.members : [entry.choice]);
  return members.map(weeklyPoseEvidence).filter(Boolean).join('\n');
}
function poseInfoTooltipRows(evidence) {
  return String(evidence || '').split('\n').filter(Boolean).map(line => {
    const [method, ...rawMetrics] = line.split(' · ');
    const metrics = rawMetrics.map(metric => {
      const match = /^(ligand pLDDT|smina|H-bonds)\s+(.+)$/.exec(metric);
      return match ? { label: match[1], value: match[2] } : { label: 'metric', value: metric };
    });
    return { method, metrics };
  });
}
const GOOD = 0x2BA84A, BAD = 0xE23B2E, PROT = 0x9aa6b2, AF3PROT = 0x8FA8CC, XTAL = 0x8B5CF6;
const TRAINING_LIGAND = 0x0891B2;
const XTAL_POSE_SIZE = 0.32;
const REJECTED_POSE = 0x9aa6b2;
const GHOST_POSE_ALPHA = 0.10, GHOST_POSE_SIZE = 0.14, GHOST_PROTEIN_ALPHA = 0.12;
const ENABLE_PROTEIN_ENSEMBLE_EXPERIMENT = false;
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

const APP_QUERY = new URLSearchParams(location.search);
const DEV = APP_QUERY.has('dev');   // no-vote inspection/browse mode (?dev=1)
const WEEKLY_ONLY = window.FOLDARIUM_QUIZ_MODE === 'weekly';
const DEPLOYMENT_PERFORMANCE_BETA =
  window.FOLDARIUM_SUPABASE?.performanceBetaEnabled === true;
const PERFORMANCE_RECORDING_REQUESTED = DEPLOYMENT_PERFORMANCE_BETA
  || (APP_QUERY.has('perf') && APP_QUERY.get('record_performance') === '1');
// Accepted Weekly speedups are enabled by default. Explicit zero-valued query
// switches remain available only for controlled A/B diagnosis.
const GRID_VIEWER_POOL_ENABLED = WEEKLY_ONLY && APP_QUERY.get('viewer_pool') !== '0';
const FAST_GRID_CAMERA_SYNC_ENABLED = WEEKLY_ONLY && APP_QUERY.get('fast_camera') !== '0';
const GRID_VIEWER_PREWARM_ENABLED = GRID_VIEWER_POOL_ENABLED
  && APP_QUERY.get('warm_viewers') !== '0';
const gridViewerPool = window.foldariumGridViewerPool?.createGridViewerPool?.({
  enabled: GRID_VIEWER_POOL_ENABLED,
  maxSize: GRID_PAGE_SIZE,
}) || {
  enabled: false,
  add: cell => {
    try { cell?.viewer?.dispose?.(); } catch (_) {}
    return false;
  },
  release: cell => {
    try { cell?.viewer?.dispose?.(); } catch (_) {}
    return false;
  },
  acquire: async () => null,
  drain: () => {},
  size: () => 0,
};
const researchBackend = () => DEV ? null : window.foldariumBackend;
const isReadOnlyPreview = () => window.FOLDARIUM_SUPABASE?.enabled === true
  && window.FOLDARIUM_SUPABASE?.writable === false;
const isPrivatePrecloseReview = () => window.FOLDARIUM_PRIVATE_REVIEW?.active === true;
const isArchiveRetrospective = () => window.FOLDARIUM_ARCHIVE_REVIEW?.active === true;
const isArchivePlayForFun = () => window.FOLDARIUM_ARCHIVE_PLAY?.active === true;
const isRetrospectiveReview = () => isPrivatePrecloseReview() || isArchiveRetrospective();
const itemHasReleasedCrystal = item => !!item?.released_crystal?.cif_url;
const itemHasXtalOverlay = item => typeof item?.xtal_lig_file === 'string' && !!item.xtal_lig_file;
const isXtalReferenceChoice = choice => choice?._xtalReference === true;
const isTrainingReferenceChoice = choice => choice?._trainingReference === true;
const isFixedReferenceChoice = choice => (
  isXtalReferenceChoice(choice) || isTrainingReferenceChoice(choice)
);
function buildXtalReferenceChoice(item) {
  const overlay = item?.answer_overlay;
  const sourcePose = [...(overlay?.poses || [])]
    .sort((left, right) => left.rmsd - right.rmsd || left.id.localeCompare(right.id))[0];
  return {
    _xtalReference: true,
    _weeklyChoiceId: '__xtal_reference__',
    id: '__xtal_reference__',
    label: 'Xtal',
    color: XTAL,
    answer_crystal_pdb: overlay?.crystal_ligand_pdb || sourcePose?.crystal_ligand_pdb,
    answer_crystal_pocket_pdb: sourcePose?.crystal_pocket_pdb,
    correct: false,
    clusterAccepted: false,
    rmsd: 0,
  };
}
function buildTrainingReferenceChoice(item) {
  const similarity = item?.similarity;
  if (!similarity?.overlay?.object_uri) return null;
  return {
    _trainingReference: true,
    _weeklyChoiceId: '__training_reference__',
    id: '__training_reference__',
    label: 'Training',
    color: TRAINING_LIGAND,
    pose_file: similarity.overlay.object_uri,
    training_pdb: similarity.train_pdb?.toUpperCase() || null,
    training_ligand: similarity.train_het || null,
    training_score: Number.isFinite(similarity.train_shape_overlap)
      ? similarity.train_shape_overlap : null,
    correct: false,
    clusterAccepted: false,
    rmsd: null,
  };
}
function trainingReferenceAnnotation(choice = buildTrainingReferenceChoice(cur?.item)) {
  if (!choice) return '';
  const source = choice.training_pdb && choice.training_ligand
    ? `${choice.training_pdb} + ${choice.training_ligand}`
    : 'source unavailable';
  const score = Number.isFinite(choice.training_score)
    ? choice.training_score.toFixed(4)
    : 'not scored';
  return `Closest training · ${source} · overlap ${score}`;
}
function retrospectiveNavChoices() {
  const base = visibleChoices();
  if (!retrospectiveAnswerActive()) return base;
  if (displayMode === 'all') return base;
  const references = [];
  if (itemHasReleasedCrystal(cur.item)) references.push(buildXtalReferenceChoice(cur.item));
  const training = buildTrainingReferenceChoice(cur.item);
  if (training) references.push(training);
  return [...base, ...references];
}
const viewingReleasedCrystal = () => releasedCrystalMode && itemHasReleasedCrystal(cur?.item);

function resetCrystalViewState() {
  showXtal = false;
  releasedCrystalMode = false;
  releasedCrystalError = '';
  const checkbox = $('#showXtal');
  if (checkbox) checkbox.checked = false;
  syncXtalRow();
}

function syncXtalRow() {
  const row = $('#xtalrow');
  const label = $('#xtal-label');
  const link = $('#rcsb-link');
  const status = $('#xtal-status');
  if (!row || !label) return;
  const crystalReviewAllowed = cur?.item?.source !== 'weekly' || isRetrospectiveReview();
  const showRow = !!(crystalReviewAllowed && cur?.showAnswer
    && (itemHasXtalOverlay(cur.item) || itemHasReleasedCrystal(cur.item)));
  row.style.display = showRow ? '' : 'none';
  if (!showRow) {
    if (link) link.style.display = 'none';
    if (status) status.textContent = '';
    return;
  }
  if (itemHasReleasedCrystal(cur.item)) {
    const released = cur.item.released_crystal;
    label.innerHTML = isPrivatePrecloseReview()
      ? '<span style="color:#2BA84A">Predictions aligned to crystal protein</span> · '
        + '<span style="color:#8B5CF6">crystal ligand: violet</span>'
      : (releasedCrystalMode
        ? 'Released crystal visible <span style="color:#8B5CF6">(uncheck for predicted poses)</span>'
        : 'View released crystal structure <span style="color:#8B5CF6">(target ligand, violet)</span>');
    if (link) {
      link.href = released.structure_page_url;
      link.style.display = '';
      link.textContent = 'Open in RCSB ↗';
    }
    if (status) status.textContent = releasedCrystalError;
    const checkbox = $('#showXtal');
    if (checkbox) {
      checkbox.checked = isPrivatePrecloseReview() ? showXtal : releasedCrystalMode;
      checkbox.disabled = isPrivatePrecloseReview();
    }
    return;
  }
  label.innerHTML = 'Show crystal reference <span style="color:#8B5CF6">(true pose, violet)</span>';
  if (link) link.style.display = 'none';
  if (status) status.textContent = '';
}
const assetUrl = path => window.foldariumAssetUrl?.(path) || path;
let viewer, plugin, ITEMS = [], idx = 0, cur = null;
const performanceDiagnosticsCollector = PERFORMANCE_RECORDING_REQUESTED
  ? window.foldariumPerformanceDiagnostics?.createPerformanceDiagnosticsCollector?.()
  : null;
let POOLS = { cameo: [], rnp: [], weekly: [] };
let quizSource = WEEKLY_ONLY ? 'weekly' : 'cameo', difficulty = WEEKLY_ONLY ? 'hard' : 'easy';
let WEEKLY_ROUND = null;
let WEEKLY_VOTES = new Map(), WEEKLY_TOTALS = new Map();
let WEEKLY_LEADERBOARD = null;
let WEEKLY_FOR_FUN_LEADERBOARD = null;
let WEEKLY_QUESTION_RESULTS = null;
let WEEKLY_ARCHIVE_DETAIL = null;
let WEEKLY_RETROSPECTIVE_SUMMARY = null;
let retrospectiveQuestionFilter = 'all';
let retrospectiveSimilaritySort = 'default';
let WEEKLY_LEADERBOARD_ERROR = '';
let localWeeklyScore = { correct: 0, answered: 0 };
let localWeeklyScoredItems = new Set();
let WEEKLY_SELECTOR_RESULTS = null;
let WEEKLY_SELECTOR_RESULTS_ERROR = '';
let WEEKLY_ITEM_STATES = new Map();
let WEEKLY_PREFETCHED_CLUSTERS = new Map();
let WEEKLY_PREPARED_SESSION = null;
let weeklyCommentPromptEnabled = true;
let remoteSessionId = null;
let participantDisplayName = '';
let viewerTraceRecorder = null;
let weeklyTraceStream = null;
let weeklyTraceSessionSeed = null;
let viewerRebuild = null, revealAfterIdle = null, revealRequested = false;
let viewerTransitionBusy = false;
let pendingQuestionPrefetchIndexes = [];
let displayMode = WEEKLY_ONLY ? 'grid' : 'all', clustered = true, shownOne = 0, showXtal = false, releasedCrystalMode = false, releasedCrystalError = '', proteinMode = 'crystal';
let showHbonds = false;   // H-bond overlay toggle — persisted across questions like the other view choices
let retrospectiveHbondStatus = '';
let showProteinEnsemble = false; // optional faint receptor backbones for the Weekly visual experiment
let showSurface = false;
let gridViewers = [], gridBuildRevision = 0, gridMethodIndex = 0;
let gridViewerPrewarmGeneration = 0;
let pendingQuestionPerformanceTiming = null;
let activePaneId = null, selectedPaneId = null;
let stopGridCameraSync = null, stopGridLayout = null;
let poseChoiceByRepresentation = new WeakMap();
let retrospectiveProteinFrame = 'xtal';
let retrospectiveGridProteinFrames = new Map();
let canonicalPoseClickSubscription = null;
let nextCanonicalCameraSnapshot = null, canonicalPoseActivationRevision = 0;
let resetCameraOnNextBuild = false;
let weeklyCountdownTimer = null;
// The user's chosen "my view" display preferences, persisted ACROSS questions. reveal()/toggleAnswer()
// temporarily override the live globals to render the correctness list (always all/unclustered), so we
// remember the user's real choice here and restore/seed from it (loadQuestion, back-to-my-view).
let userView = { displayMode, clustered: true, proteinMode: 'crystal', showHbonds: false,
  showProteinEnsemble: false, showSurface: false };
const rememberView = () => { userView = { displayMode, clustered, proteinMode, showHbonds,
  showProteinEnsemble, showSurface }; };
const applyUserView = () => {
  ({ displayMode, clustered, proteinMode, showHbonds, showProteinEnsemble, showSurface } = userView);
  if (quizSource === 'rnp' || quizSource === 'weekly') proteinMode = 'crystal';
};
let score = { you: 0, af3: 0, n: 0, randExp: 0 };
const $ = s => document.querySelector(s);
const CACHE_BUST = Date.now();
const hex = c => '#' + c.toString(16).padStart(6, '0');
// "locked" = the green/red answer is on screen; controls are inert only then. In "my view" (revealed but
// answer hidden) everything is interactive again, exactly as before voting.
const locked = () => cur && cur.revealed && cur.showAnswer;
const interactionBlocked = () => viewerTransitionBusy || revealRequested || locked();
// Viewer chrome stays inert during ordinary answer reveal, but private retrospective answer
// review still needs display mode / surface / H-bond controls without unlocking vote actions.
const viewerControlBlocked = () => viewerTransitionBusy || revealRequested
  || (locked() && !retrospectiveAnswerActive());
const oppLabel = () => (quizSource === 'rnp' ? 'Best automated pick (ligand pLDDT)'
  : (quizSource === 'weekly' ? 'Ligand pLDDT' : 'AlphaFold3 (pLDDT-ranked)'));
const opponentChoiceCorrect = choice => quizSource === 'weekly'
  ? choice?.correct === true
  : acceptedChoiceCorrect(choice);

function currentReplayableAppState({ includeVoteComment = false, continuousTrace = null } = {}) {
  const selectionKind = !cur?.selected ? null
    : (cur.selected.none ? 'none' : (cur.selectionExact ? 'exact' : 'cluster'));
  const selectedChoiceIds = cur?.selected && !cur.selected.none
    ? reviewChoiceIds(cur.selected).slice(0, 20) : [];
  const shownChoice = cur && displayMode === 'one' ? visibleChoices()[shownOne] : null;
  return {
    schema_version: 1,
    source: quizSource,
    difficulty,
    round_id: quizSource === 'weekly' ? (WEEKLY_ROUND?.round_id || null) : null,
    question_index: cur ? idx : null,
    item_id: cur?.item?.id || null,
    display_mode: displayMode,
    clustered,
    protein_mode: proteinMode,
    show_hbonds: showHbonds,
    show_protein_ensemble: showProteinEnsemble,
    show_surface: showSurface,
    show_xtal: showXtal,
    released_crystal_mode: releasedCrystalMode,
    shown_one_index: shownOne,
    grid_page_index: gridMethodIndex,
    active_pane_id: activePaneId,
    selected_pane_id: selectedPaneId,
    selection_kind: selectionKind,
    selected_choice_id: selectedChoiceIds[0] || null,
    selected_choice_ids: selectedChoiceIds,
    shown_choice_id: shownChoice?._weeklyChoiceId || null,
    context_choice_id: cur?.contextChoice?._weeklyChoiceId || null,
    rejected_choice_ids: cur ? [...(cur.rejectedChoiceIds || [])].slice(0, 50) : [],
    vote_comment_enabled: quizSource === 'weekly' ? weeklyCommentPromptEnabled : null,
    ...(includeVoteComment && typeof cur?.voteCommentText === 'string'
      ? { vote_comment: cur.voteCommentText }
      : {}),
    ...(continuousTrace ? { continuous_trace: continuousTrace } : {}),
    viewer_busy: viewerTransitionBusy || revealRequested,
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      dpr: window.devicePixelRatio || 1,
    },
  };
}

function newVoteAttemptId() {
  return crypto.randomUUID();
}

function invalidatePendingWeeklyVote() {
  if (!cur || cur.item?.source !== 'weekly') return;
  cur.pendingWeeklyVote = null;
  cur.voteCommentHandled = false;
  cur.voteCommentText = null;
}

function rememberWeeklyItemState() {
  if (!cur || cur.item?.source !== 'weekly') return;
  cur.savedShownOne = shownOne;
  cur.savedGridPage = gridMethodIndex;
  WEEKLY_ITEM_STATES.set(cur.item.id, cur);
}

function syncWeeklyGuideContent() {
  if (!isRetrospectiveReview()) return;
  $('#quick-start-open').textContent = 'Scoring rules';
  $('#quick-start-title').textContent = 'Scoring rules';
  $('#quick-start-intro').textContent = 'How clustered and exact-pose selections are defined and scored.';
  $('.quick-start-list').innerHTML = `
    <li><div><strong>Clusters use 2.0 Å</strong><p>The frozen prospective clustering uses a strict ligand-RMSD cutoff below 2.0 Å after shared receptor alignment. It does not use the later crystal answer.</p></div></li>
    <li><div><strong>The representative is the medoid</strong><p>Before scoring, each cluster is shown by the member with the lowest total distance to the other members; ties are deterministic. It is only a display choice, not an exact-pose vote.</p></div></li>
    <li><div><strong>Choose cluster or exact pose</strong><p>With clustering on, a vote selects the whole cluster. With <b>Uncluster</b> on, it selects one raw pose. Selector/API ballots can submit independent cluster and exact-pose decisions; results keep them separate.</p></div></li>
    <li><div><strong>Scoring uses 1.5 Å</strong><p>An exact pose is correct below 1.5 Å to crystal. A cluster is correct when any member is below 1.5 Å. Yellow marks a pose outside 1.5 Å that belongs to a correct cluster; <b>None</b> is correct only when no pose passes.</p></div></li>`;
  $('#quick-start-close').textContent = 'Close';
}

const RETROSPECTIVE_QUESTION_FILTERS = [
  ['all', 'All questions'],
  ['pose', 'Has a correct pose'],
  ['none', 'No correct pose'],
  ['pose-solved', 'Correct pose · someone right'],
  ['pose-unsolved', 'Correct pose · nobody right'],
  ['none-solved', 'No pose · someone chose None'],
  ['none-unsolved', 'No pose · nobody chose None'],
];

function weeklyItemHasCorrectPose(item) {
  return item?.choices?.some(choice => choice?.correct === true) === true;
}

function weeklyQuestionResultForItem(item) {
  return WEEKLY_QUESTION_RESULTS?.items?.find(result => result.item_id === item?.id) || null;
}

function restoreWeeklyPriorVote(questionState, prior, clusters) {
  if (!questionState || !prior || !Array.isArray(clusters)) return false;
  const choices = clusters.flatMap(cluster => cluster.members);
  if (prior.picked_none) {
    questionState.selected = {
      none: true,
      correct: !choices.some(acceptedChoiceCorrect),
      label: 'None of these',
    };
    questionState.selectionExact = true;
    questionState.selectedAsCluster = false;
    questionState.answerChoices = choices;
    return true;
  }
  if (!prior.choice_id) return false;
  const choice = choices.find(member => member._weeklyChoiceId === prior.choice_id);
  if (!choice) return false;
  const exact = prior.selection_kind === 'exact';
  questionState.selected = choice;
  questionState.selectionExact = exact;
  questionState.selectedAsCluster = !exact;
  questionState.answerChoices = choices;
  return true;
}

function retrospectiveQuestionMatches(item, filter = retrospectiveQuestionFilter) {
  if (filter === 'all') return true;
  const hasPose = weeklyItemHasCorrectPose(item);
  const result = weeklyQuestionResultForItem(item);
  const solved = Number(result?.correct_count || 0) > 0;
  if (filter === 'pose') return hasPose;
  if (filter === 'none') return !hasPose;
  if (filter === 'pose-solved') return hasPose && solved;
  if (filter === 'pose-unsolved') return hasPose && !solved;
  if (filter === 'none-solved') return !hasPose && solved;
  if (filter === 'none-unsolved') return !hasPose && !solved;
  return true;
}

function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter) {
  if (!isRetrospectiveReview()) return ITEMS.map((_, index) => index);
  const rows = ITEMS
    .map((item, index) => ({
      item,
      index,
      publicationIndex: Number.isSafeInteger(item.publicationIndex)
        ? item.publicationIndex
        : index,
      similarity: item.similarity || null,
    }))
    .filter(({ item }) => retrospectiveQuestionMatches(item, filter));
  if (typeof isArchiveRetrospective !== 'function' || !isArchiveRetrospective()) {
    return rows.map(({ index }) => index);
  }
  const sortRows = window.foldariumWeeklyTrainingSimilarity?.sortWeeklySimilarityRows;
  return (typeof sortRows === 'function'
    ? sortRows(rows, retrospectiveSimilaritySort)
    : rows
  ).map(({ index }) => index);
}

function syncRetrospectiveQuestionFilter(visible) {
  const box = $('#retrospective-question-filter');
  const select = $('#retrospective-question-filter-select');
  if (!box || !select) return;
  box.hidden = !visible || !isRetrospectiveReview();
  if (box.hidden) return;
  select.innerHTML = RETROSPECTIVE_QUESTION_FILTERS.map(([value, label]) => {
    const count = retrospectiveQuestionIndexes(value).length;
    return `<option value="${value}"${count ? '' : ' disabled'}>${label} (${count})</option>`;
  }).join('');
  select.value = retrospectiveQuestionFilter;
}

function syncRetrospectiveQuestionSort(visible) {
  const box = $('#retrospective-question-sort');
  const select = $('#retrospective-question-sort-select');
  if (!box || !select) return;
  box.hidden = !visible || !isArchiveRetrospective();
  if (box.hidden) return;
  select.value = retrospectiveSimilaritySort;
}

function syncQuestionNavigation() {
  const nav = $('#question-nav');
  if (!nav) return;
  const visible = !!cur && quizSource === 'weekly' && ITEMS.length > 0;
  nav.style.display = visible ? 'flex' : 'none';
  const quickStart = $('#quick-start-open');
  if (quickStart) quickStart.hidden = !visible;
  syncRetrospectiveQuestionFilter(visible);
  if (typeof syncRetrospectiveQuestionSort === 'function') {
    syncRetrospectiveQuestionSort(visible);
  }
  if (visible) syncWeeklyGuideContent();
  if (!visible) return;
  const filteredIndexes = retrospectiveQuestionIndexes();
  const filteredPosition = filteredIndexes.indexOf(idx);
  $('#question-prev').disabled = viewerTransitionBusy || revealRequested || filteredPosition <= 0;
  $('#question-next').disabled = viewerTransitionBusy || revealRequested
    || filteredPosition < 0 || filteredPosition >= filteredIndexes.length - 1;
}

function openWeeklyQuickStart(origin = 'manual') {
  if (quizSource !== 'weekly') return false;
  const dialog = $('#quick-start-dialog');
  if (!dialog || dialog.open) return false;
  try { dialog.showModal(); }
  catch (error) { dialog.setAttribute('open', ''); }
  recordAppEvent('quick_start_opened', { quick_start_origin: origin });
  return true;
}

async function navigateWeeklyQuestion(nextIndex, action = 'question_navigated') {
  if (quizSource !== 'weekly' || viewerTransitionBusy || revealRequested
      || nextIndex < 0 || nextIndex >= ITEMS.length || nextIndex === idx) return;
  recordAppEvent(action);
  rememberWeeklyItemState();
  await loadQuestion(nextIndex);
}

function adjacentRetrospectiveQuestionIndex(direction) {
  const filteredIndexes = retrospectiveQuestionIndexes();
  const position = filteredIndexes.indexOf(idx);
  return filteredIndexes[position + direction] ?? null;
}

async function setRetrospectiveQuestionFilter(filter) {
  if (!isRetrospectiveReview()
      || !RETROSPECTIVE_QUESTION_FILTERS.some(([value]) => value === filter)) return;
  const matchingIndexes = retrospectiveQuestionIndexes(filter);
  if (!matchingIndexes.length) return;
  retrospectiveQuestionFilter = filter;
  const target = matchingIndexes.includes(idx)
    ? idx
    : (matchingIndexes.find(index => index >= idx) ?? matchingIndexes[0]);
  if (target === idx) {
    renderUI();
    return;
  }
  await navigateWeeklyQuestion(target, 'question_filter_changed');
}

async function setRetrospectiveSimilaritySort(mode) {
  if (!isArchiveRetrospective()
      || !['default', 'novel-first', 'familiar-first'].includes(mode)) return;
  retrospectiveSimilaritySort = mode;
  const matchingIndexes = retrospectiveQuestionIndexes();
  if (!matchingIndexes.length) return;
  if (matchingIndexes.includes(idx)) {
    renderUI();
    return;
  }
  await navigateWeeklyQuestion(matchingIndexes[0], 'question_similarity_sort_changed');
}

function recordAppEvent(action, stateDetails = null) {
  const state = currentReplayableAppState();
  if (stateDetails && typeof stateDetails === 'object') Object.assign(state, stateDetails);
  try { viewerTraceRecorder?.recordAppEvent?.(action, state); }
  catch (error) { console.warn('App replay event omitted:', error.message); }
}

function performanceDiagnosticsConsented() {
  return PERFORMANCE_RECORDING_REQUESTED
    && $('#performance-consent-checkbox')?.checked === true;
}

function recordPerformanceDiagnostics(report) {
  if (!report || !remoteSessionId || !performanceDiagnosticsConsented()
      || !performanceDiagnosticsCollector) return;
  try {
    const payload = performanceDiagnosticsCollector.capture(report, {
      startupReports: viewerPerformance.reports,
      gl: plugin?.canvas3d?.webgl?.gl || plugin?.canvas3d?.webgl?.context?.gl || null,
    });
    const submission = researchBackend()?.submitWeeklyPerformanceReport?.({
      sessionId: remoteSessionId,
      roundId: WEEKLY_ROUND?.round_id,
      itemId: report.metadata?.itemId,
      questionIndex: report.metadata?.questionIndex,
      report: payload,
    });
    void submission?.catch(error => {
      console.warn('Performance diagnostics were not saved:', error.message);
    });
  } catch (error) {
    console.warn('Performance diagnostics omitted:', error.message);
  }
}

function saveWeeklyResumePosition(questionIndex = idx) {
  if (quizSource !== 'weekly' || !remoteSessionId || !WEEKLY_ROUND?.round_id) return;
  try {
    window.foldariumWeeklySessionResume?.save?.({
      sessionId: remoteSessionId,
      roundId: WEEKLY_ROUND.round_id,
      questionIndex,
      phase: WEEKLY_ROUND.public_status === 'revealed' ? 'post_reveal' : 'blind',
    });
  } catch (error) {
    console.warn('Weekly refresh position was not saved:', error.message);
  }
}

function reportWeeklyTraceWarning(message) {
  console.warn(message);
  const status = $('#verdict');
  if (!status || status.textContent === 'Recording…') return;
  status.style.display = '';
  status.dataset.state = 'warning';
  status.textContent = /remains queued/i.test(message)
    ? 'Interaction history is queued locally and will retry automatically.'
    : 'Part of the interaction history could not be saved; vote recording will keep a safety replay.';
}

function weeklyViewerInstruction() {
  const reset = displayMode === 'all'
    ? 'zoom out and restore all poses'
    : 'zoom out';
  return `Click a ligand to zoom in; click white space to ${reset}. `
    + 'Drag to rotate, right-drag or Ctrl-drag to pan, and scroll or pinch to zoom; '
    + 'use Select or a pose name to vote.';
}

function setVoteStatus(message, state) {
  const status = $('#verdict');
  status.style.display = '';
  status.dataset.state = state;
  status.textContent = message;
}

function startWeeklyThinkingTrace() {
  if (quizSource !== 'weekly' || !remoteSessionId || isRetrospectiveReview()
      || WEEKLY_ROUND?.public_status === 'revealed'
      || typeof window.createWeeklyTraceStream !== 'function') return;
  try {
    void weeklyTraceStream?.dispose?.();
    const backend = researchBackend();
    weeklyTraceStream = window.createWeeklyTraceStream({
      submitBatch: payload => backend.submitWeeklyTraceBatch(payload),
      getAppState: currentReplayableAppState,
      onWarning: reportWeeklyTraceWarning,
    });
    weeklyTraceStream.startSession({
      sessionId: remoteSessionId,
      roundId: WEEKLY_ROUND.round_id,
      ...(weeklyTraceSessionSeed || {}),
    });
    weeklyTraceSessionSeed = null;
  } catch (error) {
    weeklyTraceStream = null;
    console.warn('Continuous thinking trace disabled:', error.message);
  }
}

function activatePane(paneId, reason = 'interaction') {
  if (!paneId || paneId === activePaneId) return;
  activePaneId = paneId;
  try { viewerTraceRecorder?.setActivePane?.(paneId, reason); }
  catch (error) { console.warn('Grid pane attribution omitted:', error.message); }
}

function setViewerControlsBusy(busy) {
  viewerTransitionBusy = busy;
  document.querySelectorAll(
    '#choices button, #mode button, #protmode button, #uncluster, #hbonds, #surface, #protein-ensemble, #lock, '
    + '#next, #prev, #question-prev, #question-next, #myview, #showXtal, #start, #gridpages button, '
    + '.grid-review-actions button, #one-review-actions button',
  ).forEach(control => { control.disabled = busy; });
  if (!busy && viewingReleasedCrystal()) {
    document.querySelectorAll(
      '#mode button, #protmode button, #uncluster, #hbonds, #surface, #protein-ensemble',
    ).forEach(control => { control.disabled = true; });
  }
  if (!busy && cur && !cur.revealed) {
    $('#lock').disabled = revealRequested || cur.selected == null;
  }
  syncQuestionNavigation();
}

function structureRequestUrl(url) {
  const resolved = assetUrl(url);
  // Weekly Storage objects are content-addressed and immutable. A new random
  // query on every page load defeats both the browser and Supabase CDN caches.
  if (typeof url === 'string' && url.startsWith('supabase://')) return resolved;
  return resolved + (resolved.includes('?') ? '&' : '?') + 'v=' + CACHE_BUST;
}
async function loadStruct(url, format, targetPlugin = plugin, structureParams = undefined) {
  // Mol* otherwise uses the full signed/public Storage URL as the model label,
  // which leaks into its residue hover overlay.
  const requestUrl = structureRequestUrl(url);
  const prefetchedText = await structurePrefetcher.textWhenReady(requestUrl);
  const timing = viewerPerformance?.current();
  const data = prefetchedText === null
    ? await viewerPerformance.measure(timing, 'structure-download', () => (
        targetPlugin.builders.data.download({
          url: requestUrl,
          isBinary: false,
          label: 'Foldarium',
        })
      ))
    : await viewerPerformance.measure(timing, 'prefetched-data-load', () => (
        targetPlugin.builders.data.rawData({ data: prefetchedText, label: 'Foldarium' })
      ));
  const traj = await viewerPerformance.measure(timing, 'trajectory-parse', () => (
    targetPlugin.builders.structure.parseTrajectory(data, format)
  ));
  const model = await viewerPerformance.measure(timing, 'model-create', () => (
    targetPlugin.builders.structure.createModel(traj)
  ));
  const struct = await viewerPerformance.measure(timing, 'structure-create', () => (
    targetPlugin.builders.structure.createStructure(model, structureParams)
  ));
  return { data, struct };
}
async function loadStructText(text, format, targetPlugin = plugin) {
  const timing = viewerPerformance?.current();
  const data = await viewerPerformance.measure(timing, 'inline-data-load', () => (
    targetPlugin.builders.data.rawData({ data: text, label: 'Foldarium answer' })
  ));
  const traj = await viewerPerformance.measure(timing, 'trajectory-parse', () => (
    targetPlugin.builders.structure.parseTrajectory(data, format)
  ));
  const model = await viewerPerformance.measure(timing, 'model-create', () => (
    targetPlugin.builders.structure.createModel(traj)
  ));
  const struct = await viewerPerformance.measure(timing, 'structure-create', () => (
    targetPlugin.builders.structure.createStructure(model)
  ));
  return { data, struct };
}
async function fetchPdbText(url) {   // raw PDB text (for merging pocket+pose into ONE structure for interactions)
  const requestUrl = structureRequestUrl(url);
  const prefetchedText = await structurePrefetcher.textWhenReady(requestUrl);
  if (prefetchedText !== null) return prefetchedText;
  const r = await viewerPerformance.measure(
    viewerPerformance.current(),
    'structure-text-download',
    () => fetch(requestUrl),
  );
  return r.ok ? await r.text() : '';
}
function pdbCoordinateRecords(text) {
  return String(text || '').split(/\r?\n/).filter(line => /^(ATOM  |HETATM)/.test(line)).map(line => ({
    atom: `${line.slice(12, 16)}|${line.slice(76, 78)}`,
    position: [
      Number.parseFloat(line.slice(30, 38)),
      Number.parseFloat(line.slice(38, 46)),
      Number.parseFloat(line.slice(46, 54)),
    ],
  })).filter(record => record.position.every(Number.isFinite));
}
function rigidPdbTransform(sourcePdb, targetPdb) {
  const source = pdbCoordinateRecords(sourcePdb);
  const target = pdbCoordinateRecords(targetPdb);
  if (source.length < 3 || source.length !== target.length
      || source.some((record, index) => record.atom !== target[index].atom)) {
    throw new Error('Aligned pose atom correspondence is unavailable');
  }
  const subtract = (left, right) => left.map((value, index) => value - right[index]);
  const dot = (left, right) => left.reduce((sum, value, index) => sum + value * right[index], 0);
  const scale = (vector, factor) => vector.map(value => value * factor);
  const norm = vector => Math.sqrt(dot(vector, vector));
  const normalize = vector => {
    const length = norm(vector);
    if (!(length > 1e-6)) throw new Error('Aligned pose does not define a stable rigid frame');
    return scale(vector, 1 / length);
  };
  const cross = (left, right) => [
    left[1] * right[2] - left[2] * right[1],
    left[2] * right[0] - left[0] * right[2],
    left[0] * right[1] - left[1] * right[0],
  ];
  let first = 0, second = 1, farthest = -1;
  for (let i = 0; i < source.length; i++) {
    for (let j = i + 1; j < source.length; j++) {
      const distance = dot(subtract(source[j].position, source[i].position),
        subtract(source[j].position, source[i].position));
      if (distance > farthest) { first = i; second = j; farthest = distance; }
    }
  }
  const sourceAxis1 = normalize(subtract(source[second].position, source[first].position));
  let third = -1, widest = -1;
  for (let i = 0; i < source.length; i++) {
    const offset = subtract(source[i].position, source[first].position);
    const perpendicular = subtract(offset, scale(sourceAxis1, dot(offset, sourceAxis1)));
    const width = dot(perpendicular, perpendicular);
    if (width > widest) { third = i; widest = width; }
  }
  if (third < 0 || widest < 1e-6) throw new Error('Aligned pose atoms are collinear');
  const orthonormalBasis = (records) => {
    const axis1 = normalize(subtract(records[second].position, records[first].position));
    const offset = subtract(records[third].position, records[first].position);
    const axis2 = normalize(subtract(offset, scale(axis1, dot(offset, axis1))));
    return [axis1, axis2, normalize(cross(axis1, axis2))];
  };
  const sourceBasis = orthonormalBasis(source);
  const targetBasis = orthonormalBasis(target);
  const rotation = Array.from({ length: 3 }, (_, row) => Array.from({ length: 3 }, (_, column) => (
    targetBasis.reduce((sum, axis, index) => sum + axis[row] * sourceBasis[index][column], 0)
  )));
  const applyRotation = position => rotation.map(row => dot(row, position));
  const centroid = records => records.reduce(
    (sum, record) => sum.map((value, index) => value + record.position[index]),
    [0, 0, 0],
  ).map(value => value / records.length);
  const sourceCenter = centroid(source), targetCenter = centroid(target);
  const translation = subtract(targetCenter, applyRotation(sourceCenter));
  const apply = position => applyRotation(position).map(
    (value, index) => value + translation[index],
  );
  const residuals = source.map((record, index) => norm(
    subtract(apply(record.position), target[index].position),
  ));
  const rmsd = Math.sqrt(residuals.reduce((sum, value) => sum + value * value, 0) / residuals.length);
  if (!Number.isFinite(rmsd) || rmsd > 0.03 || Math.max(...residuals) > 0.08) {
    throw new Error('Could not recover the evaluator alignment from this pose');
  }
  return { rotation, translation, apply, rmsd };
}
function transformPdbCoordinates(text, transform) {
  const coordinate = value => {
    const formatted = value.toFixed(3);
    if (formatted.length > 8) throw new Error('Aligned coordinate exceeds PDB field width');
    return formatted.padStart(8);
  };
  return String(text || '').split(/\r?\n/).map(line => {
    if (!/^(ATOM  |HETATM)/.test(line)) return line;
    const position = [
      Number.parseFloat(line.slice(30, 38)),
      Number.parseFloat(line.slice(38, 46)),
      Number.parseFloat(line.slice(46, 54)),
    ];
    if (!position.every(Number.isFinite)) return line;
    const aligned = transform.apply(position);
    return `${line.slice(0, 30)}${aligned.map(coordinate).join('')}${line.slice(54)}`;
  }).join('\n');
}
function extractAlignedPocketPdb(proteinPdb, ligandPdb, radiusAngstrom = 8) {
  const anchors = pdbCoordinateRecords(ligandPdb).map(record => record.position);
  if (!anchors.length) throw new Error('Crystal ligand coordinates are unavailable');
  const radiusSq = radiusAngstrom * radiusAngstrom;
  const records = String(proteinPdb || '').split(/\r?\n/).filter(
    line => line.startsWith('ATOM  '),
  ).map(line => {
    const atomName = line.slice(12, 16).trim();
    const element = (line.slice(76, 78).trim() || atomName.replace(/^[0-9]+/, '').slice(0, 1))
      .toUpperCase();
    return {
      line,
      chain: line[21] || ' ',
      residue: `${line[21] || ' '}|${line.slice(22, 27)}`,
      heavy: element !== 'H' && element !== 'D',
      position: [
        Number.parseFloat(line.slice(30, 38)),
        Number.parseFloat(line.slice(38, 46)),
        Number.parseFloat(line.slice(46, 54)),
      ],
    };
  }).filter(record => record.heavy && record.position.every(Number.isFinite));
  const selectedResidues = new Set();
  for (const record of records) {
    if (anchors.some(anchor => record.position.reduce(
      (sum, value, index) => sum + (value - anchor[index]) ** 2,
      0,
    ) <= radiusSq)) selectedResidues.add(record.residue);
  }
  const selected = records.filter(record => selectedResidues.has(record.residue));
  if (!selected.length) throw new Error('Aligned folded pocket is empty');
  const sourceChains = [...new Set(selected.map(record => record.chain))].sort();
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  if (sourceChains.length > alphabet.length) throw new Error('Aligned folded pocket has too many chains');
  const chainMap = new Map(sourceChains.map((chain, index) => [chain, alphabet[index]]));
  return selected.map((record, index) => (
    `ATOM  ${String(index + 1).padStart(5)}${record.line.slice(11, 21)}`
    + `${chainMap.get(record.chain)}${record.line.slice(22)}`
  )).join('\n') + '\nEND\n';
}
const alignedFoldedAssetCache = new Map();
async function alignedFoldedAssets(choice, urls) {
  const key = retrospectiveChoiceKey(choice);
  if (!key) throw new Error('Folded pose identity is missing');
  if (!alignedFoldedAssetCache.has(key)) {
    const pending = (async () => {
      const [sourcePosePdb, proteinPdb, pocketPdb] = await Promise.all([
        fetchPdbText(choice.pose_file),
        fetchPdbText(urls.prot),
        urls.pocket ? fetchPdbText(urls.pocket) : Promise.resolve(''),
      ]);
      if (!sourcePosePdb || !proteinPdb || !choice.answer_overlay_pdb) {
        throw new Error('Folded alignment assets are incomplete');
      }
      const transform = rigidPdbTransform(sourcePosePdb, choice.answer_overlay_pdb);
      const alignedProteinPdb = transformPdbCoordinates(proteinPdb, transform);
      return {
        proteinPdb: alignedProteinPdb,
        pocketPdb: pocketPdb ? transformPdbCoordinates(pocketPdb, transform) : '',
        transformRmsd: transform.rmsd,
      };
    })().catch(error => {
      alignedFoldedAssetCache.delete(key);
      throw error;
    });
    alignedFoldedAssetCache.set(key, pending);
  }
  return alignedFoldedAssetCache.get(key);
}
function relabelPdbRecords(text, options = {}) {
  const {
    chain = 'X',
    residueName = 'LIG',
    residueNumber = 1,
    startSerial = 1,
    recordTypes = /^(ATOM|HETATM)/,
  } = options;
  let serial = startSerial;
  const lines = [];
  for (const line of String(text || '').split('\n')) {
    if (!recordTypes.test(line)) continue;
    const record = line.startsWith('HETATM') ? 'HETATM' : 'ATOM  ';
    const atomName = line.slice(12, 16);
    const element = (line.slice(76, 78).trim() || atomName.trim().slice(0, 2) || 'C').padStart(2);
    const coords = line.slice(30, 54);
    const occB = line.slice(54, 66).padEnd(12).slice(0, 12);
    const res = String(residueName || 'LIG').replace(/[^A-Za-z0-9]/g, '').padEnd(3, ' ').slice(0, 3);
    const chainId = String(chain || 'X').slice(0, 1);
    const resNum = String(residueNumber ?? 1).padStart(4);
    lines.push(`${record}${String(serial).padStart(5)} ${atomName} ${res} ${chainId}${resNum}    ${coords}${occB}          ${element}`);
    serial += 1;
  }
  return { text: lines.join('\n'), nextSerial: serial };
}
function maxPdbSerial(text) {
  let maxSerial = 0;
  for (const line of String(text || '').split('\n')) {
    if (!/^(ATOM|HETATM)/.test(line)) continue;
    const serial = Number.parseInt(line.slice(6, 11), 10);
    if (Number.isFinite(serial)) maxSerial = Math.max(maxSerial, serial);
  }
  return maxSerial;
}
function mergeRetrospectiveInteractionPdb({
  pocketPdb,
  ligandPdb,
  chain = 'P',
  residueName = 'PRD',
}) {
  const pocketRecords = atomRecords(String(pocketPdb || ''));
  const parts = [pocketRecords];
  if (typeof ligandPdb === 'string' && ligandPdb) {
    parts.push(relabelPdbRecords(ligandPdb, {
      chain,
      residueName,
      residueNumber: 1,
      startSerial: maxPdbSerial(pocketRecords) + 1,
    }).text);
  }
  return parts.filter(Boolean).join('\nTER\n') + '\nEND\n';
}
const structurePrefetcher = window.foldariumStructurePrefetch.createStructurePrefetcher();
function buildQuestionClusters(item) {
  const byCluster = {};
  item.choices.forEach(choice => (byCluster[choice.cluster] ??= []).push({ ...choice }));
  return shuffle(Object.values(byCluster)).map((members, index) => {
    const color = PALETTE[index % PALETTE.length];
    const label = LABELS[index % LABELS.length];
    decorateClusterMembers(members, label, item.source);
    members.forEach(member => {
      member.color = color;
    });
    return { label, color, members, rep: members.find(member => member.is_rep) || members[0] };
  });
}
async function prefetchWeeklyItemAssets(item, clusters, {
  questionIndex,
  page = 0,
  mode = userView.displayMode,
  isClustered = userView.clustered,
  proteinEnsemble = userView.showProteinEnsemble,
  stage = 'next-question-prefetch',
  priority = 0,
} = {}) {
  let initialChoice = item.choices?.[0];
  let paths;
  if (mode === 'grid') {
    paths = window.foldariumStructurePrefetch.gridQuestionAssetPaths(item, clusters, {
        page,
        pageSize: GRID_PAGE_SIZE,
        clustered: isClustered,
        showProteinEnsemble: proteinEnsemble,
    });
  } else {
    initialChoice = isClustered ? clusters[0]?.rep : clusters[0]?.members?.[0];
  }
  paths ||= window.foldariumStructurePrefetch.initialQuestionAssetPaths(item, initialChoice);
  await viewerPerformance.measureStartup(
    stage,
    () => structurePrefetcher.prefetch(paths.map(structureRequestUrl), { priority }),
    {
      questionIndex,
      itemId: item.id || null,
      mode,
      assetCount: paths.length,
    },
  );
}

async function prefetchQuestionAssets(questionIndex, { priority = 0 } = {}) {
  const item = ITEMS[questionIndex];
  if (!item) return;
  if (item.source !== 'weekly') {
    const paths = window.foldariumStructurePrefetch.initialQuestionAssetPaths(item);
    await structurePrefetcher.prefetch(paths.map(structureRequestUrl), { priority });
    return;
  }
  const savedState = WEEKLY_ITEM_STATES.get(item.id);
  const clusters = savedState?.clusters
    || WEEKLY_PREFETCHED_CLUSTERS.get(item.id)
    || buildQuestionClusters(item);
  WEEKLY_PREFETCHED_CLUSTERS.set(item.id, clusters);
  await prefetchWeeklyItemAssets(item, clusters, {
    questionIndex,
    page: savedState?.savedGridPage || 0,
    priority,
  });
}

function startPendingQuestionPrefetch() {
  const questionIndexes = pendingQuestionPrefetchIndexes;
  pendingQuestionPrefetchIndexes = [];
  questionIndexes.forEach((questionIndex, distance) => {
    if (questionIndex >= ITEMS.length) return;
    void prefetchQuestionAssets(questionIndex, {
      priority: QUESTION_PREFETCH_LOOKAHEAD - distance,
    });
  });
}

async function prepareFirstWeeklyQuestionAssets() {
  if (quizSource !== 'weekly' || !POOLS.weekly.length || isRetrospectiveReview()) return;
  const items = drawSession();
  const item = items[0];
  if (!item) return;
  const clusters = buildQuestionClusters(item);
  const prefetchedClusters = new Map([[item.id, clusters]]);
  for (const futureItem of items.slice(1, QUESTION_PREFETCH_LOOKAHEAD + 1)) {
    prefetchedClusters.set(futureItem.id, buildQuestionClusters(futureItem));
  }
  WEEKLY_PREPARED_SESSION = {
    items,
    prefetchedClusters,
  };
  await prefetchWeeklyItemAssets(item, clusters, {
    questionIndex: 0,
    page: 0,
    mode: userView.displayMode,
    isClustered: userView.clustered,
    proteinEnsemble: userView.showProteinEnsemble,
    stage: 'first-question-prefetch',
    priority: QUESTION_PREFETCH_LOOKAHEAD + 1,
  });
  items.slice(1, QUESTION_PREFETCH_LOOKAHEAD + 1).forEach((futureItem, distance) => {
    void prefetchWeeklyItemAssets(futureItem, prefetchedClusters.get(futureItem.id), {
      questionIndex: distance + 1,
      page: 0,
      mode: userView.displayMode,
      isClustered: userView.clustered,
      proteinEnsemble: userView.showProteinEnsemble,
      stage: 'setup-lookahead-prefetch',
      priority: QUESTION_PREFETCH_LOOKAHEAD - distance,
    });
  });
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
async function addPose(struct, carbon, targetPlugin = plugin, {
  alpha = 1,
  sizeFactor = 0.24,
} = {}) {
  let comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'ligand');
  if (!comp) comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return targetPlugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor, alpha },
    color: 'element-symbol', colorParams: { carbonColor: { name: 'uniform', params: { value: carbon } } },
  });
}
const addCrystalPose = (struct, targetPlugin = plugin) => addPose(
  struct,
  XTAL,
  targetPlugin,
  { sizeFactor: XTAL_POSE_SIZE },
);
async function addSticks(struct, sizeFactor, alpha, targetPlugin = plugin) {
  const comp = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'all');
  if (!comp) return null;
  return targetPlugin.builders.structure.representation.addRepresentation(comp, {
    type: 'ball-and-stick', typeParams: { sizeFactor, alpha }, color: 'element-symbol',
  });
}
async function addRetrospectiveCrystalPocketSticks(
  choice,
  targetPlugin = plugin,
  onData = null,
) {
  const pocketPdb = choice?.answer_crystal_pocket_pdb;
  if (typeof pocketPdb !== 'string' || !pocketPdb) {
    throw new Error('Crystal pocket artifact is missing');
  }
  const displayPocketPdb = extractAlignedPocketPdb(
    pocketPdb,
    choice.answer_crystal_pdb,
    5,
  );
  const pocket = await loadStructText(displayPocketPdb, 'pdb', targetPlugin);
  onData?.(pocket.data);
  await addSticks(pocket.struct, 0.16, 0.95, targetPlugin);
  return pocket;
}
async function addReleasedCrystalLigand(struct, componentId, targetPlugin = plugin) {
  const repParams = {
    type: 'ball-and-stick',
    typeParams: { sizeFactor: XTAL_POSE_SIZE, alpha: 1 },
    color: 'element-symbol',
    colorParams: { carbonColor: { name: 'uniform', params: { value: XTAL } } },
  };
  const MS = globalThis.molstar?.MolScriptBuilder;
  if (MS && componentId) {
    try {
      const expression = MS.struct.generator.atomGroups({
        'residue-test': MS.core.rel.eq([
          MS.struct.atomProperty.macromolecular.label_comp_id(),
          componentId,
        ]),
      });
      const comp = await targetPlugin.builders.structure.tryCreateComponentFromExpression(
        struct, expression, `released-ligand-${componentId}`, { label: componentId },
      );
      if (comp) {
        return targetPlugin.builders.structure.representation.addRepresentation(comp, repParams);
      }
    } catch (error) {
      console.warn('Released-crystal ligand selection failed; falling back to all ligands:', error.message);
    }
  }
  return addPose(struct, XTAL, targetPlugin);
}
async function addClosestTrainingLigand(struct, componentId, targetPlugin = plugin) {
  if (!componentId) throw new Error('Exact training ligand identifier is unavailable');
  // Published training overlays contain all source polymer chains and exactly
  // one non-polymer residue: the scored ligand identified by componentId.
  // Use Mol*'s viewer-supported static selector instead of MolScriptBuilder,
  // which is not exported by the CDN viewer bundle.
  const component = await targetPlugin.builders.structure.tryCreateComponentStatic(
    struct,
    'ligand',
    { label: componentId },
  );
  if (!component) throw new Error(`Training ligand ${componentId} was not found`);
  return targetPlugin.builders.structure.representation.addRepresentation(component, {
    type: 'ball-and-stick',
    typeParams: { sizeFactor: 0.28, alpha: 1 },
    color: 'element-symbol',
    colorParams: {
      carbonColor: { name: 'uniform', params: { value: TRAINING_LIGAND } },
    },
  });
}
async function addTrainingReferencePose(
  choice,
  targetPlugin = plugin,
  onData = null,
  { surface = false } = {},
) {
  if (!isTrainingReferenceChoice(choice) || !choice.pose_file) return null;
  let loaded = null;
  try {
    // The published artifact is already in the released target frame. Its
    // polymer is deliberately not represented: this is one reference pose,
    // not a second protein overlay.
    loaded = await loadStruct(choice.pose_file, 'pdb', targetPlugin);
    const representation = await addClosestTrainingLigand(
      loaded.struct,
      choice.training_ligand,
      targetPlugin,
    );
    let surfaceRepresentation = null;
    if (surface) {
      surfaceRepresentation = await addRep(
        loaded.struct,
        'ligand',
        'molecular-surface',
        TRAINING_LIGAND,
        0.7,
        targetPlugin,
      );
    }
    registerPoseClickTarget(representation, choice);
    registerPoseClickTarget(surfaceRepresentation, choice);
    onData?.(loaded.data);
    return { ...loaded, representation };
  } catch (error) {
    if (loaded?.data) {
      try {
        const update = targetPlugin.build();
        update.delete(loaded.data.ref || loaded.data);
        await update.commit();
      } catch (_) {}
    }
    console.warn('Closest training pose omitted:', error.message);
    return null;
  }
}
async function clearViewerScene() {
  if (!proteinData.length && !layerData.length && !hbondData.length) return;
  const b = plugin.build();
  for (const x of proteinData) b.delete(x.ref || x);
  for (const d of layerData) b.delete(d.ref || d);
  for (const d of hbondData) b.delete(d.ref || d);
  await b.commit();
  proteinData = []; layerData = []; hbondData = [];
  currentProteinKey = null;
}
async function addRetrospectiveCrystalContext(
  targetPlugin = plugin,
  onData = null,
  crystalPdbs = [cur.item.answer_overlay?.crystal_ligand_pdb],
  onCrystalRepresentation = null,
) {
  const released = cur.item.released_crystal;
  const loaded = await loadStruct(
    released.cif_url,
    'mmcif',
    targetPlugin,
    { name: 'model', params: {} },
  );
  onData?.(loaded.data, 'protein');
  await addRep(loaded.struct, 'polymer', 'cartoon', PROT, 0.5, targetPlugin);
  if (showSurface) {
    await addRep(loaded.struct, 'polymer', 'molecular-surface', PROT, 0.7, targetPlugin);
  }
  const uniqueCrystals = [...new Set(crystalPdbs)];
  if (!uniqueCrystals.length || uniqueCrystals.some(pdb => typeof pdb !== 'string' || !pdb)) {
    throw new Error('Retrospective crystal ligand overlay is missing');
  }
  for (const crystalPdb of uniqueCrystals) {
    const crystal = await loadStructText(crystalPdb, 'pdb', targetPlugin);
    onData?.(crystal.data, 'layer');
    const representation = await addCrystalPose(crystal.struct, targetPlugin);
    onCrystalRepresentation?.(representation, crystalPdb);
    if (showSurface) {
      await addRep(crystal.struct, 'all', 'molecular-surface', XTAL, 0.7, targetPlugin);
    }
  }
}
async function buildReleasedCrystalScene(preserveCamera = true) {
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await clearViewerScene();
    const released = cur.item.released_crystal;
    if (isPrivatePrecloseReview()) {
      await addRetrospectiveCrystalContext(plugin, (data, kind) => {
        (kind === 'protein' ? proteinData : layerData).push(data);
      });
    } else {
      const loaded = await loadStruct(released.cif_url, 'mmcif');
      proteinData.push(loaded.data);
      await addRep(loaded.struct, 'polymer', 'cartoon', PROT, 0.5);
      await addReleasedCrystalLigand(loaded.struct, released.ligand_component_id);
    }
    if (!preserveCamera) plugin.canvas3d?.requestCameraReset?.();
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
function shuffle(a) { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
function decorateClusterMembers(members, label, source) {
  const clusterAccepted = source === 'weekly'
    && members.some(member => (
      member.accepted_correct === true || member.correct === true
    ));
  members.forEach((member, index) => {
    member.label = label + (members.length > 1 ? '-' + (index + 1) : '');
    member.clusterAccepted = source === 'weekly' ? clusterAccepted : member.correct === true;
  });
}

// ---- visible-choice logic --------------------------------------------------------------------
function visibleChoices() {
  return clustered ? cur.clusters.map(c => c.rep) : cur.clusters.flatMap(c => c.members);
}

function clusterForChoice(choice) {
  return cur?.clusters?.find(cluster => cluster.members.includes(choice)) || null;
}

function reviewChoiceIds(choice) {
  const cluster = clusterForChoice(choice);
  if (DEV2_FEEDBACK.reviewChoiceIds) {
    return DEV2_FEEDBACK.reviewChoiceIds(choice, cluster, clustered);
  }
  const members = clustered ? (cluster?.members || [choice]) : [choice];
  return members.map(member => String(member?._weeklyChoiceId || member?.pose_file || member?.label || ''))
    .filter(Boolean);
}

function choiceRejected(choice) {
  if (!cur?.rejectedChoiceIds) return false;
  const cluster = clusterForChoice(choice);
  if (DEV2_FEEDBACK.rejectedState) {
    return DEV2_FEEDBACK.rejectedState(cur.rejectedChoiceIds, choice, cluster, clustered);
  }
  const ids = reviewChoiceIds(choice);
  return ids.length > 0 && ids.every(id => cur.rejectedChoiceIds.has(id));
}

function syncReviewState() {
  for (const cell of gridViewers) {
    cell.card?.classList.toggle('rejected', choiceRejected(cell.entry.choice));
    cell.card?.classList.toggle('inspecting', sameChoice(cur?.contextChoice, cell.entry.choice));
    const reject = cell.card?.querySelector('[data-review="reject"]');
    if (reject) {
      const rejected = choiceRejected(cell.entry.choice);
      reject.classList.toggle('on', rejected);
      reject.textContent = rejected ? 'Undo reject' : 'Reject';
      reject.setAttribute('aria-pressed', String(rejected));
    }
    const select = cell.card?.querySelector('[data-review="select"]');
    if (select) {
      const selected = gridChoiceSelected(cell.entry.choice);
      select.classList.toggle('on', selected);
      select.textContent = selected ? 'Selected ✓' : 'Select';
      select.setAttribute('aria-pressed', String(selected));
    }
  }
  syncOneReviewState();
  syncGridSelection();
}

function oneReviewChoice() {
  if (!cur || displayMode !== 'one') return null;
  const choices = retrospectiveNavChoices();
  return choices[Math.min(shownOne, Math.max(0, choices.length - 1))] || null;
}

const retrospectiveChoiceKey = choice => {
  const choiceId = choice?._weeklyChoiceId || choice?.id || choice?.label || '';
  return choiceId ? `${cur?.item?.id || ''}|${choiceId}` : '';
};

async function setRetrospectiveProteinFrame(frame) {
  if (!retrospectiveAnswerActive() || !['xtal', 'folded'].includes(frame)
      || frame === retrospectiveProteinFrame || viewerControlBlocked()) return;
  const choice = oneReviewChoice();
  if (frame === 'folded' && choice && isFixedReferenceChoice(choice)) return;
  const cameraSnapshot = plugin.canvas3d?.camera?.getSnapshot?.() || null;
  await viewerRebuild.enqueue(() => {
    retrospectiveProteinFrame = frame;
    currentProteinKey = null;
    nextCanonicalCameraSnapshot = cameraSnapshot;
    resetCameraOnNextBuild = !cameraSnapshot;
    syncButtons();
  }, async () => {
    plugin.canvas3d?.requestDraw?.();
    await nextAnimationFrame();
    plugin.canvas3d?.requestDraw?.();
    renderUI();
    syncButtons();
  });
}

async function setRetrospectiveGridProteinFrame(choice, frame) {
  const key = retrospectiveChoiceKey(choice);
  if (!retrospectiveAnswerActive() || displayMode !== 'grid' || !key
      || isFixedReferenceChoice(choice) || !['xtal', 'folded'].includes(frame)
      || (retrospectiveGridProteinFrames.get(key) || 'xtal') === frame
      || viewerControlBlocked()) return;
  const cell = gridViewers.find(candidate => sameChoice(candidate.entry.choice, choice));
  if (!cell?.plugin || cell.disposed || cell.failed) return;
  const previousFrame = cell.spec.retrospectiveProteinFrame || 'xtal';
  const previousCamera = cell.plugin.canvas3d?.camera?.getSnapshot?.() || null;
  const revision = gridBuildRevision;
  const sceneRevision = (cell.sceneRevision || 0) + 1;
  const releaseCamera = holdCameraSnapshot(cell.plugin, previousCamera);
  cell.sceneRevision = sceneRevision;
  viewerTransitionBusy = true;
  setViewerControlsBusy(true);
  if (stopGridCameraSync) { stopGridCameraSync(); stopGridCameraSync = null; }
  retrospectiveGridProteinFrames.set(key, frame);
  cell.spec.retrospectiveProteinFrame = frame;
  cell.card.classList.add('loading-frame');
  syncGridFrameControls(cell);
  try {
    try { cell.poseClickSubscription?.unsubscribe?.(); } catch (_) {}
    cell.poseClickSubscription = null;
    await cell.plugin.clear();
    if (cell.disposed || revision !== gridBuildRevision || sceneRevision !== cell.sceneRevision) return;
    cell.poseSphere = null;
    cell.hbondStatus = '';
    await populateGridCell(cell, revision, { preserveCamera: previousCamera });
  } catch (error) {
    if (!cell.disposed && revision === gridBuildRevision && sceneRevision === cell.sceneRevision) {
      console.warn('Could not switch this Grid protein frame:', error.message);
      retrospectiveGridProteinFrames.set(key, previousFrame);
      cell.spec.retrospectiveProteinFrame = previousFrame;
      syncGridFrameControls(cell);
      try {
        await cell.plugin.clear();
        await populateGridCell(cell, revision, { preserveCamera: previousCamera });
      } catch (restoreError) {
        console.warn('Could not restore this Grid card:', restoreError.message);
      }
    }
  } finally {
    if (!cell.disposed && revision === gridBuildRevision && sceneRevision === cell.sceneRevision) {
      cell.card.classList.remove('loading-frame');
      refreshGridCameraSync();
    }
    viewerTransitionBusy = false;
    setViewerControlsBusy(false);
    releaseCamera();
  }
}

function syncGridFrameControls(cell) {
  const frame = cell.spec.retrospectiveProteinFrame || 'xtal';
  cell.card.querySelectorAll('[data-frame]').forEach(button => {
    const on = button.dataset.frame === frame;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
  });
}

function refreshGridCameraSync() {
  if (stopGridCameraSync) { stopGridCameraSync(); stopGridCameraSync = null; }
  const active = gridViewers.filter(cell => !cell.disposed && cell.plugin?.canvas3d);
  if (active.length) stopGridCameraSync = syncGridCameras(active);
}

function syncOneReviewState() {
  const actions = $('#one-review-actions');
  if (!actions) return;
  const choice = oneReviewChoice();
  const retrospective = !!choice && retrospectiveAnswerActive();
  const visible = !!choice && cur.item.source === 'weekly'
    && (!cur.revealed || retrospective);
  const rejected = visible && choiceRejected(choice);
  // Match Grid's whole-card rejection treatment in One-at-a-time. Applying
  // the shared class to the viewer shell mutes every molecular layer together
  // without changing Mol* state, camera, or the review controls above it.
  $('#app')?.classList.toggle('rejected', rejected);
  actions.hidden = !visible;
  if (!visible) return;
  const select = $('#one-select');
  const reject = $('#one-reject');
  if (retrospective) {
    $('#app')?.classList.remove('rejected');
    const effectiveFrame = isFixedReferenceChoice(choice) ? 'xtal' : retrospectiveProteinFrame;
    select.textContent = 'Xtal';
    select.classList.toggle('on', effectiveFrame === 'xtal');
    select.setAttribute('aria-pressed', String(effectiveFrame === 'xtal'));
    reject.classList.remove('reject');
    reject.textContent = 'Folded';
    reject.classList.toggle('on', effectiveFrame === 'folded');
    reject.setAttribute('aria-pressed', String(effectiveFrame === 'folded'));
    reject.disabled = isFixedReferenceChoice(choice);
    return;
  }
  const selected = gridChoiceSelected(choice);
  select.classList.toggle('on', selected);
  select.textContent = selected ? 'Selected ✓' : 'Select';
  select.setAttribute('aria-pressed', String(selected));
  reject.classList.add('reject');
  reject.disabled = false;
  reject.classList.toggle('on', rejected);
  reject.textContent = rejected ? 'Undo reject' : 'Reject';
  reject.setAttribute('aria-pressed', String(rejected));
}

async function toggleChoiceRejected(choice) {
  if (!cur || cur.revealed) return;
  const update = () => {
    invalidatePendingWeeklyVote();
    const ids = reviewChoiceIds(choice);
    const rejecting = !choiceRejected(choice);
    ids.forEach(id => rejecting ? cur.rejectedChoiceIds.add(id) : cur.rejectedChoiceIds.delete(id));
    if (rejecting && cur.selected && !cur.selected.none
        && reviewChoiceIds(cur.selected).some(id => ids.includes(id))) {
      cur.selected = null;
      cur.selectionExact = false;
      cur.selectedAsCluster = false;
      $('#lock').disabled = true;
    }
    recordAppEvent(rejecting ? 'choice_rejected' : 'choice_rejection_undone');
  };
  const finish = () => { renderUI(); syncReviewState(); };
  if (cur.item.source === 'weekly' && displayMode !== 'grid' && viewerRebuild) {
    await viewerRebuild.enqueue(update, finish);
  } else {
    update(); finish();
  }
}

function inspectGridChoice(entry, paneId, reason = 'inspect') {
  const retrospectiveInspection = retrospectiveAnswerActive() && displayMode === 'grid';
  if (!cur || (retrospectiveInspection ? viewerControlBlocked() : interactionBlocked())) return;
  cur.contextChoice = entry.choice;
  cur.poseFocusChoice = entry.choice;
  selectedPaneId = paneId;
  activatePane(paneId, reason);
  syncReviewState();
  recordAppEvent('pose_inspected');
}

function inspectCanonicalChoice(choice) {
  const retrospectiveInspection = retrospectiveAnswerActive() && displayMode === 'one';
  if (!cur || (retrospectiveInspection ? viewerControlBlocked() : interactionBlocked())) return;
  const index = retrospectiveInspection && isFixedReferenceChoice(choice)
    ? retrospectiveNavChoices().findIndex(candidate => sameChoice(candidate, choice))
    : visibleIndexForChoice(choice);
  if (index < 0) return;
  shownOne = index;
  cur.contextChoice = choice;
  cur.poseFocusChoice = choice;
  selectedPaneId = null;
  recordAppEvent('pose_inspected');
}

function poseFocusBeforeClusterToggle() {
  if (!cur || displayMode === 'grid') return null;
  const choices = visibleChoices();
  const displayed = displayMode === 'all'
    ? cur.contextChoice
    : choices[Math.min(shownOne, Math.max(0, choices.length - 1))];
  if (!displayed) return null;
  // When a raw member was folded into its cluster, retain it behind the
  // representative so a later Uncluster returns to that exact member.
  const remembered = cur.poseFocusChoice;
  const rememberedCluster = remembered && clusterForChoice(remembered);
  return clustered && rememberedCluster && sameChoice(rememberedCluster.rep, displayed)
    ? remembered
    : displayed;
}

function restorePoseFocusAfterClusterToggle(exactChoice) {
  cur.poseFocusChoice = exactChoice || null;
  const choices = visibleChoices();
  if (!exactChoice || !choices.length) { shownOne = 0; return; }
  const target = clustered ? (clusterForChoice(exactChoice)?.rep || exactChoice) : exactChoice;
  const targetIndex = choices.findIndex(choice => sameChoice(choice, target));
  shownOne = targetIndex >= 0 ? targetIndex : 0;
  if (displayMode === 'all' && cur.contextChoice) cur.contextChoice = target;
}

// A clustered Weekly choice remains one vote (the medoid/representative raw
// choice ID), but the geometry shows every member: faint members first and the
// representative last so its colour and silhouette stay visually dominant.
function weeklyPoseLayers(choices) {
  if (cur?.item?.source !== 'weekly') {
    return choices.map(choice => ({ choice, ghost: false }));
  }
  if (retrospectiveAnswerActive()) {
    const focused = displayMode === 'all' ? cur.contextChoice : null;
    if (displayMode === 'all') {
      const visibleFocus = clustered && focused && !isFixedReferenceChoice(focused)
        ? (clusterForChoice(focused)?.rep || focused)
        : focused;
      return choices.map(choice => ({
        choice,
        ghost: !!visibleFocus && !sameChoice(choice, visibleFocus),
      }));
    }
    if (!clustered) return choices.map(choice => ({
      choice,
      ghost: false,
    }));
    return choices.flatMap(choice => {
      const cluster = clusterForChoice(choice);
      const members = !cluster || cluster.members.length < 2
        ? [choice]
        : [...cluster.members.filter(member => !sameChoice(member, choice)), choice];
      return members.map(member => ({
        choice: member,
        ghost: focused
          ? !sameChoice(member, focused)
          : !sameChoice(member, choice),
      }));
    });
  }
  if (cur.revealed && cur.showAnswer) {
    return choices.map(choice => ({ choice, ghost: false }));
  }
  if (displayMode === 'all') {
    const focused = cur.contextChoice;
    return choices.map(choice => {
      const rejected = choiceRejected(choice);
      return {
        choice,
        ...(rejected ? { rejected: true } : {}),
        ghost: rejected || !!focused && !sameChoice(choice, focused),
      };
    });
  }
  if (!clustered) return choices.map(choice => {
    const rejected = choiceRejected(choice);
    return { choice, ...(rejected ? { rejected: true } : {}), ghost: rejected };
  });
  return choices.flatMap(choice => {
    const cluster = clusterForChoice(choice);
    const rejected = choiceRejected(choice);
    if (!cluster || cluster.members.length < 2) return [{
      choice, ...(rejected ? { rejected: true } : {}), ghost: rejected,
    }];
    return [
      ...cluster.members.filter(member => !sameChoice(member, choice))
        .map(member => ({ choice: member, ...(rejected ? { rejected: true } : {}), ghost: true })),
      { choice, ...(rejected ? { rejected: true } : {}), ghost: rejected },
    ];
  });
}

function weeklyGhostProteinUrls(choices, primaryUrl) {
  if (!ENABLE_PROTEIN_ENSEMBLE_EXPERIMENT || !showProteinEnsemble
      || cur?.item?.source !== 'weekly' || !clustered
      || cur.revealed && cur.showAnswer) return [];
  const urls = [];
  for (const choice of choices) {
    const cluster = clusterForChoice(choice);
    for (const member of cluster?.members || [choice]) {
      if (member.afprotein_file && member.afprotein_file !== primaryUrl
          && !urls.includes(member.afprotein_file)) urls.push(member.afprotein_file);
    }
  }
  return urls;
}

function configurePlugin(targetPlugin) {
  try {
    targetPlugin.canvas3d?.setProps({
      camera: { manualReset: true, helper: { axes: { name: 'off', params: {} } } },
      cameraResetDurationMs: 0,
      renderer: { backgroundColor: 0xffffff },
    });
  } catch (e) {}
}
function cancelGridViewerPrewarm() {
  gridViewerPrewarmGeneration++;
}
function waitForViewerPrewarmIdle() {
  return new Promise(resolve => {
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(resolve, { timeout: 250 });
    } else {
      setTimeout(resolve, 0);
    }
  });
}
async function prewarmGridViewerPool() {
  if (!GRID_VIEWER_PREWARM_ENABLED || !WEEKLY_ONLY || quizSource !== 'weekly'
      || displayMode !== 'grid' || isRetrospectiveReview()) return;
  const generation = ++gridViewerPrewarmGeneration;
  await viewerPerformance.measureStartup('grid-viewer-pool-prewarm', async () => {
    while (gridViewerPool.size() < GRID_PAGE_SIZE && generation === gridViewerPrewarmGeneration) {
      await waitForViewerPrewarmIdle();
      if (generation !== gridViewerPrewarmGeneration) return;
      const host = document.createElement('div');
      host.className = 'grid-host';
      let prewarmedViewer;
      try {
        prewarmedViewer = await molstar.Viewer.create(host, { ...OPTS, extensions: [] });
        configurePlugin(prewarmedViewer.plugin);
      } catch (error) {
        try { prewarmedViewer?.dispose?.(); } catch (_) {}
        console.warn('Grid viewer prewarm stopped:', error.message);
        return;
      }
      if (generation !== gridViewerPrewarmGeneration) {
        try { prewarmedViewer.dispose(); } catch (_) {}
        return;
      }
      if (!gridViewerPool.add({
        viewer: prewarmedViewer,
        plugin: prewarmedViewer.plugin,
        host,
      })) return;
    }
  }, { targetSize: GRID_PAGE_SIZE });
}
function cameraChanges(targetPlugin) {
  return targetPlugin?.canvas3d?.camera?.changed || targetPlugin?.canvas3d?.camera?.stateChanged;
}
async function pinCameraSnapshot(targetPlugin, snapshot) {
  if (!targetPlugin?.canvas3d?.camera || !snapshot) return;
  const setSnapshot = value => targetPlugin.canvas3d?.camera?.setState?.(value, 0);
  if (window.pinCameraAfterSettled) {
    await window.pinCameraAfterSettled({
      cameraChanged: cameraChanges(targetPlugin),
      setSnapshot,
      snapshot,
    });
  } else setSnapshot(snapshot);
}
// Grid panes share a viewpoint, but each Mol* scene has its own molecular
// extent.  Never replace a pane's clipping envelope with a smaller pane's:
// doing so can make atoms disappear as the shared camera rotates.
function cameraSnapshotForScene(sharedSnapshot, sceneSnapshot) {
  if (!sharedSnapshot) return sharedSnapshot;
  if (!sceneSnapshot) return sharedSnapshot;
  const snapshot = { ...sharedSnapshot };
  for (const field of ['radius', 'radiusMax']) {
    const shared = Number(sharedSnapshot[field]);
    const local = Number(sceneSnapshot[field]);
    if (Number.isFinite(local) && (!Number.isFinite(shared) || local > shared)) {
      snapshot[field] = local;
    }
  }
  return snapshot;
}
// Keep the current viewpoint pinned while Mol* replaces structures. The
// builders can publish an automatic focus between awaits; restoring only after
// the whole rebuild lets that intermediate camera render as a visible flash.
// Re-pinning from the camera event happens before the next frame instead.
function holdCameraSnapshot(targetPlugin, snapshot) {
  const camera = targetPlugin?.canvas3d?.camera;
  if (!camera || !snapshot) return () => {};
  let released = false, applying = false;
  let signature = null;
  try { signature = JSON.stringify(snapshot); } catch (e) {}
  const apply = () => {
    if (released || applying) return;
    try {
      const current = camera.getSnapshot?.();
      if (signature !== null && JSON.stringify(current) === signature) return;
      applying = true;
      camera.setState?.(snapshot, 0);
    } catch (e) {
      // Camera preservation is best-effort; a failed pin must not break the
      // protein/pose swap itself.
    } finally {
      applying = false;
    }
  };
  const subscription = cameraChanges(targetPlugin)?.subscribe?.(apply) || null;
  apply();
  return () => {
    released = true;
    subscription?.unsubscribe?.();
  };
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
function registerPoseClickTarget(representationSelector, choice) {
  const representation = representationSelector?.obj?.data?.repr;
  if (representation && typeof representation === 'object') {
    poseChoiceByRepresentation.set(representation, choice);
  }
}
function choiceFromPoseInteraction(event) {
  const representation = event?.current?.repr;
  return representation && typeof representation === 'object'
    ? (poseChoiceByRepresentation.get(representation) || null)
    : null;
}
function clearTransientPoseSelection(targetPlugin) {
  const clear = () => {
    try { targetPlugin?.managers?.interactivity?.lociSelects?.deselectAll?.(); } catch (e) {}
  };
  const schedule = window.requestAnimationFrame || globalThis.requestAnimationFrame;
  if (typeof schedule === 'function') schedule(clear);
  else setTimeout(clear, 0);
}
function canonicalInteractionIsEmpty(event) {
  const current = event?.current;
  return !current || current?.loci?.kind === 'empty-loci'
    || (!current.repr && !current.loci);
}
async function cameraSnapshotAfterInteraction(targetPlugin) {
  const camera = targetPlugin?.canvas3d?.camera;
  if (!camera) return null;
  // Mol* click-focus is a 250 ms CameraTransition. Its stateChanged observable
  // fires when the transition is requested, not for each animated frame, so a
  // debounce captures an intermediate zoom. Let the transition actually reach
  // its target before freezing that exact camera through the scene rebuild.
  await nextAnimationFrame();
  for (let frame = 0; camera.transition?.inTransition && frame < 90; frame++) {
    await nextAnimationFrame();
  }
  try { return camera.getSnapshot?.() || null; } catch (e) { return null; }
}
function nextAnimationFrame() {
  return new Promise(resolve => {
    const schedule = window.requestAnimationFrame || globalThis.requestAnimationFrame;
    if (typeof schedule === 'function') schedule(() => resolve());
    else setTimeout(resolve, 0);
  });
}
async function activateCanonicalPoseChoice(index, choice) {
  const revision = ++canonicalPoseActivationRevision;
  const item = cur?.item;
  const cameraSnapshot = await cameraSnapshotAfterInteraction(plugin);
  const retrospective = retrospectiveAnswerActive();
  if (revision !== canonicalPoseActivationRevision || cur?.item !== item
      || displayMode !== 'all' || (cur?.revealed && !retrospective)) return;
  await viewerRebuild.enqueue(() => {
    shownOne = index;
    cur.contextChoice = choice;
    cur.poseFocusChoice = choice;
    selectedPaneId = null;
    nextCanonicalCameraSnapshot = cameraSnapshot;
  }, () => {
    renderUI();
    recordAppEvent('pose_inspected');
  });
}
async function clearWeeklyShowAllContext() {
  canonicalPoseActivationRevision++;
  if (!cur?.contextChoice) return;
  await viewerRebuild.enqueue(() => {
    cur.contextChoice = null;
    cur.poseFocusChoice = null;
    selectedPaneId = null;
    resetCameraOnNextBuild = true;
  }, () => {
    plugin.canvas3d?.requestCameraReset?.();
  });
  recordAppEvent('pose_context_cleared');
}
function visibleIndexForChoice(choice) {
  const visible = visibleChoices();
  if (!clustered) return visible.findIndex(candidate => sameChoice(candidate, choice));
  const cluster = clusterForChoice(choice);
  return visible.findIndex(candidate => clusterForChoice(candidate) === cluster);
}
function onCanonicalPoseInteraction(event) {
  const retrospectiveInteraction = retrospectiveAnswerActive()
    && (displayMode === 'one' || displayMode === 'all');
  if ((retrospectiveInteraction ? viewerControlBlocked() : interactionBlocked())
      || cur?.item?.source !== 'weekly' || displayMode === 'grid'
      || (cur.revealed && !retrospectiveInteraction)) return;
  const choice = choiceFromPoseInteraction(event);
  if (!choice) {
    canonicalPoseActivationRevision++;
    if (displayMode === 'all' && cur.contextChoice && canonicalInteractionIsEmpty(event)) {
      void clearWeeklyShowAllContext().catch(error => {
        console.warn('Could not reset the Show all pose context:', error.message);
      });
    }
    return;
  }
  clearTransientPoseSelection(plugin);
  if (displayMode === 'one') {
    inspectCanonicalChoice(choice);
    return;
  }
  const fixedReference = isFixedReferenceChoice(choice);
  const index = fixedReference ? -1 : visibleIndexForChoice(choice);
  if (index < 0 && !fixedReference) return;
  if (sameChoice(choice, cur.contextChoice)) return;
  void activateCanonicalPoseChoice(index, choice).catch(error => {
    console.warn('Could not inspect the clicked pose:', error.message);
  });
}
function acceptedChoiceCorrect(choice) {
  return cur?.item?.source === 'weekly'
    ? choice?.clusterAccepted === true
    : choice?.correct === true;
}
function rawChoiceCorrect(choice) {
  return choice?.correct === true;
}
function allItemChoices() {
  return cur?.clusters?.flatMap(cluster => cluster.members) || [];
}
function bestRawCorrectPose(choices = allItemChoices()) {
  return choices
    .filter(choice => rawChoiceCorrect(choice) && Number.isFinite(choice.rmsd))
    .sort((left, right) => left.rmsd - right.rmsd)[0] || null;
}
function weeklyResultsRevealActive() {
  return quizSource === 'weekly'
    && (WEEKLY_ROUND?.public_status === 'revealed' || isRetrospectiveReview());
}
function retrospectiveAnswerActive() {
  return weeklyResultsRevealActive() && !!cur?.revealed && !!cur.showAnswer;
}
function answerViewPoseCorrect(choice) {
  if (!cur?.revealed || !cur.showAnswer || cur.item.source !== 'weekly') {
    return acceptedChoiceCorrect(choice);
  }
  return rawChoiceCorrect(choice);
}
function answerPoseStatus(choice) {
  if (rawChoiceCorrect(choice)) return 'Exact correct ✓';
  if (choice?.clusterAccepted === true) return 'Cluster-accepted only';
  return 'Incorrect';
}
function exactChoicesForEntry(entry) {
  const choices = clustered && entry?.cluster
    ? entry.cluster.members
    : [entry?.choice].filter(Boolean);
  return choices
    .filter(rawChoiceCorrect)
    .sort((left, right) => left.rmsd - right.rmsd);
}
function applyAnswerRevealView() {
  const choices = allItemChoices();
  const best = bestRawCorrectPose(choices);
  cur.answerRevealBest = best;
  releasedCrystalMode = false;
  showXtal = false;
  releasedCrystalError = '';
  if (best && displayMode === 'one') {
    const vis = visibleChoices();
    const index = vis.findIndex(choice => sameChoice(choice, best));
    shownOne = index >= 0 ? index : 0;
    cur.contextChoice = best;
    cur.poseFocusChoice = best;
  } else {
    if (displayMode === 'one') shownOne = 0;
    cur.contextChoice = null;
    cur.poseFocusChoice = null;
  }
  resetCameraOnNextBuild = true;
  syncXtalRow();
}
function displayedPoseLabel(choice, asCluster = clustered) {
  if (!choice) return '';
  return asCluster ? (clusterForChoice(choice)?.label || choice.label) : choice.label;
}
function gridPageMethod() {
  if (cur?.item?.source === 'weekly') return null;
  const methods = cur?.gridMethods || [];
  if (!methods.length) return null;
  gridMethodIndex = Math.min(gridMethodIndex, methods.length - 1);
  return methods[gridMethodIndex];
}
function gridEntriesFor(method) {
  const vis = visibleChoices();
  let entries;
  if (!method) entries = vis.map((choice, choiceIndex) => {
    const cluster = cur.clusters.find(c => c.members.includes(choice));
    return { choice, choiceIndex, cluster, memberCount: clustered ? cluster.members.length : 1 };
  });
  else if (!clustered) entries = vis.map((choice, choiceIndex) => ({ choice, choiceIndex,
    cluster: cur.clusters.find(c => c.members.includes(choice)), memberCount: 1 }))
    .filter(x => x.choice._method === method);
  else entries = cur.clusters.map((cluster, choiceIndex) => {
    const members = cluster.members.filter(c => c._method === method);
    const choice = members.find(c => c.is_rep) || members[0];
    return choice ? { choice, choiceIndex, cluster, memberCount: members.length } : null;
  }).filter(Boolean);
  if (retrospectiveAnswerActive()) {
    if (itemHasReleasedCrystal(cur.item)) {
      entries.push({
        choice: buildXtalReferenceChoice(cur.item),
        choiceIndex: entries.length,
        cluster: null,
        memberCount: 1,
        xtalReference: true,
      });
    }
    const training = buildTrainingReferenceChoice(cur.item);
    if (training) {
      entries.push({
        choice: training,
        choiceIndex: entries.length,
        cluster: null,
        memberCount: 1,
        trainingReference: true,
      });
    }
  }
  return entries;
}
function weeklyGridPage() {
  const entries = gridEntriesFor(null);
  if (DEV2_FEEDBACK.gridPage) return DEV2_FEEDBACK.gridPage(entries, gridMethodIndex, GRID_PAGE_SIZE);
  const pages = Math.max(1, Math.ceil(entries.length / GRID_PAGE_SIZE));
  const index = Math.min(Math.max(0, gridMethodIndex), pages - 1);
  return { index, pages, entries: entries.slice(index * GRID_PAGE_SIZE, (index + 1) * GRID_PAGE_SIZE) };
}
function gridEntries() {
  return cur?.item?.source === 'weekly' ? weeklyGridPage().entries : gridEntriesFor(gridPageMethod());
}
function allGridEntries() {
  if (cur?.item?.source === 'weekly') return gridEntriesFor(null);
  const methods = cur?.gridMethods || [];
  return methods.length ? methods.flatMap(gridEntriesFor) : gridEntriesFor(null);
}
function choiceEntriesForSidebar() {
  if (displayMode === 'grid') {
    // Grid pagination is only a rendering optimization.  Keep the complete
    // ballot visible so moving to page 2 never hides or forgets page-1 poses.
    return cur?.item?.source === 'weekly' ? allGridEntries() : gridEntries();
  }
  return visibleChoices().map((choice, choiceIndex) => ({ choice, choiceIndex,
    cluster: cur.clusters.find(c => c.members.includes(choice)), memberCount: 1 }));
}
function weeklyGridPageIndexForChoice(choice) {
  if (cur?.item?.source !== 'weekly' || !choice) return gridMethodIndex;
  const index = allGridEntries().findIndex(entry => sameChoice(entry.choice, choice));
  return index < 0 ? gridMethodIndex : Math.floor(index / GRID_PAGE_SIZE);
}
async function pickSidebarEntry(entry) {
  await onPick(entry.choiceIndex, displayMode === 'grid' ? entry.choice : null);
  if (displayMode !== 'grid' || cur?.item?.source !== 'weekly') return;
  const pageIndex = weeklyGridPageIndexForChoice(entry.choice);
  if (pageIndex === gridMethodIndex || viewerControlBlocked()) return;
  await viewerRebuild.enqueue(
    () => { gridMethodIndex = pageIndex; },
    () => { renderGridPages(); renderUI(); recordAppEvent('grid_page_changed'); },
  );
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

function applyRetrospectiveAnswer() {
  if (!isRetrospectiveReview() || cur?.item?.source !== 'weekly') return false;
  const choices = allItemChoices();
  const best = (isPrivatePrecloseReview()
    ? window.foldariumPrivateReview?.selectRetrospectiveAnswer?.({ choices })
    : null) || bestRawCorrectPose(choices);
  cur.selected = best || {
    none: true,
    correct: true,
    label: 'None of these',
  };
  cur.selectionExact = true;
  cur.selectedAsCluster = false;
  cur.answerChoices = choices;
  cur.revealed = true;
  cur.showAnswer = true;
  applyAnswerRevealView();
  if (displayMode === 'all') {
    cur.contextChoice = null;
    cur.poseFocusChoice = null;
  }
  if (itemHasReleasedCrystal(cur.item)) {
    releasedCrystalMode = false;
    showXtal = true;
    releasedCrystalError = '';
  }
  return true;
}
function renderGridPages() {
  const nav = $('#gridpages'), methods = cur?.gridMethods || [];
  if (!cur || displayMode !== 'grid') { nav.style.display = 'none'; nav.innerHTML = ''; return; }
  if (cur.item.source === 'weekly') {
    const page = weeklyGridPage();
    gridMethodIndex = page.index;
    if (page.pages < 2) { nav.style.display = 'none'; nav.innerHTML = ''; return; }
    nav.style.display = ''; nav.innerHTML = '';
    for (let i = 0; i < page.pages; i++) {
      const b = document.createElement('button');
      b.classList.toggle('on', i === page.index);
      const start = i * GRID_PAGE_SIZE + 1;
      const end = Math.min((i + 1) * GRID_PAGE_SIZE, allGridEntries().length);
      b.textContent = `${start}–${end}`;
      b.onclick = async () => {
        if (i === gridMethodIndex || viewerControlBlocked()) return;
        await viewerRebuild.enqueue(
          () => { gridMethodIndex = i; },
          () => { renderGridPages(); renderUI(); recordAppEvent('grid_page_changed'); },
        );
      };
      nav.appendChild(b);
    }
    return;
  }
  if (methods.length < 2) { nav.style.display = 'none'; nav.innerHTML = ''; return; }
  nav.style.display = ''; nav.innerHTML = '';
  methods.forEach((method, i) => {
    const b = document.createElement('button');
    b.classList.toggle('on', i === gridMethodIndex);
    b.textContent = cur.showAnswer ? methodName(method) : `Set ${i + 1}`;
    b.onclick = async () => {
      if (i === gridMethodIndex || viewerControlBlocked()) return;
      await viewerRebuild.enqueue(
        () => { gridMethodIndex = i; },
        () => { renderGridPages(); renderUI(); recordAppEvent('grid_page_changed'); },
      );
    };
    nav.appendChild(b);
  });
}
function gridProteinUrls(choice, spec) {
  const item = spec.item;
  if (item.source === 'weekly') {
    return {
      prot: choice.afprotein_file || item.protein_file,
      pocket: choice.afpocket_file || item.pocket_file,
      color: PROT,
    };
  }
  if (spec.proteinMode === 'af3' && item.afprotein_ref) {
    return { prot: choice.afprotein_file || item.afprotein_ref,
      pocket: choice.afpocket_file || item.afpocket_union, color: AF3PROT };
  }
  return { prot: item.protein_file, pocket: item.pocket_file, color: PROT };
}
function gridHeader(entry) {
  const c = entry.choice, answer = cur.revealed && cur.showAnswer;
  if (isTrainingReferenceChoice(c)) {
    return `<span class="grid-dot" style="background:${hex(TRAINING_LIGAND)}"></span>`
      + '<span class="grid-title">Closest training</span>'
      + `<span class="grid-meta">${trainingReferenceAnnotation(c).replace(
        'Closest training · ',
        '',
      )}</span>`;
  }
  if (isXtalReferenceChoice(c)) {
    return `<span class="grid-dot" style="background:${hex(XTAL)}"></span><span class="grid-title">Xtal reference</span>`;
  }
  const bits = [];
  if (clustered && entry.memberCount > 1) bits.push(`${entry.memberCount} poses`);
  if (cur.item.source === 'weekly') {
    if (answer) {
      const members = clustered && entry.cluster ? entry.cluster.members : [c];
      const votes = members.reduce(
        (total, choice) => total + Number(choice._weeklyVoteCount || 0),
        0,
      );
      bits.push(`${votes} votes`);
    } else {
      const confidence = weeklyLigandPlddt(c);
      if (confidence) bits.push(confidence.replace(/^ligand /, ''));
    }
  }
  if (answer) {
    if (cur.item.source === 'rnp' && c._method) bits.push(methodName(c._method));
    if (gridChoiceSelected(c)) bits.push('YOU');
    if (c.af3_sample === cur.item.plddt_pick_sample) bits.push('AI');
  }
  const color = answer ? (answerViewPoseCorrect(c) ? GOOD : BAD) : c.color;
  const rmsd = answer && Number.isFinite(c.rmsd)
    ? `<span class="grid-rmsd ${answerViewPoseCorrect(c) ? 'correct' : 'wrong'}">RMSD ${c.rmsd.toFixed(2)} Å</span>`
    : '';
  return `<span class="grid-dot" style="background:${hex(color)}"></span><span class="grid-title">Pose ${displayedPoseLabel(c)}</span>`
    + rmsd
    + (bits.length ? `<span class="grid-meta">${bits.join(' · ')}</span>` : '');
}
function viewerQuestionIdentity() {
  const released = cur?.item?.released_crystal;
  if (isRetrospectiveReview() && released?.pdb_id && released?.structure_page_url) {
    return {
      label: released.pdb_id.toUpperCase(),
      url: released.structure_page_url,
    };
  }
  return { label: cur?.item?.ligand || '', url: null };
}
function renderViewerQuestionTitle(poseSummary) {
  const host = $('#ligand');
  const identity = viewerQuestionIdentity();
  host.replaceChildren();
  if (identity.url) {
    const link = document.createElement('a');
    link.href = identity.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = `${identity.label} ↗`;
    link.setAttribute('aria-label', `Open ${identity.label} in RCSB`);
    host.appendChild(link);
  } else {
    host.append(identity.label);
  }
  const summary = document.createElement('small');
  summary.textContent = `· ${poseSummary}`;
  host.append(' ', summary);
}
function attachPoseInfo(root, evidence) {
  if (!evidence) return;
  const info = document.createElement('span');
  info.className = 'pose-info';
  info.textContent = 'i';
  info.tabIndex = 0;
  info.setAttribute('aria-label', `Pose information: ${evidence}`);
  info.dataset.tooltip = evidence;
  const showTooltip = () => showPoseInfoTooltip(info, evidence);
  const hideTooltip = () => hidePoseInfoTooltip(info);
  info.addEventListener('pointerenter', showTooltip);
  info.addEventListener('pointerleave', hideTooltip);
  info.addEventListener('focus', showTooltip);
  info.addEventListener('blur', hideTooltip);
  const stopSelection = event => {
    event.preventDefault();
    event.stopPropagation();
  };
  info.addEventListener('pointerdown', stopSelection);
  info.addEventListener('click', stopSelection);
  info.addEventListener('keydown', event => {
    if (event.key === 'Escape') { hideTooltip(); info.blur(); }
    event.stopPropagation();
  });
  root.appendChild(info);
}
let poseInfoTooltipOwner = null;
function poseInfoTooltipPosition(anchor, tooltip, viewport) {
  const margin = 8, gap = 8;
  const maxLeft = Math.max(margin, viewport.width - tooltip.width - margin);
  const left = Math.min(maxLeft, Math.max(margin, anchor.right - tooltip.width));
  const above = anchor.top - tooltip.height - gap;
  const below = anchor.bottom + gap;
  const maxTop = Math.max(margin, viewport.height - tooltip.height - margin);
  return { left, top: Math.min(maxTop, Math.max(margin, above >= margin ? above : below)) };
}
function showPoseInfoTooltip(owner, evidence) {
  const tooltip = $('#pose-tooltip');
  if (!tooltip) return;
  poseInfoTooltipOwner = owner;
  tooltip.replaceChildren();
  for (const item of poseInfoTooltipRows(evidence)) {
    const row = document.createElement('div');
    row.className = 'pose-tooltip-row';
    const method = document.createElement('span');
    method.className = 'pose-tooltip-method';
    method.textContent = item.method;
    const metrics = document.createElement('div');
    metrics.className = 'pose-tooltip-metrics';
    for (const itemMetric of item.metrics) {
      const metric = document.createElement('span');
      metric.className = 'pose-tooltip-metric';
      const label = document.createElement('small');
      label.textContent = itemMetric.label;
      const value = document.createElement('strong');
      value.textContent = itemMetric.value;
      metric.append(label, value);
      metrics.appendChild(metric);
    }
    row.append(method, metrics);
    tooltip.appendChild(row);
  }
  tooltip.hidden = false;
  const position = poseInfoTooltipPosition(
    owner.getBoundingClientRect(),
    tooltip.getBoundingClientRect(),
    { width: window.innerWidth, height: window.innerHeight },
  );
  tooltip.style.left = `${position.left}px`;
  tooltip.style.top = `${position.top}px`;
}
function hidePoseInfoTooltip(owner) {
  if (owner !== poseInfoTooltipOwner) return;
  const tooltip = $('#pose-tooltip');
  if (tooltip) tooltip.hidden = true;
  poseInfoTooltipOwner = null;
}
function hideActivePoseInfoTooltip() {
  const tooltip = $('#pose-tooltip');
  if (tooltip) tooltip.hidden = true;
  poseInfoTooltipOwner = null;
}
function disposeGridViewers() {
  hideActivePoseInfoTooltip();
  if (stopGridCameraSync) { stopGridCameraSync(); stopGridCameraSync = null; }
  if (stopGridLayout) { stopGridLayout(); stopGridLayout = null; }
  const performanceTiming = viewerPerformance.current();
  for (const cell of gridViewers) {
    cell.disposed = true;
    try { cell.poseClickSubscription?.unsubscribe?.(); } catch (e) {}
    try { cell.detachReplay?.(); } catch (e) {}
    const clear = () => cell.plugin.clear();
    gridViewerPool.release(cell, {
      clear: performanceTiming
        ? () => viewerPerformance.measure(
          performanceTiming,
          'grid-viewer-reuse-clear',
          clear,
          { paneId: cell.paneId },
        )
        : clear,
    });
  }
  gridViewers = []; $('#gridcells').replaceChildren();
}
function layoutGrid() {
  const view = $('#gridview'), box = $('#gridcells'), n = gridViewers.length;
  if (!n || !view.classList.contains('on')) return;
  const width = view.clientWidth - 20, height = view.clientHeight - 20, gap = 10, aspect = 4 / 3;
  let best = null;
  for (let columns = 1; columns <= Math.min(3, n); columns++) {
    const rows = Math.ceil(n / columns);
    const tileWidth = Math.min((width - gap * (columns - 1)) / columns, (height - gap * (rows - 1)) / rows * aspect);
    if (!best || tileWidth > best.tileWidth) {
      best = { columns, tileWidth, tileHeight: tileWidth / aspect };
    }
  }
  if (!best || best.tileWidth <= 0) return;
  const cardWidth = Math.floor(best.tileWidth * 10) / 10;
  const cardHeight = Math.floor(best.tileHeight * 10) / 10;
  const gridWidth = Math.floor((cardWidth * best.columns + gap * (best.columns - 1)) * 10) / 10;
  box.style.setProperty('--grid-card-w', `${cardWidth}px`);
  box.style.setProperty('--grid-card-h', `${cardHeight}px`);
  box.style.maxWidth = `${gridWidth}px`;
  for (const cell of gridViewers) cell.viewer?.handleResize?.();
}
function reserveGridControlClearance() {
  const controls = $('#view-options'), stage = $('#stage');
  if (!controls || !stage || controls.hidden) return;
  const height = Math.ceil(controls.getBoundingClientRect().height);
  stage.style.setProperty('--grid-controls-clearance', `${height + 28}px`);
}
function reserveGridTopClearance() {
  const question = $('#viewer-question'), stage = $('#stage');
  if (!question || !stage) return;
  const questionRect = question.getBoundingClientRect();
  const stageRect = stage.getBoundingClientRect();
  const clearance = Math.max(84, Math.ceil(questionRect.bottom - stageRect.top + 12));
  stage.style.setProperty('--grid-top-clearance', `${clearance}px`);
}
function startGridLayout() {
  const observer = new ResizeObserver(() => {
    reserveGridControlClearance(); reserveGridTopClearance(); layoutGrid();
  });
  observer.observe($('#gridview'));
  observer.observe($('#view-options'));
  observer.observe($('#viewer-question'));
  reserveGridControlClearance(); reserveGridTopClearance(); layoutGrid();
  stopGridLayout = () => observer.disconnect();
}
function hideGrid() {
  gridBuildRevision++; disposeGridViewers(); $('#gridview').classList.remove('on', 'loading-grid');
  $('#stage').classList.remove('grid-active'); renderGridPages();
}
function syncGridCameras(cells) {
  const cameraSnapshot = cell => cell.plugin?.canvas3d?.camera?.getSnapshot?.();
  let enabled = true, raf = 0, last = cells.map(cell => JSON.stringify(cameraSnapshot(cell)));
  const tick = () => {
    if (!enabled) return;
    const snapshots = cells.map(cameraSnapshot);
    const changed = snapshots.map((snapshot, i) => (
      snapshot && JSON.stringify(snapshot) !== last[i] ? i : -1
    )).filter(i => i >= 0);
    const attributed = cells.findIndex(cell => cell.paneId === activePaneId);
    const source = changed.includes(attributed) ? attributed : (changed[0] ?? -1);
    if (source >= 0) {
      const snapshot = snapshots[source];
      activatePane(cells[source].paneId, 'camera');
      for (let i = 0; i < cells.length; i++) {
        if (i !== source) cells[i].plugin?.canvas3d?.camera?.setState(
          cameraSnapshotForScene(snapshot, cells[i].cameraEnvelope), 0);
      }
      // mirror into the hidden canonical viewer so the trace recorder still sees Grid camera movement
      try {
        const canonicalEnvelope = plugin?.canvas3d?.camera?.getSnapshot?.();
        plugin?.canvas3d?.camera?.setState(
          cameraSnapshotForScene(snapshot, canonicalEnvelope), 0);
      } catch (e) {}
      try { viewerTraceRecorder?.captureCamera?.(snapshot, { sourcePaneId: cells[source].paneId }); }
      catch (error) { console.warn('Grid camera replay event omitted:', error.message); }
      last = cells.map(cell => JSON.stringify(cameraSnapshot(cell)));
    }
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => { enabled = false; cancelAnimationFrame(raf); };
}
async function buildRetrospectiveXtalGridCell(cell) {
  const xtal = cell.entry.choice;
  await addRetrospectiveCrystalContext(
    cell.plugin,
    null,
    [xtal.answer_crystal_pdb],
    representation => registerPoseClickTarget(representation, xtal),
  );
  await addRetrospectiveCrystalPocketSticks(xtal, cell.plugin);
  if (cell.spec.showHbonds) {
    const pocketPdb = xtal.answer_crystal_pocket_pdb;
    if (typeof pocketPdb !== 'string' || !pocketPdb) {
      cell.hbondStatus = 'H-bonds unavailable: crystal pocket artifact is missing.';
    } else {
      try {
        await buildRetrospectiveInteractions(
          pocketPdb,
          null,
          xtal.answer_crystal_pdb,
          cell.plugin,
          null,
          { includePredicted: false },
        );
      } catch (error) {
        cell.hbondStatus = `H-bonds unavailable: ${error.message}`;
      }
    }
  }
  cell.poseSphere = null;
}
async function buildRetrospectiveTrainingGridCell(cell) {
  const training = cell.entry.choice;
  const context = visibleChoices()[0] || null;
  if (context?.answer_crystal_pdb) {
    await addRetrospectiveCrystalContext(
      cell.plugin,
      null,
      [context.answer_crystal_pdb],
    );
    await addRetrospectiveCrystalPocketSticks(context, cell.plugin);
  }
  const loaded = await addTrainingReferencePose(
    training,
    cell.plugin,
    null,
    { surface: cell.spec.showSurface },
  );
  if (!loaded) throw new Error('Closest training pose could not be rendered');
  cell.poseSphere = structureSphere(loaded.struct);
}
async function buildRetrospectiveGridCell(cell, c) {
  if (isTrainingReferenceChoice(c)) {
    await buildRetrospectiveTrainingGridCell(cell);
    return;
  }
  if (isXtalReferenceChoice(c)) {
    await buildRetrospectiveXtalGridCell(cell);
    return;
  }
  const poseMembers = cell.spec.clustered
    ? [
        ...(cell.entry.cluster?.members || []).filter(member => !sameChoice(member, c))
          .map(choice => ({ choice, ghost: true })),
        { choice: c, ghost: false },
      ]
    : [{ choice: c, ghost: false }];
  await addRetrospectiveCrystalContext(
    cell.plugin,
    null,
    poseMembers.map(layer => layer.choice.answer_crystal_pdb),
  );
  await addRetrospectiveCrystalPocketSticks(c, cell.plugin);
  for (const layer of poseMembers) {
    if (typeof layer.choice.answer_overlay_pdb !== 'string' || !layer.choice.answer_overlay_pdb) {
      throw new Error(`Retrospective pose overlay is missing for ${
        layer.choice._weeklyChoiceId || layer.choice.id
      }`);
    }
    const pose = await loadStructText(layer.choice.answer_overlay_pdb, 'pdb', cell.plugin);
    const poseColor = answerViewPoseCorrect(layer.choice) ? GOOD : BAD;
    const poseRepresentation = await addPose(
      pose.struct,
      poseColor,
      cell.plugin,
      layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined,
    );
    let surfaceRepresentation = null;
    if (cell.spec.showSurface && !layer.ghost) {
      surfaceRepresentation = await addRep(
        pose.struct, 'all', 'molecular-surface', poseColor, 0.7, cell.plugin,
      );
    }
    registerPoseClickTarget(poseRepresentation, layer.choice);
    registerPoseClickTarget(surfaceRepresentation, layer.choice);
    if (!layer.ghost) cell.poseSphere = structureSphere(pose.struct);
  }
  if (cell.spec.showHbonds && itemHasReleasedCrystal(cell.spec.item)) {
    const pocketPdb = c.answer_crystal_pocket_pdb;
    if (typeof pocketPdb !== 'string' || !pocketPdb) {
      cell.hbondStatus = 'H-bonds unavailable: crystal pocket artifact is missing.';
    } else {
      try {
        await buildRetrospectiveInteractions(
          pocketPdb,
          c.answer_overlay_pdb,
          c.answer_crystal_pdb,
          cell.plugin,
          null,
        );
      } catch (error) {
        cell.hbondStatus = `H-bonds unavailable: ${error.message}`;
        console.warn('Retrospective grid H-bonds omitted:', error.message);
      }
    }
  }
}
async function buildRetrospectiveFoldedGridCell(cell, c, urls) {
  const aligned = await alignedFoldedAssets(c, urls);
  const protein = await loadStructText(aligned.proteinPdb, 'pdb', cell.plugin);
  await addRep(protein.struct, 'polymer', 'cartoon', urls.color, 0.5, cell.plugin);
  if (cell.spec.showSurface) {
    await addRep(protein.struct, 'polymer', 'molecular-surface', urls.color, 0.7, cell.plugin);
  }
  if (aligned.pocketPdb) {
    const pocket = await loadStructText(aligned.pocketPdb, 'pdb', cell.plugin);
    await addSticks(pocket.struct, 0.16, 0.95, cell.plugin);
  }
  const poseMembers = cell.spec.clustered
    ? [
        ...(cell.entry.cluster?.members || []).filter(member => !sameChoice(member, c))
          .map(choice => ({ choice, ghost: true })),
        { choice: c, ghost: false },
      ]
    : [{ choice: c, ghost: false }];
  const crystalChoiceByPdb = new Map();
  for (const layer of poseMembers) {
    if (!crystalChoiceByPdb.has(layer.choice.answer_crystal_pdb)) {
      crystalChoiceByPdb.set(layer.choice.answer_crystal_pdb, layer.choice);
    }
  }
  for (const [crystalPdb, crystalChoice] of crystalChoiceByPdb) {
    const crystal = await loadStructText(crystalPdb, 'pdb', cell.plugin);
    const representation = await addCrystalPose(crystal.struct, cell.plugin);
    registerPoseClickTarget(representation, crystalChoice);
    if (cell.spec.showSurface) {
      await addRep(crystal.struct, 'all', 'molecular-surface', XTAL, 0.7, cell.plugin);
    }
  }
  for (const layer of poseMembers) {
    const pose = await loadStructText(layer.choice.answer_overlay_pdb, 'pdb', cell.plugin);
    const poseColor = answerViewPoseCorrect(layer.choice) ? GOOD : BAD;
    const representation = await addPose(
      pose.struct,
      poseColor,
      cell.plugin,
      layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined,
    );
    let surfaceRepresentation = null;
    if (cell.spec.showSurface && !layer.ghost) {
      surfaceRepresentation = await addRep(
        pose.struct, 'all', 'molecular-surface', poseColor, 0.7, cell.plugin,
      );
    }
    registerPoseClickTarget(representation, layer.choice);
    registerPoseClickTarget(surfaceRepresentation, layer.choice);
    if (!layer.ghost) cell.poseSphere = structureSphere(pose.struct);
  }
  if (cell.spec.showHbonds && aligned.pocketPdb) {
    try {
      await buildRetrospectiveInteractions(
        aligned.pocketPdb,
        c.answer_overlay_pdb,
        c.answer_crystal_pdb,
        cell.plugin,
      );
    } catch (error) {
      cell.hbondStatus = `H-bonds unavailable: ${error.message}`;
      console.warn('Folded-protein Grid H-bonds omitted:', error.message);
    }
  }
}
async function populateGridCell(cell, revision, { preserveCamera = null } = {}) {
  const c = cell.entry.choice, urls = gridProteinUrls(c, cell.spec);
  const crystalFrame = cell.spec.retrospectiveProteinFrame !== 'folded'
    || isFixedReferenceChoice(c);
  if (cell.spec.retrospectiveReview && cell.spec.answer && !crystalFrame) {
    await buildRetrospectiveFoldedGridCell(cell, c, urls);
  } else if (cell.spec.retrospectiveReview && cell.spec.answer && crystalFrame
      && itemHasReleasedCrystal(cell.spec.item)) {
    await buildRetrospectiveGridCell(cell, c);
  } else {
    const pr = await loadStruct(urls.prot, 'pdb', cell.plugin);
    await addRep(pr.struct, 'polymer', 'cartoon', urls.color, 0.5, cell.plugin);
    if (cell.spec.showSurface) {
      await addRep(pr.struct, 'polymer', 'molecular-surface', urls.color, 0.7, cell.plugin);
    }
    if (cell.spec.showProteinEnsemble && cell.spec.item.source === 'weekly' && cell.spec.clustered) {
      const proteinUrls = [...new Set((cell.entry.cluster?.members || [])
        .map(member => member.afprotein_file)
        .filter(url => url && url !== urls.prot))];
      for (const proteinUrl of proteinUrls) {
        const ghostProtein = await loadStruct(proteinUrl, 'pdb', cell.plugin);
        await addRep(ghostProtein.struct, 'polymer', 'cartoon', urls.color,
          GHOST_PROTEIN_ALPHA, cell.plugin);
      }
    }
    if (urls.pocket) { const ps = await loadStruct(urls.pocket, 'pdb', cell.plugin); await addSticks(ps.struct, 0.16, 0.95, cell.plugin); }
    const poseMembers = cell.spec.item.source === 'weekly' && cell.spec.clustered
      ? [
          ...(cell.entry.cluster?.members || []).filter(member => !sameChoice(member, c))
            .map(choice => ({ choice, ghost: true })),
          { choice: c, ghost: false },
        ]
      : [{ choice: c, ghost: false }];
    for (const layer of poseMembers) {
      const pose = await loadStruct(layer.choice.pose_file, 'pdb', cell.plugin);
      const poseColor = cell.spec.answer
        ? ((cell.spec.retrospectiveReview ? answerViewPoseCorrect(layer.choice) : acceptedChoiceCorrect(layer.choice))
          ? GOOD : BAD)
        : c.color;
      const poseRepresentation = await addPose(pose.struct,
        poseColor,
        cell.plugin,
        layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined);
      let surfaceRepresentation = null;
      if (cell.spec.showSurface && !layer.ghost) {
        surfaceRepresentation = await addRep(
          pose.struct, 'all', 'molecular-surface', poseColor, 0.7, cell.plugin);
      }
      registerPoseClickTarget(poseRepresentation, c);
      registerPoseClickTarget(surfaceRepresentation, c);
      if (!layer.ghost) cell.poseSphere = structureSphere(pose.struct);
    }
    if (cell.spec.showHbonds && urls.pocket) {
      await buildInteractions(urls.pocket, [c.pose_file], cell.plugin);
    }
  }
  if (cell.spec.item.source === 'weekly') {
    cell.poseClickSubscription = cell.plugin.behaviors?.interaction?.click?.subscribe(event => {
      if ((locked() && !cell.spec.retrospectiveReview)
          || !sameChoice(choiceFromPoseInteraction(event), c)) return;
      clearTransientPoseSelection(cell.plugin);
      inspectGridChoice(cell.entry, cell.paneId, 'ligand-click');
    }) || null;
  }
  if (revision !== gridBuildRevision || cell.disposed) return;
  cell.viewer.handleResize?.();
  if (preserveCamera) {
    await pinCameraSnapshot(cell.plugin, preserveCamera);
  } else {
    await window.waitForCameraSettled({
      cameraChanged: cameraChanges(cell.plugin),
      requestReset: () => cell.plugin.canvas3d?.requestCameraReset?.(),
    });
  }
  cell.cameraEnvelope = cell.plugin.canvas3d?.camera?.getSnapshot?.() || null;
}
async function buildGridCell(cell, revision) {
  try {
    const reporter = cell.spec.performanceReporter;
    const pooled = typeof gridViewerPool !== 'undefined'
      ? await gridViewerPool.acquire()
      : null;
    let gridViewer;
    if (pooled) {
      cell.host.replaceWith(pooled.host);
      cell.host = pooled.host;
      gridViewer = pooled.viewer;
      cell.reusedViewer = true;
      cell.viewerSource = pooled.source;
    } else {
      const createViewer = () => molstar.Viewer.create(cell.host, { ...OPTS, extensions: [] });
      gridViewer = reporter
        ? await reporter.measure(cell.spec.performanceTiming, 'grid-viewer-create', createViewer)
        : await createViewer();
      cell.viewerSource = 'created';
    }
    cell.viewer = gridViewer; cell.plugin = gridViewer.plugin;
    if (revision !== gridBuildRevision || cell.disposed) return gridViewer.dispose();
    configurePlugin(cell.plugin);
    try {
      cell.detachReplay = viewerTraceRecorder?.attachPane?.({
        plugin: cell.plugin,
        paneId: cell.paneId,
        element: cell.card,
      }) || null;
    } catch (error) {
      console.warn('Grid pane replay attachment omitted:', error.message);
    }
    const populate = () => populateGridCell(cell, revision);
    if (reporter) {
      await reporter.measure(cell.spec.performanceTiming, 'grid-cell-populate', populate);
      if (revision !== gridBuildRevision || cell.disposed) return;
      cell.card.classList.remove('grid-card-loading');
      reporter.milestone(
        cell.spec.performanceTiming,
        'first-grid-card-ready',
        { paneId: cell.paneId },
      );
    } else {
      await populate();
    }
    if (revision === gridBuildRevision && !cell.disposed) cell.reusable = true;
  } catch (e) {
    try { cell.poseClickSubscription?.unsubscribe?.(); } catch (_) {}
    cell.poseClickSubscription = null;
    try { cell.viewer?.dispose(); } catch (_) {}
    cell.viewer = null; cell.plugin = null;
    if (!cell.disposed && revision === gridBuildRevision) {
      cell.failed = true;
      cell.card.classList.remove('grid-card-loading');
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
async function buildGrid(preserveCamera = true, preserveCanonicalCamera = true) {
  const previousCamera = preserveCamera
    ? (gridViewers.find(cell => cell.plugin?.canvas3d)?.plugin.canvas3d.camera.getSnapshot()
      || (preserveCanonicalCamera ? plugin?.canvas3d?.camera?.getSnapshot?.() : null))
    : null;
  const revision = ++gridBuildRevision;
  disposeGridViewers();
  const view = $('#gridview'), cellsBox = $('#gridcells');
  view.classList.add('on', 'loading-grid'); $('#stage').classList.add('grid-active'); renderGridPages();
  const cells = gridEntries().map((entry, paneIndex) => {
    const paneId = `pane-${gridMethodIndex}-${paneIndex}`;
    const card = document.createElement('div');
    const xtalReference = isXtalReferenceChoice(entry.choice);
    const trainingReference = isTrainingReferenceChoice(entry.choice);
    const fixedReference = xtalReference || trainingReference;
    const answerActive = cur.revealed && cur.showAnswer;
    const entryProteinFrame = fixedReference
      ? 'xtal'
      : (retrospectiveGridProteinFrames.get(retrospectiveChoiceKey(entry.choice)) || 'xtal');
    const exactEntryChoices = answerActive && !fixedReference
      ? exactChoicesForEntry(entry) : [];
    card.className = 'grid-card grid-card-loading'
      + ((answerActive && !fixedReference)
        ? (exactEntryChoices.length ? ' correct' : ' wrong') : '')
      + (xtalReference ? ' xtal-reference' : '')
      + (trainingReference ? ' training-reference' : '')
      + (choiceRejected(entry.choice) ? ' rejected' : '')
      + (sameChoice(cur.contextChoice, entry.choice) ? ' inspecting' : '');
    card.dataset.paneId = paneId;
    for (const [eventName, reason] of [['pointerenter', 'hover'], ['focusin', 'focus'], ['wheel', 'scroll']]) {
      card.addEventListener(eventName, () => activatePane(paneId, reason), { passive: true });
    }
    const head = document.createElement('button');
    const canInspect = !locked() || (answerActive && isRetrospectiveReview());
    head.type = 'button'; head.className = 'grid-head'; head.innerHTML = gridHeader(entry);
    head.disabled = !canInspect;
    attachPoseInfo(head, weeklyEntryEvidence(entry));
    head.onclick = () => {
      if (!canInspect) return;
      inspectGridChoice(entry, paneId, 'header-click');
    };
    const actions = document.createElement('div'); actions.className = 'grid-review-actions';
    const select = document.createElement('button');
    const reject = document.createElement('button');
    select.type = 'button';
    reject.type = 'button';
    select.disabled = viewerTransitionBusy;
    reject.disabled = viewerTransitionBusy;
    if (answerActive && isRetrospectiveReview()) {
      select.dataset.frame = 'xtal';
      select.textContent = 'Xtal';
      select.classList.toggle('on', entryProteinFrame === 'xtal');
      select.setAttribute('aria-pressed', String(entryProteinFrame === 'xtal'));
      select.onclick = event => {
        event.stopPropagation();
        void setRetrospectiveGridProteinFrame(entry.choice, 'xtal');
      };
      reject.dataset.frame = 'folded';
      reject.textContent = 'Folded';
      reject.classList.toggle('on', entryProteinFrame === 'folded');
      reject.setAttribute('aria-pressed', String(entryProteinFrame === 'folded'));
      reject.onclick = event => {
        event.stopPropagation();
        void setRetrospectiveGridProteinFrame(entry.choice, 'folded');
      };
    } else {
      select.dataset.review = 'select'; select.textContent = 'Select';
      select.setAttribute('aria-label', `Select Pose ${displayedPoseLabel(entry.choice)}`);
      select.onclick = event => {
        event.stopPropagation();
        selectedPaneId = paneId;
        void onPick(entry.choiceIndex, entry.choice);
      };
      reject.dataset.review = 'reject'; reject.className = 'reject';
      reject.textContent = choiceRejected(entry.choice) ? 'Undo reject' : 'Reject';
      reject.classList.toggle('on', choiceRejected(entry.choice));
      reject.setAttribute('aria-pressed', String(choiceRejected(entry.choice)));
      reject.onclick = event => {
        event.stopPropagation();
        void toggleChoiceRejected(entry.choice);
      };
    }
    actions.append(select, reject);
    if (fixedReference) actions.hidden = true;
    const host = document.createElement('div'); host.className = 'grid-host';
    card.append(host, head);
    card.appendChild(actions);
    cellsBox.appendChild(card);
    return { entry, paneId, card, head, host, viewer: null, plugin: null, poseSphere: null,
      cameraEnvelope: null, disposed: false, reusable: false, reusedViewer: false,
      viewerSource: null,
      detachReplay: null, poseClickSubscription: null,
      spec: { item: cur.item, proteinMode, answer: cur.revealed && cur.showAnswer,
        clustered, showHbonds, showProteinEnsemble, showSurface,
        retrospectiveReview: isRetrospectiveReview(), retrospectiveProteinFrame: entryProteinFrame,
        performanceTiming: viewerPerformance.current(),
        performanceReporter: viewerPerformance } };
  });
  gridViewers = cells; startGridLayout(); syncGridSelection();
  await Promise.allSettled(cells.map(cell => buildGridCell(cell, revision)));
  if (revision !== gridBuildRevision) return;
  const active = cells.filter(cell => cell.plugin?.canvas3d);
  cells[0]?.spec.performanceReporter?.milestone(
    cells[0]?.spec.performanceTiming,
    'all-grid-cards-ready',
    {
      cards: active.length,
      failed: cells.length - active.length,
      viewersReused: active.filter(cell => cell.reusedViewer).length,
      viewersPrewarmed: active.filter(cell => cell.viewerSource === 'prewarmed').length,
      viewersRecycled: active.filter(cell => cell.viewerSource === 'recycled').length,
    },
  );
  if (active.length) {
    const snapshot = previousCamera || active[0].plugin.canvas3d.camera.getSnapshot();
    const synchronizeCameras = async () => {
      if (FAST_GRID_CAMERA_SYNC_ENABLED) {
        for (const cell of active) {
          cell.plugin.canvas3d.camera.setState(
            cameraSnapshotForScene(snapshot, cell.cameraEnvelope),
            0,
          );
          cell.plugin.canvas3d.requestDraw?.();
        }
        try {
          plugin?.canvas3d?.camera?.setState?.(
            cameraSnapshotForScene(snapshot, plugin?.canvas3d?.camera?.getSnapshot?.()),
            0,
          );
          plugin?.canvas3d?.requestDraw?.();
        } catch (e) {}
        await nextAnimationFrame();
        return;
      }
      await Promise.all(active.map(cell => pinCameraSnapshot(
        cell.plugin, cameraSnapshotForScene(snapshot, cell.cameraEnvelope))));
      try {
        await pinCameraSnapshot(plugin, cameraSnapshotForScene(
          snapshot, plugin?.canvas3d?.camera?.getSnapshot?.()));
      } catch (e) {}
    };
    const reporter = cells[0]?.spec.performanceReporter;
    const timing = cells[0]?.spec.performanceTiming;
    if (reporter) {
      await reporter.measure(timing, 'grid-camera-finalize', synchronizeCameras, {
        fast: FAST_GRID_CAMERA_SYNC_ENABLED,
      });
    } else {
      await synchronizeCameras();
    }
    stopGridCameraSync = syncGridCameras(active);
  }
  view.classList.remove('loading-grid'); syncReviewState();
  startPendingQuestionPrefetch();
}

// ---- two layers: a protein/pocket context (fixed for classic questions; pose-specific for Weekly
//      one-at-a-time) and the rebuilt POSE layer (ligands + crystal-reveal). -----
let proteinData = [], layerData = [], hbondData = [], currentProteinKey = null;
function protUrls() {
  const answer = cur.revealed && cur.showAnswer;
  if (cur.item.source === 'weekly') {
    const vis = visibleChoices();
    const shown = vis[Math.min(shownOne, vis.length - 1)];
    // Show all starts from the prediction-set receptor medoid without pocket
    // sticks. Clicking a pose keeps the overlay but swaps to that exact
    // prediction's protein/pocket. One-at-a-time and Grid are already exact.
    if (displayMode === 'all' && !answer) {
      const context = cur.contextChoice;
      return context ? {
        prot: context.afprotein_file || cur.item.protein_file,
        pocket: context.afpocket_file || null,
      } : { prot: cur.item.protein_file, pocket: null };
    }
    if (answer && retrospectiveAnswerActive() && retrospectiveProteinFrame === 'folded'
        && displayMode === 'one' && shown && !isFixedReferenceChoice(shown)) {
      return {
        prot: shown.afprotein_file || cur.item.protein_file,
        pocket: shown.afpocket_file || cur.item.pocket_file,
      };
    }
    if (displayMode !== 'one' || answer) {
      return { prot: cur.item.protein_file, pocket: cur.item.pocket_file };
    }
    return {
      prot: shown?.afprotein_file || cur.item.protein_file,
      pocket: shown?.afpocket_file || cur.item.pocket_file,
    };
  }
  if (proteinMode === 'af3' && cur.item.afprotein_ref) {   // CAMEO only; RnP has no per-pose AF3 protein
    const vis = visibleChoices();
    const shown = vis[Math.min(shownOne, vis.length - 1)];
    if (displayMode === 'one' && !answer && shown && shown.afprotein_file)
      return { prot: shown.afprotein_file, pocket: shown.afpocket_file };
    return { prot: cur.item.afprotein_ref, pocket: cur.item.afpocket_union };
  }
  return { prot: cur.item.protein_file, pocket: cur.item.pocket_file };
}
async function buildProtein(shown) {    // rebuilds ONLY when the protein ensemble changes (no flicker)
  const { prot, pocket } = protUrls();
  const ghostProteinUrls = weeklyGhostProteinUrls(shown, prot);
  const proteinKey = JSON.stringify([prot, pocket, ghostProteinUrls, showSurface]);
  if (proteinKey === currentProteinKey) return;
  if (proteinData.length) { const b = plugin.build(); for (const x of proteinData) b.delete(x.ref || x); await b.commit(); proteinData = []; }
  const pr = await loadStruct(prot, 'pdb');
  proteinData.push(pr.data);
  const proteinColor = proteinMode === 'af3' ? AF3PROT : PROT;
  await addRep(pr.struct, 'polymer', 'cartoon', proteinColor, 0.5);
  if (showSurface) await addRep(pr.struct, 'polymer', 'molecular-surface', proteinColor, 0.7);
  for (const proteinUrl of ghostProteinUrls) {
    const ghostProtein = await loadStruct(proteinUrl, 'pdb');
    proteinData.push(ghostProtein.data);
    await addRep(ghostProtein.struct, 'polymer', 'cartoon', PROT, GHOST_PROTEIN_ALPHA);
  }
  if (pocket) {
    const ps = await loadStruct(pocket, 'pdb');
    proteinData.push(ps.data);
    await addSticks(ps.struct, 0.16, 0.95);
  }
  currentProteinKey = proteinKey;
}
async function clearLayer() {
  if (!layerData.length && !hbondData.length) return;
  const b = plugin.build();
  for (const d of layerData) b.delete(d.ref || d);
  for (const d of hbondData) b.delete(d.ref || d);
  await b.commit();
  layerData = []; hbondData = [];
}
// Mol* computes interactions within one structure. Build one pocket+ligand structure per pose and render
// only contacts crossing from that ligand component to its parent pocket. Combining poses, or rendering
// the whole structure, creates artificial pose↔pose and pocket↔pocket interaction networks.
async function renderLigandInteractions(pdb, targetPlugin = plugin, onData = null) {
  const data = await targetPlugin.builders.data.rawData({ data: pdb });
  onData?.(data);
  const traj = await targetPlugin.builders.structure.parseTrajectory(data, 'pdb');
  const model = await targetPlugin.builders.structure.createModel(traj);
  const struct = await targetPlugin.builders.structure.createStructure(model);
  const ligand = await targetPlugin.builders.structure.tryCreateComponentStatic(struct, 'ligand');
  if (!ligand) throw new Error('Interaction ligand component could not be built');
  await targetPlugin.builders.structure.representation.addRepresentation(ligand, {
    type: 'interactions',
    typeParams: { includeParent: true, parentDisplay: 'between' },
  });
}
async function buildInteractions(pocket, poseUrls, targetPlugin = plugin, onData = null) {
  if (!pocket || !poseUrls.length) return;
  const pocketPdb = await fetchPdbText(pocket);
  for (const poseUrl of poseUrls) {
    const pdb = mergeRetrospectiveInteractionPdb({
      pocketPdb,
      ligandPdb: await fetchPdbText(poseUrl),
    });
    await renderLigandInteractions(pdb, targetPlugin, onData);
  }
}
async function buildHbonds(poseUrls) {
  if (!showHbonds || !poseUrls.length) return;
  const { pocket } = protUrls();
  await buildInteractions(pocket, poseUrls, plugin, data => hbondData.push(data));
}
async function buildRetrospectiveInteractions(
  pocketPdb,
  poseOverlayPdb,
  crystalLigandPdb,
  targetPlugin = plugin,
  onData = null,
  { includePredicted = true } = {},
) {
  if (typeof pocketPdb !== 'string' || !pocketPdb.trim()) {
    throw new Error('Retrospective crystal pocket is missing');
  }
  const ligands = [];
  if (includePredicted && typeof poseOverlayPdb === 'string' && poseOverlayPdb) {
    ligands.push({ ligandPdb: poseOverlayPdb, chain: 'P', residueName: 'PRD' });
  }
  if (typeof crystalLigandPdb === 'string' && crystalLigandPdb) {
    ligands.push({ ligandPdb: crystalLigandPdb, chain: 'Q', residueName: 'XTL' });
  }
  if (!ligands.length) throw new Error('Retrospective interaction ligand is missing');
  for (const ligand of ligands) {
    await renderLigandInteractions(
      mergeRetrospectiveInteractionPdb({ pocketPdb, ...ligand }),
      targetPlugin,
      onData,
    );
  }
}
async function buildRetrospectiveHbonds(shown, targetPlugin = plugin, onData = data => hbondData.push(data)) {
  retrospectiveHbondStatus = '';
  if (!showHbonds || !itemHasReleasedCrystal(cur?.item)) return;
  let built = 0;
  for (const layer of weeklyPoseLayers(shown).filter(entry => !entry.ghost)) {
    const choice = layer.choice;
    if (isTrainingReferenceChoice(choice)) continue;
    if (isXtalReferenceChoice(choice)) {
      const pocketPdb = choice.answer_crystal_pocket_pdb;
      if (typeof pocketPdb !== 'string' || !pocketPdb) {
        retrospectiveHbondStatus = 'H-bonds unavailable: crystal pocket artifact is missing.';
        continue;
      }
      try {
        await buildRetrospectiveInteractions(
          pocketPdb,
          null,
          choice.answer_crystal_pdb,
          targetPlugin,
          onData,
          { includePredicted: false },
        );
        built += 1;
      } catch (error) {
        retrospectiveHbondStatus = `H-bonds unavailable: ${error.message}`;
        console.warn('Retrospective Xtal H-bonds omitted:', error.message);
      }
      continue;
    }
    const pocketPdb = choice.answer_crystal_pocket_pdb;
    if (typeof pocketPdb !== 'string' || !pocketPdb) {
      retrospectiveHbondStatus = 'H-bonds unavailable: crystal pocket artifact is missing.';
      continue;
    }
    try {
      await buildRetrospectiveInteractions(
        pocketPdb,
        choice.answer_overlay_pdb,
        choice.answer_crystal_pdb,
        targetPlugin,
        onData,
      );
      built += 1;
    } catch (error) {
      retrospectiveHbondStatus = `H-bonds unavailable: ${error.message}`;
      console.warn('Retrospective H-bonds omitted:', error.message);
    }
  }
  if (showHbonds && !built && !retrospectiveHbondStatus) {
    retrospectiveHbondStatus = 'H-bonds unavailable: no interaction data could be built.';
  }
}
async function buildRetrospectiveXtalLayer(preserveCamera = true) {
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await clearViewerScene();
    const xtal = buildXtalReferenceChoice(cur.item);
    poseChoiceByRepresentation = new WeakMap();
    await addRetrospectiveCrystalContext(
      plugin,
      (data, kind) => { (kind === 'protein' ? proteinData : layerData).push(data); },
      [xtal.answer_crystal_pdb],
      representation => registerPoseClickTarget(representation, xtal),
    );
    await addRetrospectiveCrystalPocketSticks(xtal, plugin, data => proteinData.push(data));
    await buildRetrospectiveHbonds([xtal]);
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
async function buildRetrospectiveTrainingLayer(training, preserveCamera = true) {
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await clearViewerScene();
    poseChoiceByRepresentation = new WeakMap();
    const context = visibleChoices()[0] || null;
    if (context?.answer_crystal_pdb) {
      await addRetrospectiveCrystalContext(
        plugin,
        (data, kind) => { (kind === 'protein' ? proteinData : layerData).push(data); },
        [context.answer_crystal_pdb],
      );
      await addRetrospectiveCrystalPocketSticks(
        context,
        plugin,
        data => proteinData.push(data),
      );
    }
    const loaded = await addTrainingReferencePose(
      training,
      plugin,
      data => layerData.push(data),
      { surface: showSurface },
    );
    if (!loaded) throw new Error('Closest training pose could not be rendered');
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
async function buildRetrospectiveCanonicalLayer(shown, preserveCamera = true) {
  if (shown.length === 1 && isXtalReferenceChoice(shown[0])) {
    return buildRetrospectiveXtalLayer(preserveCamera);
  }
  if (shown.length === 1 && isTrainingReferenceChoice(shown[0])) {
    return buildRetrospectiveTrainingLayer(shown[0], preserveCamera);
  }
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await clearViewerScene();
    poseChoiceByRepresentation = new WeakMap();
    const poseLayers = weeklyPoseLayers(shown);
    const crystalChoiceByPdb = new Map();
    for (const layer of poseLayers) {
      if (!crystalChoiceByPdb.has(layer.choice.answer_crystal_pdb)) {
        crystalChoiceByPdb.set(layer.choice.answer_crystal_pdb, layer.choice);
      }
    }
    const xtalClickChoice = displayMode === 'all' ? buildXtalReferenceChoice(cur.item) : null;
    await addRetrospectiveCrystalContext(plugin, (data, kind) => {
      (kind === 'protein' ? proteinData : layerData).push(data);
    }, poseLayers.map(layer => layer.choice.answer_crystal_pdb), (representation, crystalPdb) => {
      registerPoseClickTarget(representation, xtalClickChoice || crystalChoiceByPdb.get(crystalPdb));
    });
    const contextChoice = displayMode === 'all' ? cur.contextChoice : null;
    if (displayMode !== 'all' || contextChoice) {
      const pocketChoice = contextChoice
        || poseLayers.find(layer => !layer.ghost)?.choice
        || poseLayers[0]?.choice;
      await addRetrospectiveCrystalPocketSticks(
        pocketChoice,
        plugin,
        data => proteinData.push(data),
      );
    }
    for (const layer of poseLayers) {
      const c = layer.choice;
      if (typeof c.answer_overlay_pdb !== 'string' || !c.answer_overlay_pdb) {
        throw new Error(`Retrospective pose overlay is missing for ${c._weeklyChoiceId || c.id}`);
      }
      const pose = await loadStructText(c.answer_overlay_pdb, 'pdb');
      layerData.push(pose.data);
      const poseColor = answerViewPoseCorrect(c) ? GOOD : BAD;
      const representation = await addPose(
        pose.struct,
        poseColor,
        plugin,
        layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined,
      );
      let surfaceRepresentation = null;
      if (showSurface && !layer.ghost) {
        surfaceRepresentation = await addRep(pose.struct, 'all', 'molecular-surface', poseColor, 0.7);
      }
      registerPoseClickTarget(representation, c);
      registerPoseClickTarget(surfaceRepresentation, c);
    }
    const hbondChoices = displayMode === 'all'
      ? (contextChoice ? [contextChoice] : [])
      : shown;
    await buildRetrospectiveHbonds(hbondChoices);
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
async function buildRetrospectiveFoldedCanonicalLayer(shown, preserveCamera = true) {
  const contextChoice = displayMode === 'all' && cur.contextChoice
    && !isFixedReferenceChoice(cur.contextChoice) ? cur.contextChoice : null;
  const c = contextChoice || shown.find(choice => !isFixedReferenceChoice(choice));
  if (!c) return buildRetrospectiveCanonicalLayer(shown, preserveCamera);
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await clearViewerScene();
    poseChoiceByRepresentation = new WeakMap();
    const urls = gridProteinUrls(c, { item: cur.item, proteinMode });
    const aligned = await alignedFoldedAssets(c, urls);
    const protein = await loadStructText(aligned.proteinPdb, 'pdb');
    proteinData.push(protein.data);
    await addRep(protein.struct, 'polymer', 'cartoon', urls.color, 0.5);
    if (showSurface) {
      await addRep(protein.struct, 'polymer', 'molecular-surface', urls.color, 0.7);
    }
    if (aligned.pocketPdb) {
      const pocket = await loadStructText(aligned.pocketPdb, 'pdb');
      proteinData.push(pocket.data);
      await addSticks(pocket.struct, 0.16, 0.95);
    }
    const poseLayers = weeklyPoseLayers(shown);
    const crystalPdbs = [...new Set(poseLayers.map(layer => layer.choice.answer_crystal_pdb))];
    const xtalClickChoice = buildXtalReferenceChoice(cur.item);
    for (const crystalPdb of crystalPdbs) {
      const crystal = await loadStructText(crystalPdb, 'pdb');
      layerData.push(crystal.data);
      const representation = await addCrystalPose(crystal.struct);
      registerPoseClickTarget(representation, xtalClickChoice);
      if (showSurface) await addRep(crystal.struct, 'all', 'molecular-surface', XTAL, 0.7);
    }
    const poseSpheres = [];
    for (const layer of poseLayers) {
      const pose = await loadStructText(layer.choice.answer_overlay_pdb, 'pdb');
      layerData.push(pose.data);
      const poseColor = answerViewPoseCorrect(layer.choice) ? GOOD : BAD;
      const representation = await addPose(
        pose.struct,
        poseColor,
        plugin,
        layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined,
      );
      registerPoseClickTarget(representation, layer.choice);
      if (showSurface && !layer.ghost) {
        const surface = await addRep(pose.struct, 'all', 'molecular-surface', poseColor, 0.7);
        registerPoseClickTarget(surface, layer.choice);
      }
      if (!layer.ghost) poseSpheres.push(structureSphere(pose.struct));
    }
    if (showHbonds && aligned.pocketPdb) {
      try {
        await buildRetrospectiveInteractions(
          aligned.pocketPdb,
          c.answer_overlay_pdb,
          c.answer_crystal_pdb,
          plugin,
          data => hbondData.push(data),
        );
      } catch (error) {
        retrospectiveHbondStatus = `H-bonds unavailable: ${error.message}`;
        console.warn('Folded-protein H-bonds omitted:', error.message);
      }
    }
    if (!preservedCamera) focusLigandSpheres(plugin, poseSpheres);
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
async function buildCanonicalLayer(shown, preserveCamera = true) {
  const retrospective = retrospectiveAnswerActive();
  const foldedOne = displayMode === 'one' && retrospectiveProteinFrame === 'folded';
  const foldedShowAll = displayMode === 'all' && cur.contextChoice
    && !isFixedReferenceChoice(cur.contextChoice);
  const foldedRetrospective = retrospective && (foldedOne || foldedShowAll)
    && shown.some(choice => !isFixedReferenceChoice(choice));
  if (foldedRetrospective && itemHasReleasedCrystal(cur.item)) {
    return buildRetrospectiveFoldedCanonicalLayer(shown, preserveCamera);
  }
  if (retrospective && itemHasReleasedCrystal(cur.item) && !foldedRetrospective) {
    return buildRetrospectiveCanonicalLayer(shown, preserveCamera);
  }
  let preservedCamera = preserveCamera ? nextCanonicalCameraSnapshot : null;
  nextCanonicalCameraSnapshot = null;
  if (preserveCamera && !preservedCamera) {
    try { preservedCamera = plugin.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  }
  const releaseCamera = holdCameraSnapshot(plugin, preservedCamera);
  try {
    syncStageBadge();
    await buildProtein(shown);           // swap protein only if it changed (AF3 one-at-a-time, or toggle)
    await clearLayer();
    poseChoiceByRepresentation = new WeakMap();
    const answer = cur.revealed && cur.showAnswer;      // green/red reveal vs the anonymised "my view"
    for (const layer of weeklyPoseLayers(shown).filter(
      entry => !isFixedReferenceChoice(entry.choice),
    )) {
      const c = layer.choice;
      const s = await loadStruct(c.pose_file, 'pdb');
      layerData.push(s.data);
      const poseColor = layer.rejected ? REJECTED_POSE
        : (answer ? (answerViewPoseCorrect(c) ? GOOD : BAD) : c.color);
      const representation = await addPose(s.struct,
        poseColor, plugin,
        layer.ghost ? { alpha: GHOST_POSE_ALPHA, sizeFactor: GHOST_POSE_SIZE } : undefined);
      let surfaceRepresentation = null;
      if (showSurface && !layer.ghost) {
        surfaceRepresentation = await addRep(s.struct, 'all', 'molecular-surface',
          poseColor, 0.7);
      }
      registerPoseClickTarget(representation, c);
      registerPoseClickTarget(surfaceRepresentation, c);
    }
    // crystal reference (true pose) — only after reveal, when toggled on
    const weeklyOverlayContext = cur.item.source === 'weekly' && displayMode === 'all' && !answer;
    const hbondPoses = weeklyOverlayContext
      ? (cur.contextChoice ? [cur.contextChoice.pose_file] : [])
      : shown.filter(c => !isFixedReferenceChoice(c)).map(c => c.pose_file);
    if (cur.revealed && showXtal && itemHasXtalOverlay(cur.item) && !viewingReleasedCrystal()
        && !retrospectiveAnswerActive()) {
      const xl = await loadStruct(cur.item.xtal_lig_file, 'pdb');
      layerData.push(xl.data);
      await addPose(xl.struct, XTAL);
      if (showSurface) await addRep(xl.struct, 'all', 'molecular-surface', XTAL, 0.7);
      hbondPoses.push(cur.item.xtal_lig_file); // also show the crystal reference's H-bonds when it's visible
    }
    await buildHbonds(hbondPoses);      // H-bond overlay for whatever pose(s) are currently shown
    await pinCameraSnapshot(plugin, preservedCamera);
    viewerTraceRecorder?.captureState();
  } finally {
    releaseCamera();
  }
}
async function buildSingleLayer(preserveCamera = true) {
  const answer = cur.revealed && cur.showAnswer;
  const vis = retrospectiveNavChoices();
  const showAnswerEnsemble = answer && !weeklyResultsRevealActive();
  const shown = showAnswerEnsemble || displayMode === 'all'
    ? visibleChoices()
    : [vis[Math.min(shownOne, vis.length - 1)]];
  return buildCanonicalLayer(shown, preserveCamera);
}
async function buildLayer() {
  const resetCamera = resetCameraOnNextBuild;
  resetCameraOnNextBuild = false;
  if (viewingReleasedCrystal()) {
    hideGrid();
    $('#stage')?.classList.remove('grid-active');
    try {
      await buildReleasedCrystalScene(!resetCamera);
      return;
    } catch (error) {
      console.warn('Released crystal could not be loaded:', error.message);
      releasedCrystalMode = false;
      showXtal = false;
      releasedCrystalError = 'Could not load the in-app crystal. Use “Open in RCSB” or try again.';
      syncXtalRow();
      syncButtons();
      return buildSingleLayer(false);
    }
  }
  if (displayMode === 'grid') {
    // Cover the canonical viewer before it is rebuilt with the Grid pose set;
    // otherwise One-at-a-time briefly flashes as Show all during the transition.
    $('#stage').classList.add('grid-active');
    $('#gridview').classList.add('on', 'loading-grid');
    // The canonical viewer is invisible in Grid. Rebuilding the same structures
    // there duplicated Mol* parse/representation work and kept controls locked
    // after all visible cards were ready. Non-Grid modes rebuild it lazily.
    await buildGrid(!resetCamera, false);
    return;
  }
  if ($('#gridview').classList.contains('on')) {
    gridBuildRevision++;
    try { return await buildSingleLayer(!resetCamera); }
    finally { hideGrid(); }
  }
  hideGrid();
  return buildSingleLayer(!resetCamera);
}

function requestQuestionCameraReset() {
  if (displayMode !== 'grid') plugin.canvas3d?.requestCameraReset?.();
}

async function loadQuestion(i) {
  pendingQuestionPrefetchIndexes = Array.from(
    { length: QUESTION_PREFETCH_LOOKAHEAD },
    (_, distance) => i + distance + 1,
  );
  const item = ITEMS[i];
  const loadStartedAt = Date.now();
  const performanceTiming = pendingQuestionPerformanceTiming
    || viewerPerformance.beginQuestion({
      itemId: item?.id || null,
      questionIndex: i,
      requestedMode: userView.displayMode,
      clustered: userView.clustered,
    });
  pendingQuestionPerformanceTiming = null;
  let loadSucceeded = false;
  const wrap = $('#wrap');
  wrap.classList.add('question-loading');
  $('#stage').classList.add('loading-system');
  $('#choices').style.display = 'none';
  $('#choices').replaceChildren();
  $('#answer-details').hidden = true;
  $('#answer-details').open = false;
  $('#answer-choices').replaceChildren();
  const savedWeeklyState = item.source === 'weekly' ? WEEKLY_ITEM_STATES.get(item.id) : null;
  // Keep a Weekly question's randomised labels and all local review state stable when navigating away/back.
  const clusters = savedWeeklyState?.clusters
    || WEEKLY_PREFETCHED_CLUSTERS.get(item.id)
    || buildQuestionClusters(item);
  if (item.source === 'weekly') WEEKLY_PREFETCHED_CLUSTERS.set(item.id, clusters);
  try {
    await viewerRebuild.enqueue(
    async () => {
      viewerTraceRecorder?.stop();
      void weeklyTraceStream?.endVisit?.('navigation');
      // A question owns its framing. Tear down the previous question's visible
      // camera publishers before rebuilding, and do not pin their snapshot
      // through the new canonical/Grid scene construction.
      resetCameraOnNextBuild = true;
      gridBuildRevision++;
      disposeGridViewers();
      idx = i;
      const gridMethods = savedWeeklyState?.gridMethods || (item.source === 'rnp'
        ? shuffle([...new Set(item.choices.map(c => c._method).filter(Boolean))]) : []);
      cur = savedWeeklyState || { item, clusters, gridMethods, selected: null, selectionExact: false,
        selectedAsCluster: false, contextChoice: null, answerChoices: [], revealed: false, showAnswer: false,
        rejectedChoiceIds: new Set(), voteCommentHandled: false, voteCommentText: null,
        pendingWeeklyVote: null };
      cur.item = item;
      const restoreWeeklyResult = !!(savedWeeklyState?.revealed && weeklyResultsRevealActive());
      if (!restoreWeeklyResult) {
        cur.revealed = false;
        cur.showAnswer = false;
      }
      if (item.source === 'weekly' && !savedWeeklyState) {
        restoreWeeklyPriorVote(cur, WEEKLY_VOTES.get(item.id), clusters);
      }
      gridMethodIndex = savedWeeklyState?.savedGridPage || 0;
      activePaneId = null;
      selectedPaneId = null;
      // Molecular surfaces are a deliberately question-local expensive opt-in.
      // Carrying them into a six-pane Grid rebuilt 19 surfaces and made an
      // observed transition take 20.3 s rather than 4.4 s. Preserve the chosen
      // layout and H-bonds, but begin each new question without surfaces.
      userView.showSurface = false;
      // Seed the remaining view preferences from the player's last choice,
      // then reset question-specific navigation/reveal state.
      applyUserView();
      shownOne = savedWeeklyState?.savedShownOne || 0;
      resetCrystalViewState();
      if (applyRetrospectiveAnswer()) {
        // A retrospective is an answer browser, not an unanswered ballot.
      } else if (restoreWeeklyResult && cur.showAnswer) {
        applyAnswerRevealView();
      } else if (restoreWeeklyResult) {
        cur.answerRevealBest = bestRawCorrectPose();
      } else if (!cur.revealed) {
        cur.answerRevealBest = null;
      }
      $('#myview').style.display = 'none'; $('#start').style.display = 'none';
      $('#instruction').style.display = isRetrospectiveReview() ? 'none' : '';
      $('#choices').style.display = '';
      $('#answer-details').hidden = true; $('#answer-details').open = false;
      $('#answer-choices').replaceChildren(); $('#answer-ai').textContent = '';
      try { await plugin.clear(); } catch (e) {}
      proteinData = []; layerData = []; hbondData = [];
      currentProteinKey = null;
      syncButtons();
    },
    async () => {
      // Grid cells settle and pin their own cameras. Waiting on the hidden
      // canonical viewer here can keep the question locked long after every
      // visible Grid card is ready.
      if (displayMode !== 'grid') {
        await window.waitForCameraSettled({
          cameraChanged: plugin.canvas3d?.camera?.changed,
          requestReset: requestQuestionCameraReset,
        });
      }
      weeklyTraceStream?.startVisit?.({ itemId: item.id, questionIndex: i });
      viewerTraceRecorder?.start({ appState: currentReplayableAppState() });
      recordAppEvent('question_loaded', {
        question_load_ms: Math.max(0, Date.now() - loadStartedAt),
      });
      renderUI();
      if (cur.revealed && weeklyResultsRevealActive()) renderRevealedQuestionUi();
      saveWeeklyResumePosition(i);
      requestAnimationFrame(() => requestAnimationFrame(() => $('#stage').classList.remove('loading-system')));
      startPendingQuestionPrefetch();
    },
    );
    loadSucceeded = true;
  } finally {
    wrap.classList.remove('question-loading');
    viewerPerformance.milestone(performanceTiming, 'question-ready');
    const completedPerformance = viewerPerformance.finishQuestion(performanceTiming, {
      itemId: item?.id || null,
      questionIndex: i,
      requestedMode: userView.displayMode,
      clustered: userView.clustered,
      status: loadSucceeded ? 'ready' : 'failed',
      mode: displayMode,
      gridCards: displayMode === 'grid' ? gridViewers.length : 0,
      viewerPoolEnabled: GRID_VIEWER_POOL_ENABLED,
      fastGridCameraSyncEnabled: FAST_GRID_CAMERA_SYNC_ENABLED,
      gridViewerPrewarmEnabled: GRID_VIEWER_PREWARM_ENABLED,
      gridViewersReused: displayMode === 'grid'
        ? gridViewers.filter(cell => cell.reusedViewer).length
        : 0,
      gridViewersPrewarmed: displayMode === 'grid'
        ? gridViewers.filter(cell => cell.viewerSource === 'prewarmed').length
        : 0,
      gridViewersRecycled: displayMode === 'grid'
        ? gridViewers.filter(cell => cell.viewerSource === 'recycled').length
        : 0,
      gridViewersCreated: displayMode === 'grid'
        ? gridViewers.filter(cell => cell.plugin && !cell.reusedViewer).length
        : 0,
      gridViewerPoolSize: gridViewerPool.size(),
    });
    recordPerformanceDiagnostics(completedPerformance);
  }
}

function renderUI() {
  hideActivePoseInfoTooltip();
  const filteredIndexes = retrospectiveQuestionIndexes();
  const filteredPosition = filteredIndexes.indexOf(idx);
  const questionOrdinal = isRetrospectiveReview() && filteredPosition >= 0
    ? `${filteredPosition + 1} / ${filteredIndexes.length}`
    : `${idx + 1} / ${ITEMS.length}`;
  $('#progress').textContent = DEV ? `item ${questionOrdinal} · dev` : `question ${questionOrdinal}`;
  const rawPoseCount = cur.clusters.reduce((total, cluster) => total + cluster.members.length, 0);
  const poseSummary = cur.item.source === 'weekly'
    ? (cur.item.clustering_available
      ? `${rawPoseCount} predicted poses · ${cur.clusters.length} pose clusters`
      : `${rawPoseCount} predicted poses`)
    : `${cur.clusters.length} distinct pose clusters`;
  renderViewerQuestionTitle(poseSummary);
  const alignmentWarning = cur.item.source === 'weekly'
    ? cur.item.alignment_warning?.message
    : null;
  $('#instruction').textContent = alignmentWarning
    || (cur.item.source === 'weekly'
      ? weeklyViewerInstruction()
      : 'Pick the pose that best fits the binding pocket.');
  $('#instruction').classList.toggle('alignment-warning', !!alignmentWarning);
  $('#instruction').style.display = alignmentWarning || !isRetrospectiveReview() ? '' : 'none';
  const box = $('#choices'); box.innerHTML = '';
  const uiEntries = choiceEntriesForSidebar();
  const retrospectiveAnswer = retrospectiveAnswerActive();
  uiEntries.forEach(entry => {
    const c = entry.choice, k = entry.choiceIndex;
    const b = document.createElement('button');
    const xtalReference = isXtalReferenceChoice(c);
    const trainingReference = isTrainingReferenceChoice(c);
    const fixedReference = xtalReference || trainingReference;
    const exactEntryChoices = retrospectiveAnswer && !fixedReference
      ? exactChoicesForEntry(entry) : [];
    b.className = 'choice'
      + (choiceRejected(c) ? ' rejected' : '')
      + (retrospectiveAnswer && !fixedReference
        ? (exactEntryChoices.length ? ' correct' : ' wrong') : '');
    b.dataset.k = k; b.disabled = viewerTransitionBusy;
    b.style.setProperty('--choice-color', hex(c.color));
    let nm;
    if (trainingReference) {
      nm = `${trainingReferenceAnnotation(c)} <span class="pose-count">REFERENCE</span>`;
    } else if (xtalReference) {
      nm = 'Xtal reference';
    } else if (clustered) {
      const cl = entry.cluster;
      const label = cl.label;
      const count = displayMode === 'grid' ? entry.memberCount : cl.members.length;
      nm = `Pose ${label}` + (count > 1
        ? ` <span class="pose-count">${count} poses</span>` : '');
    } else nm = `Pose ${c.label}`;
    b.innerHTML = `<span class="sw" style="background:${hex(c.color)}"></span><span class="nm">${nm}</span><span class="tag" data-tag></span>`;
    if (retrospectiveAnswer) {
      const tag = b.querySelector('[data-tag]');
      tag.classList.add('answer-status');
      tag.textContent = fixedReference
        ? 'REFERENCE'
        : (exactEntryChoices.length ? 'CORRECT' : 'WRONG');
    }
    attachPoseInfo(b, weeklyEntryEvidence(entry));
    b.onclick = () => pickSidebarEntry(entry);
    box.appendChild(b);
  });
  if (difficulty === 'hard') {                          // the detect-game option
    const nb = document.createElement('button');
    const noneCorrect = retrospectiveAnswer && !allItemChoices().some(rawChoiceCorrect);
    nb.className = 'choice none'
      + (retrospectiveAnswer ? (noneCorrect ? ' correct' : ' wrong') : '');
    nb.dataset.k = 'none'; nb.disabled = viewerTransitionBusy;
    nb.style.setProperty('--choice-color', '#5a6675');
    nb.innerHTML = `<span class="sw" style="background:#5a6675;border-style:dashed"></span><span class="nm">None are correct</span><span class="tag" data-tag></span>`;
    if (retrospectiveAnswer) {
      const tag = nb.querySelector('[data-tag]');
      tag.classList.add('answer-status');
      tag.textContent = noneCorrect ? 'CORRECT' : 'WRONG';
    }
    nb.onclick = () => onPick('none');
    box.appendChild(nb);
  }
  if (cur.selected) {                                   // keep the player's pick highlighted
    let selected;
    if (cur.selected.none) selected = box.querySelector('.choice.none');
    else {
      const k = uiEntries.findIndex(entry => cur.selectionExact
        ? sameChoice(entry.choice, cur.selected) : entry.choice.cluster === cur.selected.cluster);
      if (k >= 0) selected = box.querySelectorAll('.choice')[k];
    }
    selected?.classList.add('sel');
    const tag = selected?.querySelector('[data-tag]');
    if (tag) tag.textContent = 'Selected ✓';
  }
  box.style.display = cur.revealed && cur.showAnswer ? 'none' : '';
  $('#vote-comment-enabled').checked = weeklyCommentPromptEnabled;
  $('#vote-comment-option').style.display = quizSource === 'weekly' && !DEV && !isRetrospectiveReview()
    && !cur.revealed ? 'flex' : 'none';
  if (isRetrospectiveReview()) renderWeeklyLeaderboard();
  if (quizSource === 'weekly' && WEEKLY_ROUND?.public_status !== 'revealed') {
    $('#lock').textContent = isRetrospectiveReview()
      ? 'Show result'
      : (WEEKLY_VOTES.has(cur.item.id) ? 'Update vote' : 'Record vote');
  }
  syncQuestionNavigation();
  if (DEV) { renderDevNav(); return; }                  // dev: free browse, no vote/lock/score
  $('#lock').disabled = viewerTransitionBusy || cur.selected == null; $('#lock').style.display = cur.revealed ? 'none' : '';
  $('#verdict').style.display = cur.revealed && !isRetrospectiveReview() ? '' : 'none';
  if (!cur.revealed) delete $('#verdict').dataset.state;
  $('#next').style.display = quizSource !== 'weekly' && cur.revealed ? '' : 'none';
  updateScore();
}

// dev-only chrome: Prev/Next that work on every item (no lock), + the reveal-answer toggle. The score panel
// and the verdict box stay hidden; nothing is logged.
function renderDevNav() {
  $('#lock').style.display = 'none';
  $('#verdict').style.display = 'none';
  const useQuestionNav = quizSource === 'weekly';
  $('#prev').style.display = useQuestionNav ? 'none' : '';
  $('#next').style.display = useQuestionNav ? 'none' : '';
  $('#next').textContent = 'Next →';
  syncQuestionNavigation();
  $('#myview').style.display = '';
  $('#myview').textContent = cur.showAnswer ? '← Hide answer (my view)' : 'Reveal answer →';
  syncXtalRow();
  $('#answer-details').hidden = !cur.showAnswer;
}

// items for the current (source, difficulty) selection. Easy = only game-able (a real pick puzzle).
// Hard = everything: game-able + all-wrong + all-correct (all-correct excluded from Easy — a pick with
// no wrong answer is no puzzle; it belongs in Hard as the positive control for the "none of these" call).
function filteredPool() {
  if (quizSource === 'weekly') return POOLS.weekly;
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
  $('#wrap').classList.add('intro');
  if (!DEV) $('#badge').textContent = quizSource === 'weekly'
    ? 'reference available Wednesday · pose details on hover'
    : 'crystal reference hidden · poses anonymised';
  $('#setup').style.display = '';
  $('#participant-setup').style.display = DEV || isRetrospectiveReview() ? 'none' : '';
  $('#vote-comment-option').style.display = 'none';
  $('#mode').style.display = 'none'; $('#protmode').style.display = 'none'; $('#modehint').style.display = 'none';
  $('#choices').innerHTML = ''; $('#lock').style.display = 'none'; $('#uncluster').style.display = 'none';
  $('#one-review-actions').hidden = true;
  $('#hbonds').style.display = 'none'; $('#surface').style.display = 'none';
  $('#protein-ensemble').style.display = 'none';
  $('#myview').style.display = 'none'; $('#xtalrow').style.display = 'none';
  $('#question-head').style.display = 'none'; $('#ligand').style.display = 'none';
  $('#instruction').style.display = 'none'; $('#view-options').hidden = true;
  $('#answer-details').hidden = true; $('#verdict').style.display = 'none';
  $('#progress').textContent = 'ready';
  $('#start').textContent = 'Start →';
  if ($('#name-status').textContent === 'Preparing quiz…') $('#name-status').textContent = '';
  if (quizSource === 'weekly') {
    const status = WEEKLY_ROUND?.public_status;
    const closes = WEEKLY_ROUND?.closes_at ? new Date(WEEKLY_ROUND.closes_at).toLocaleString() : 'Wednesday';
    const showRevealedModes = status === 'revealed' && !isRetrospectiveReview();
    const modes = $('#revealed-weekly-modes');
    if (modes) modes.hidden = !showRevealedModes;
    const answerMix = $('#weekly-answer-mix');
    if (answerMix) {
      const poseAnswerCount = pool.filter(item => item.has_correct).length;
      answerMix.textContent = showRevealedModes
        ? `${pool.length} questions · ${poseAnswerCount} with a correct pose · ${
          pool.length - poseAnswerCount
        } where “None” is correct`
        : '';
    }
    const retrospectiveLink = $('#current-retrospective-link');
    if (retrospectiveLink && WEEKLY_ROUND?.round_id) {
      retrospectiveLink.href = `/weekly?retrospective_round=${encodeURIComponent(WEEKLY_ROUND.round_id)}`;
    }
    const nameHint = $('#participant-name-hint');
    if (nameHint) {
      nameHint.textContent = isArchivePlayForFun()
        ? 'Enter a player name to join this round’s separate Play for fun leaderboard.'
        : (showRevealedModes
          ? 'Enter a player name to activate Play for fun. Your name labels only your separate post-reveal votes.'
          : 'Shown on the results leaderboard after release.');
      nameHint.classList.toggle('action-required', showRevealedModes);
    }
    $('#ligand').innerHTML = isArchivePlayForFun()
      ? `${pool.length} historical weekly ensembles`
      : `${pool.length} prospective weekly ensembles`;
    $('#setuphint').innerHTML = isRetrospectiveReview()
      ? `${pool.length} retrospective questions.`
      : (status === 'revealed'
        ? `${pool.length} ${
          isArchivePlayForFun() ? 'published Weekly questions' : 'prospective weekly ensembles'
        } · results are available; new votes are recorded as post-reveal and excluded from blind-week scores.`
        : (status === 'open'
          ? `${pool.length} prospective weekly ensembles · voting is open until ${closes}; results arrive Wednesday.`
          : `${pool.length} prospective weekly ensembles · voting is closed while Wednesday results are prepared.`));
    $('#start').style.display = pool.length
      && (isRetrospectiveReview() || (status !== 'closed' && !showRevealedModes)) ? '' : 'none';
    syncStartGate();
    return;
  }
  $('#revealed-weekly-modes').hidden = true;
  $('#participant-name-hint').textContent = 'Shown on the results leaderboard after release.';
  $('#participant-name-hint').classList.remove('action-required');
  $('#setuphint').textContent = pool.length ? `${pool.length} questions available` : 'No questions available';
  $('#start').style.display = pool.length ? '' : 'none';
  syncStartGate();
}

function escapeSelectorText(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);
}

function readSelectorIdentityFields() {
  return {
    displayName: $('#selector-display-name')?.value.trim().replace(/\s+/g, ' ') || '',
    methodName: $('#selector-method-name')?.value.trim() || '',
    methodVersion: $('#selector-method-version')?.value.trim() || '',
    provider: $('#selector-provider')?.value.trim() || '',
    model: $('#selector-model')?.value.trim() || '',
    modelVersion: $('#selector-model-version')?.value.trim() || '',
    promptProfileId: $('#selector-prompt-profile-id')?.value.trim() || '',
    promptSha256: $('#selector-prompt-sha256')?.value.trim() || '',
    toolsSha256: $('#selector-tools-sha256')?.value.trim() || '',
    configSha256: $('#selector-config-sha256')?.value.trim() || '',
    networkPolicy: $('#selector-network-policy')?.value || '',
    networkAllowlistSha256: $('#selector-network-allowlist-sha256')?.value.trim() || '',
  };
}

function setProgrammaticVotingStatus(message = '') {
  const status = $('#programmatic-voting-status');
  if (status) status.textContent = message;
}

function syncProgrammaticVotingPanel() {
  const panel = $('#programmatic-voting');
  if (!panel) return;
  panel.hidden = !WEEKLY_ONLY || !PROGRAMMATIC_VOTING
    || !$('#wrap')?.classList.contains('intro');
  const open = SELECTOR_ROUND_DESCRIPTOR?.public_status === 'open';
  for (const selector of ['#selector-create-token', '#selector-submit-file', '#selector-submission-file']) {
    const control = $(selector);
    if (control) control.disabled = !open;
  }
}

async function getBrowserSupabaseAccessToken() {
  const config = window.FOLDARIUM_SUPABASE;
  if (!config?.enabled || !config.url || !config.publishableKey) return null;
  try {
    const { createClient } = await import(SUPABASE_ESM);
    const client = createClient(config.url, config.publishableKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
    const current = await client.auth.getSession();
    if (current.error) throw current.error;
    if (current.data.session?.access_token) return current.data.session.access_token;
    const created = await client.auth.signInAnonymously();
    if (created.error) throw created.error;
    return created.data.session?.access_token || null;
  } catch (error) {
    console.warn('Selector token auth unavailable:', error.message);
    return null;
  }
}

async function loadProgrammaticVotingDescriptor() {
  if (!WEEKLY_ONLY || !PROGRAMMATIC_VOTING) return;
  setProgrammaticVotingStatus('');
  try {
    const response = await fetch('/api/weekly-selector/rounds/current', { cache: 'no-store' });
    if (!response.ok) throw new Error('Selector API is unavailable.');
    const descriptor = await response.json();
    if (
      descriptor?.kit?.schema_version !== 'foldarium.weekly-selector-kit/v2'
      || !descriptor?.round_id
      || !descriptor?.environment
      || !descriptor?.blind_manifest_sha256
    ) {
      throw new Error('The explicit dual-mode v2 selector round is unavailable.');
    }
    SELECTOR_ROUND_DESCRIPTOR = descriptor;
    if ($('#selector-prompt-profile-id')) {
      $('#selector-prompt-profile-id').value = descriptor.prompt_profile?.prompt_profile_id || '';
    }
    if ($('#selector-prompt-sha256')) {
      $('#selector-prompt-sha256').value = descriptor.prompt_profile?.prompt_sha256 || '';
    }
    syncProgrammaticVotingPanel();
    setProgrammaticVotingStatus(
      descriptor.public_status === 'open'
        ? 'Dual-mode v2 voting is open. Submit both clustered and unclustered decisions.'
        : 'Dual-mode v2 submissions are closed for this round.',
    );
  } catch (error) {
    SELECTOR_ROUND_DESCRIPTOR = null;
    syncProgrammaticVotingPanel();
    setProgrammaticVotingStatus(error.message);
  }
}

async function downloadSelectorKit() {
  setProgrammaticVotingStatus('Preparing kit download…');
  try {
    const descriptor = SELECTOR_ROUND_DESCRIPTOR?.round_id
      ? SELECTOR_ROUND_DESCRIPTOR
      : await (async () => {
        const response = await fetch('/api/weekly-selector/rounds/current', { cache: 'no-store' });
        if (!response.ok) throw new Error('Selector API is unavailable.');
        const payload = await response.json();
        if (
          payload?.kit?.schema_version !== 'foldarium.weekly-selector-kit/v2'
          || !payload?.round_id
          || !payload?.environment
          || !payload?.blind_manifest_sha256
        ) {
          throw new Error('The explicit dual-mode v2 selector round is unavailable.');
        }
        SELECTOR_ROUND_DESCRIPTOR = payload;
        return payload;
      })();
    const response = await fetch(
      `/api/weekly-selector/kits/${encodeURIComponent(descriptor.round_id)}`,
      { cache: 'no-store' },
    );
    if (!response.ok) throw new Error('Kit download descriptor is unavailable.');
    const payload = await response.json();
    if (!payload?.download_url) throw new Error('Verified kit URL is missing.');
    if (
      payload.round_id !== descriptor.round_id
      || payload.environment !== descriptor.environment
      || payload.blind_manifest_sha256 !== descriptor.blind_manifest_sha256
      || payload.kit_sha256 !== descriptor.kit.kit_sha256
    ) {
      throw new Error('Verified kit descriptor does not match the active v2 round.');
    }
    window.open(payload.download_url, '_blank', 'noopener,noreferrer');
    setProgrammaticVotingStatus('Kit download opened in a new tab.');
  } catch (error) {
    setProgrammaticVotingStatus(error.message);
  }
}

async function createSelectorApiToken() {
  const identity = readSelectorIdentityFields();
  const digest = /^[0-9a-f]{64}$/;
  if (
    !identity.displayName || !identity.methodName || !identity.methodVersion
    || !identity.provider || !identity.model || !identity.modelVersion
    || !identity.promptProfileId
  ) {
    setProgrammaticVotingStatus(
      'Enter display name, method, provider, model, and their exact versions.',
    );
    return;
  }
  if (![identity.promptSha256, identity.toolsSha256, identity.configSha256].every(value => digest.test(value))) {
    setProgrammaticVotingStatus('Enter lowercase SHA-256 digests for the prompt, tools, and config.');
    return;
  }
  const emptyAllowlistSha256 = '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';
  if (
    !['none', 'provider-api-only'].includes(identity.networkPolicy)
    || !digest.test(identity.networkAllowlistSha256)
    || (
      identity.networkPolicy === 'none'
      && identity.networkAllowlistSha256 !== emptyAllowlistSha256
    )
    || (
      identity.networkPolicy === 'provider-api-only'
      && identity.networkAllowlistSha256 === emptyAllowlistSha256
    )
  ) {
    setProgrammaticVotingStatus('Select the inference network policy and enter its canonical allowlist SHA-256.');
    return;
  }
  if (SELECTOR_ROUND_DESCRIPTOR?.public_status !== 'open') {
    setProgrammaticVotingStatus('Dual-mode v2 submissions are not open.');
    return;
  }
  setProgrammaticVotingStatus('Creating API token…');
  try {
    const accessToken = await getBrowserSupabaseAccessToken();
    if (!accessToken) throw new Error('Sign in is unavailable for token creation.');
    const response = await fetch('/api/weekly-selector/tokens', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({
        round_id: SELECTOR_ROUND_DESCRIPTOR.round_id,
        environment: SELECTOR_ROUND_DESCRIPTOR.environment,
        display_name: identity.displayName,
        method_name: identity.methodName,
        method_version: identity.methodVersion,
        provider: identity.provider,
        model_name: identity.model,
        model_version: identity.modelVersion,
        prompt_profile_id: identity.promptProfileId,
        prompt_sha256: identity.promptSha256,
        tools_sha256: identity.toolsSha256,
        config_sha256: identity.configSha256,
        blindness_attestation: {
          schema_version: 'foldarium.selector-blindness-attestation/v1',
          workspace_policy: 'verified-kit-only',
          network_policy: identity.networkPolicy,
          network_allowlist_sha256: identity.networkAllowlistSha256,
          browser_enabled: false,
          web_search_enabled: false,
          external_retrieval_enabled: false,
          shared_cache_enabled: false,
        },
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload?.error || 'Token creation failed.');
    }
    if (
      typeof payload?.token !== 'string'
      || !payload.token
      || payload.round_id !== SELECTOR_ROUND_DESCRIPTOR.round_id
      || payload.environment !== SELECTOR_ROUND_DESCRIPTOR.environment
    ) {
      throw new Error('Token response is invalid.');
    }
    SELECTOR_API_TOKEN = payload.token;
    const input = $('#selector-api-token');
    if (input) input.value = payload.token;
    const copyButton = $('#selector-copy-token');
    if (copyButton) copyButton.disabled = false;
    setProgrammaticVotingStatus('Token created. Copy it now — it will not be shown again.');
  } catch (error) {
    setProgrammaticVotingStatus(error.message);
  }
}

async function copySelectorApiToken() {
  const token = SELECTOR_API_TOKEN || $('#selector-api-token')?.value || '';
  if (!token) {
    setProgrammaticVotingStatus('Create a token before copying.');
    return;
  }
  try {
    await navigator.clipboard.writeText(token);
    setProgrammaticVotingStatus('Token copied to clipboard.');
  } catch {
    setProgrammaticVotingStatus('Clipboard copy failed. Select the token and copy manually.');
  }
}

async function submitSelectorFile() {
  const token = SELECTOR_API_TOKEN || $('#selector-api-token')?.value || '';
  const file = $('#selector-submission-file')?.files?.[0];
  if (!token) {
    setProgrammaticVotingStatus('Create an API token before uploading.');
    return;
  }
  if (!file) {
    setProgrammaticVotingStatus('Choose a complete selector JSON file.');
    return;
  }
  if (file.size > 131_072) {
    setProgrammaticVotingStatus('Selector JSON exceeds the 128 KiB request limit.');
    return;
  }
  setProgrammaticVotingStatus('Validating and submitting complete dual-mode v2 JSON…');
  try {
    const body = JSON.parse(await file.text());
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      throw new Error('Selector JSON must contain one object.');
    }
    if (body.schema_version !== 'foldarium.selector-submission/v2') {
      throw new Error('Selector JSON must use foldarium.selector-submission/v2.');
    }
    if (SELECTOR_ROUND_DESCRIPTOR?.round_id
        && body.round_id !== SELECTOR_ROUND_DESCRIPTOR.round_id) {
      throw new Error('Selector JSON is for a different weekly round.');
    }
    if (SELECTOR_ROUND_DESCRIPTOR?.environment
        && body.environment !== SELECTOR_ROUND_DESCRIPTOR.environment) {
      throw new Error('Selector JSON is bound to a different environment.');
    }
    if (SELECTOR_ROUND_DESCRIPTOR?.blind_manifest_sha256
        && body.blind_manifest_sha256 !== SELECTOR_ROUND_DESCRIPTOR.blind_manifest_sha256) {
      throw new Error('Selector JSON is bound to a different blind manifest.');
    }
    if (SELECTOR_ROUND_DESCRIPTOR?.kit?.kit_sha256
        && body.kit_sha256 !== SELECTOR_ROUND_DESCRIPTOR.kit.kit_sha256) {
      throw new Error('Selector JSON is bound to a different kit.');
    }
    if (!Array.isArray(body.items) || !body.items.length || body.items.some(item => (
      !item || typeof item !== 'object'
      || !item.clustered || !['cluster', 'none'].includes(item.clustered.selection_kind)
      || !item.unclustered || !['exact', 'none'].includes(item.unclustered.selection_kind)
    ))) {
      throw new Error('Every item needs explicit clustered and unclustered v2 decisions.');
    }
    const response = await fetch('/api/weekly-selector/submissions', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.error || 'Selector submission failed.');
    if (
      !payload?.submission_id
      || !payload?.payload_digest
      || payload.round_id !== body.round_id
      || payload.environment !== body.environment
      || payload.blind_manifest_sha256 !== body.blind_manifest_sha256
      || payload.kit_sha256 !== body.kit_sha256
    ) {
      throw new Error('Selector receipt is invalid.');
    }
    setProgrammaticVotingStatus(
      `Complete dual-mode submission accepted · revision ${payload.revision_number} · receipt ${payload.submission_id}`,
    );
  } catch (error) {
    setProgrammaticVotingStatus(
      error instanceof SyntaxError ? 'Selector file is not valid JSON.' : error.message,
    );
  }
}

function formatSelectorScoreLine(row) {
  const identity = row.identity || {};
  const name = escapeSelectorText(identity.display_name || 'Selector');
  const method = escapeSelectorText(
    `${identity.provider || ''} ${identity.model_name || 'model'} ${identity.model_version || ''}`.trim(),
  );
  const cluster = row.clustered || {};
  const exact = row.unclustered || {};
  const clusterScore = `${cluster.correct}/${cluster.item_count}`;
  const exactScore = `${exact.correct}/${exact.item_count}`;
  const clusterPct = Number.isFinite(cluster.accuracy) ? `${Math.round(cluster.accuracy)}%` : '—';
  const exactPct = Number.isFinite(exact.accuracy) ? `${Math.round(exact.accuracy)}%` : '—';
  const clusterRank = Number.isInteger(cluster.rank) ? `#${cluster.rank} ` : '';
  const exactRank = Number.isInteger(exact.rank) ? `#${exact.rank} ` : '';
  const benchmark = row.participant_type === 'post_close_benchmark'
    ? ` · Post-close benchmark · requested ${escapeSelectorText(
      identity.benchmark?.requested_effort || 'unknown',
    )} effort`
    : '';
  return `<b>${name}</b> · ${method}${benchmark} · Cluster ${clusterRank}${clusterScore} (${clusterPct}) · Exact ${exactRank}${exactScore} (${exactPct})`;
}

function renderWeeklySelectorLeaderboard() {
  if (!WEEKLY_ONLY) return;
  const host = $('#weekly-selector-leaderboard');
  if (!host) return;
  const revealed = WEEKLY_ROUND?.public_status === 'revealed';
  if (!revealed) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  const rows = WEEKLY_RETROSPECTIVE_SUMMARY?.automated_entries;
  if (!Array.isArray(rows)) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  host.hidden = false;
  if (!rows.length) {
    host.innerHTML = '<p class="hint">No automated results are available.</p>';
    return;
  }
  const sorted = [...rows].sort((left, right) => (
    right.correct - left.correct
    || right.accuracy - left.accuracy
    || left.participant.localeCompare(right.participant)
  ));
  host.innerHTML = `<div class="weekly-selector-heading">Automated methods</div>${
    sorted.map(row => `<div class="weekly-selector-row"><b>${
      escapeSelectorText(row.participant)
    }</b> · ${row.correct}/${row.total} correct</div>`).join('')
  }`;
}

async function loadWeeklySelectorResults() {
  WEEKLY_SELECTOR_RESULTS_ERROR = '';
  if (!WEEKLY_ROUND?.round_id || WEEKLY_ROUND.public_status !== 'revealed') {
    WEEKLY_SELECTOR_RESULTS = null;
    renderWeeklySelectorLeaderboard();
    return;
  }
  try {
    const response = await fetch(
      `/api/weekly-selector-results?round_id=${encodeURIComponent(WEEKLY_ROUND.round_id)}`,
      { cache: 'no-store' },
    );
    if (!response.ok) throw new Error('Selector results are unavailable.');
    const payload = await response.json();
    if (
      payload?.format_version !== 'foldarium.weekly-selector-results/v2'
      || !Array.isArray(payload.rows)
      || !Array.isArray(payload.questions)
    ) {
      throw new Error('Selector results response is invalid.');
    }
    WEEKLY_SELECTOR_RESULTS = payload;
  } catch (error) {
    WEEKLY_SELECTOR_RESULTS = null;
    WEEKLY_SELECTOR_RESULTS_ERROR = error.message;
  }
  renderWeeklySelectorLeaderboard();
}

function renderWeeklyResultsStatus() {
  if (!WEEKLY_ONLY) return;
  const panel = $('#weekly-results');
  const copy = $('#weekly-results-copy');
  const heading = $('#weekly-results-heading');
  if (!panel || !copy) return;
  if (isRetrospectiveReview()) {
    if (heading) heading.textContent = 'Question result';
    panel.dataset.status = isPrivatePrecloseReview() ? 'private-review' : 'archive-review';
    copy.hidden = true;
    copy.textContent = '';
    renderWeeklyLeaderboard();
    return;
  }
  const revealed = WEEKLY_ROUND?.public_status === 'revealed';
  copy.hidden = revealed && !WEEKLY_LEADERBOARD_ERROR;
  if (heading) heading.textContent = revealed ? 'Blind-week results' : 'Results';
  panel.dataset.status = revealed ? 'revealed' : 'pending';
  if (WEEKLY_LEADERBOARD_ERROR) {
    copy.hidden = false;
    copy.textContent = WEEKLY_LEADERBOARD_ERROR;
  } else if (revealed && WEEKLY_LEADERBOARD) {
    copy.textContent = '';
  } else {
    copy.textContent = revealed
      ? ''
      : (DEV2_FEEDBACK.formatReleaseCountdown?.(WEEKLY_ROUND?.closes_at)
        || 'Results Wednesday.');
  }
  renderWeeklyLeaderboard();
  renderWeeklySelectorLeaderboard();
}

function formatWeeklyScoreLine({ displayName, correct, answered, total, accuracy, coverage, rank = null }) {
  const name = escapeLeaderboardText(displayName || 'Participant');
  const rankLabel = rank == null ? '' : `#${rank} · `;
  return `${rankLabel}<b>${name}</b> · ${correct}/${answered} correct`;
}

function escapeLeaderboardText(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);
}

function summarizePrivateRetrospective(items) {
  const metricValue = metric => (Number.isFinite(metric?.value) ? metric.value : null);
  const topChoice = (choices, valueForChoice, descending = true) => choices
    .map(choice => ({ choice, value: valueForChoice(choice) }))
    .filter(row => Number.isFinite(row.value))
    .sort((left, right) => {
      const scoreOrder = descending ? right.value - left.value : left.value - right.value;
      if (scoreOrder) return scoreOrder;
      return String(left.choice._weeklyChoiceId || left.choice.id || '')
        .localeCompare(String(right.choice._weeklyChoiceId || right.choice.id || ''));
    })[0]?.choice || null;
  const rows = (items || []).map(item => {
    const choices = (item.choices || []).filter(choice => !isFixedReferenceChoice(choice));
    const methods = {};
    for (const method of ['openfold3', 'boltz2']) {
      const methodChoices = choices.filter(choice => choice._method === method);
      const top = topChoice(methodChoices, choice => metricValue(choice._confidence));
      methods[method] = {
        generatedExact: methodChoices.some(choice => choice.correct === true),
        topExact: top?.correct === true,
        topAccepted: top?.accepted_correct === true,
        maxConfidence: top ? metricValue(top._confidence) : null,
      };
    }
    const sminaTop = topChoice(
      choices,
      choice => metricValue(choice._sminaScore),
      false,
    );
    const bestSmina = sminaTop ? metricValue(sminaTop._sminaScore) : null;
    return {
      hasExact: choices.some(choice => choice.correct === true),
      methods,
      sminaTopExact: sminaTop?.correct === true,
      sminaTopAccepted: sminaTop?.accepted_correct === true,
      sminaStrength: Number.isFinite(bestSmina) ? -bestSmina : null,
    };
  });
  const exactRows = rows.filter(row => row.hasExact);
  const noneRows = rows.filter(row => !row.hasExact);
  const percent = (count, total) => total ? Math.round(1000 * count / total) / 10 : 0;
  const mean = (group, valueForRow) => {
    const values = group.map(valueForRow).filter(Number.isFinite);
    return values.length
      ? Math.round(10 * values.reduce((sum, value) => sum + value, 0) / values.length) / 10
      : null;
  };
  const auc = valueForRow => {
    if (!exactRows.length || !noneRows.length) return null;
    let comparisons = 0;
    let wins = 0;
    for (const exact of exactRows) {
      const exactValue = valueForRow(exact);
      if (!Number.isFinite(exactValue)) continue;
      for (const none of noneRows) {
        const noneValue = valueForRow(none);
        if (!Number.isFinite(noneValue)) continue;
        comparisons += 1;
        wins += exactValue > noneValue ? 1 : (exactValue === noneValue ? 0.5 : 0);
      }
    }
    return comparisons ? Math.round(100 * wins / comparisons) / 100 : null;
  };
  const methods = {};
  for (const method of ['openfold3', 'boltz2']) {
    const generatedExact = exactRows.filter(row => row.methods[method].generatedExact).length;
    const topExact = exactRows.filter(row => row.methods[method].topExact).length;
    const topAccepted = exactRows.filter(row => row.methods[method].topAccepted).length;
    methods[method] = {
      generatedExact,
      generatedExactPercent: percent(generatedExact, exactRows.length),
      topExact,
      topExactPercent: percent(topExact, exactRows.length),
      topExactWhenGeneratedPercent: percent(topExact, generatedExact),
      topAccepted,
      topAcceptedPercent: percent(topAccepted, exactRows.length),
      exactMeanMaxConfidence: mean(exactRows, row => row.methods[method].maxConfidence),
      noneMeanMaxConfidence: mean(noneRows, row => row.methods[method].maxConfidence),
      availabilityAuc: auc(row => row.methods[method].maxConfidence),
    };
  }
  const sminaTopExact = exactRows.filter(row => row.sminaTopExact).length;
  const sminaTopAccepted = exactRows.filter(row => row.sminaTopAccepted).length;
  return {
    total: rows.length,
    exactSystems: exactRows.length,
    exactSystemsPercent: percent(exactRows.length, rows.length),
    noneSystems: noneRows.length,
    noneSystemsPercent: percent(noneRows.length, rows.length),
    methods,
    smina: {
      topExact: sminaTopExact,
      topExactPercent: percent(sminaTopExact, exactRows.length),
      topAccepted: sminaTopAccepted,
      topAcceptedPercent: percent(sminaTopAccepted, exactRows.length),
      availabilityAuc: auc(row => row.sminaStrength),
    },
  };
}

function renderPrivateRetrospectiveSummary(summary) {
  if (!summary?.total) {
    return '<p class="weekly-scorecard-empty">Benchmark summary is unavailable.</p>';
  }
  const denominator = summary.exactSystems;
  const of3 = summary.methods.openfold3;
  const boltz = summary.methods.boltz2;
  const methodRow = (label, detail, result) => `<div class="weekly-benchmark-row">
    <div><b>${label}</b><span>${detail}</span></div>
    <strong>${result.topExact}/${denominator} · ${result.topExactPercent}%</strong>
  </div>`;
  return `<div class="weekly-scorecard-section weekly-benchmark">
    <div class="weekly-scorecard-heading">Ensemble result</div>
    <div class="weekly-benchmark-hero">
      <strong>${summary.exactSystems}/${summary.total} · ${summary.exactSystemsPercent}%</strong>
      <span>systems had at least one exact pose (RMSD &lt; 1.5 Å)</span>
    </div>
    <p class="weekly-scorecard-note">${summary.noneSystems}/${summary.total} · ${summary.noneSystemsPercent}% had no exact pose in the ten-pose ensemble.</p>
    <div class="weekly-scorecard-heading">Exact top-ranked pose · among those ${denominator} systems</div>
    ${methodRow(
      'OpenFold3 confidence',
      `ranks its five poses; generated an exact pose in ${of3.generatedExact}/${denominator}`,
      of3,
    )}
    ${methodRow(
      'Boltz-2 confidence',
      `ranks its five poses; generated an exact pose in ${boltz.generatedExact}/${denominator}`,
      boltz,
    )}
    ${methodRow('Smina', 'ranks all ten poses by affinity', summary.smina)}
    <p class="weekly-scorecard-note">Cluster-accepted top-1 (the leaderboard rule): OpenFold3 ${of3.topAccepted}/${denominator} · ${of3.topAcceptedPercent}%; Boltz-2 ${boltz.topAccepted}/${denominator} · ${boltz.topAcceptedPercent}%; Smina ${summary.smina.topAccepted}/${denominator} · ${summary.smina.topAcceptedPercent}%.</p>
    <div class="weekly-scorecard-heading">Signal for “None”</div>
    <div class="weekly-scorecard-row signal"><b>Yes—strongest in OpenFold3 confidence.</b><br>
      Maximum ligand pLDDT averaged ${of3.exactMeanMaxConfidence} when an exact pose existed vs ${of3.noneMeanMaxConfidence} when none did (AUROC ${of3.availabilityAuc?.toFixed(2)}).
      Boltz-2: ${boltz.exactMeanMaxConfidence} vs ${boltz.noneMeanMaxConfidence} (AUROC ${boltz.availabilityAuc?.toFixed(2)}).
    </div>
    <p class="weekly-scorecard-note">Exploratory separation on these same 29 systems, not a held-out estimate.</p>
  </div>`;
}

function privateQuestionResult() {
  const itemId = cur?.item?.id;
  return itemId
    ? WEEKLY_QUESTION_RESULTS?.items?.find(item => item.item_id === itemId) || null
    : null;
}

function privateQuestionAnswerChoice(answer) {
  if (answer?.picked_none) return null;
  return allItemChoices().find(candidate => (
    candidate._weeklyChoiceId === answer?.choice_id || candidate.id === answer?.choice_id
  )) || null;
}

function privateQuestionAnswerLabel(answer) {
  if (answer?.picked_none) return 'None are correct';
  const choice = privateQuestionAnswerChoice(answer);
  if (!choice) return 'Unknown choice';
  if (answer.selection_kind === 'cluster') {
    return `Cluster ${clusterForChoice(choice)?.label || displayedPoseLabel(choice, true)}`;
  }
  return `Pose ${displayedPoseLabel(choice, false)}`;
}

function privateQuestionAnswerState(answer) {
  if (answer?.correct) return 'correct';
  const choice = privateQuestionAnswerChoice(answer);
  return choice?.clusterAccepted === true ? 'cluster-accepted' : '';
}

function renderPrivateQuestionResult(result) {
  if (!result) {
    return '<p class="weekly-scorecard-empty">Question votes are unavailable.</p>';
  }
  if (!result.answered_count) {
    return '<p class="weekly-scorecard-empty">No players answered this question.</p>';
  }
  const playerLabel = count => `${count} ${count === 1 ? 'player' : 'players'}`;
  const popular = result.answers.slice(0, 3);
  const names = values => values.length
    ? values.map(escapeLeaderboardText).join(', ')
    : 'None';
  const detailRows = popular.map(answer => `<div>
    <b>${escapeLeaderboardText(privateQuestionAnswerLabel(answer))}:</b>
    ${names(answer.display_names)}
  </div>`).join('');
  return `<div class="weekly-question-result">
    <div class="weekly-question-result-summary">
      <div><strong>${result.correct_count}/${result.answered_count}</strong>
        <span>players got this question right</span>
      </div>
      <details class="weekly-question-result-info">
        <summary aria-label="Show player names">Players</summary>
        <div class="weekly-question-result-popover">
          <div><b>Correct:</b> ${names(result.correct_display_names)}</div>
          ${detailRows}
        </div>
      </details>
    </div>
    <div class="weekly-question-result-heading">Most popular answers</div>
    <div class="weekly-question-result-ranking">
      ${popular.map((answer, index) => {
        const state = privateQuestionAnswerState(answer);
        return `<div class="weekly-question-result-answer">
          <span class="weekly-question-result-rank">${index + 1}</span>
          <b>${escapeLeaderboardText(privateQuestionAnswerLabel(answer))}</b>
          <span class="weekly-question-result-correct ${state || 'wrong'}">${state ? 'correct' : 'wrong'}</span>
          <span>${playerLabel(answer.vote_count)}</span>
        </div>`;
      }).join('')}
    </div>
  </div>`;
}

function archiveQuestionResult() {
  const itemId = cur?.item?.id;
  return itemId
    ? WEEKLY_ARCHIVE_DETAIL?.retrospective?.questions?.find(item => item.item_id === itemId) || null
    : null;
}

function renderArchiveQuestionResult(result) {
  if (!result) {
    return '<p class="weekly-scorecard-empty">Question results are unavailable.</p>';
  }
  const human = result.human_aggregate || {};
  const answers = (human.answers || []).slice().sort((left, right) => (
    right.vote_count - left.vote_count
  ));
  const humanRows = answers.map(answer => {
    const state = privateQuestionAnswerState(answer);
    return `<div class="weekly-question-result-answer">
      <span class="weekly-question-result-rank">·</span>
      <b>${escapeLeaderboardText(privateQuestionAnswerLabel(answer))}</b>
      <span class="weekly-question-result-correct ${state || 'wrong'}">${state ? 'correct' : 'wrong'}</span>
      <span>${answer.display_names?.length
        ? answer.display_names.map(escapeLeaderboardText).join(', ')
        : `${answer.vote_count} ${answer.vote_count === 1 ? 'answer' : 'answers'}`}</span>
    </div>`;
  }).join('');
  const automatedRows = (result.automated_entries || []).map((answer, index) => {
    const state = privateQuestionAnswerState(answer);
    return `<div class="weekly-question-result-answer">
      <span class="weekly-question-result-rank">${index + 1}</span>
      <b>${escapeLeaderboardText(answer.participant)}</b>
      <span class="weekly-question-result-correct ${state || 'wrong'}">${state ? 'correct' : 'wrong'}</span>
      <span>${escapeLeaderboardText(privateQuestionAnswerLabel(answer))}</span>
    </div>`;
  }).join('');
  const humanSummary = `<div><strong>${human.correct_count || 0}/${human.answered_count || 0}</strong>
      <span>player answers were correct</span>
    </div>`;
  const humanBody = humanRows || '<p class="weekly-scorecard-empty">No player answers.</p>';
  return `<div class="weekly-question-result">
    <div class="weekly-question-result-summary">
      ${humanSummary}
    </div>
    <div class="weekly-question-result-heading">Player answers</div>
    <div class="weekly-question-result-ranking">${humanBody}</div>
    <div class="weekly-question-result-heading">Automated answers</div>
    <div class="weekly-question-result-ranking">${automatedRows}</div>
  </div>`;
}

function renderWeeklyLeaderboard() {
  if (!WEEKLY_ONLY) return;
  const host = $('#weekly-leaderboard');
  if (!host) return;
  const revealed = WEEKLY_ROUND?.public_status === 'revealed' || isRetrospectiveReview();
  if (!revealed) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  host.hidden = false;
  if (isPrivatePrecloseReview()) {
    host.innerHTML = renderPrivateQuestionResult(privateQuestionResult());
    return;
  }
  if (isArchiveRetrospective()) {
    host.innerHTML = renderArchiveQuestionResult(archiveQuestionResult());
    return;
  }
  if (WEEKLY_LEADERBOARD_ERROR && !WEEKLY_LEADERBOARD) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  const total = WEEKLY_LEADERBOARD?.item_count || ITEMS.length || 0;
  const localName = participantDisplayName || 'You';
  const localAnswered = localWeeklyScore.answered;
  const localCorrect = localWeeklyScore.correct;
  const localAccuracy = localAnswered ? Math.round(100 * localCorrect / localAnswered) : null;
  const localCoverage = total ? Math.round(100 * localAnswered / total) : null;
  const sections = [];
  if (!$('#wrap')?.classList.contains('intro')) {
    sections.push(`<div class="weekly-scorecard-section">
      <div class="weekly-scorecard-heading">Your session</div>
      <div class="weekly-scorecard-row local">${formatWeeklyScoreLine({
        displayName: localName,
        correct: localCorrect,
        answered: localAnswered,
        total,
        accuracy: localAccuracy ?? 0,
        coverage: localCoverage ?? 0,
      })}</div>
      <p class="weekly-scorecard-note">For fun · recorded separately from blind-week rankings.</p>
    </div>`);
  }
  const complete = WEEKLY_LEADERBOARD?.complete_runs || [];
  const partial = WEEKLY_LEADERBOARD?.partial_runs || [];
  if (!WEEKLY_LEADERBOARD) {
    sections.push('<p class="weekly-scorecard-empty">Leaderboard is loading…</p>');
  } else if (!complete.length && !partial.length) {
    sections.push('<p class="weekly-scorecard-empty">No human players participated this week.</p>');
  } else {
    if (complete.length) {
      sections.push(`<div class="weekly-scorecard-section">
        <div class="weekly-scorecard-heading">Leaderboard</div>
        ${complete.map(row => `<div class="weekly-scorecard-row">${formatWeeklyScoreLine({
          displayName: row.display_name,
          correct: row.correct,
          answered: row.answered,
          total: row.total,
          accuracy: row.accuracy,
          coverage: row.coverage,
          rank: row.rank,
        })}</div>`).join('')}
      </div>`);
    } else {
      sections.push('<p class="weekly-scorecard-empty">No complete runs yet.</p>');
    }
    if (partial.length) {
      sections.push(`<div class="weekly-scorecard-section">
        <div class="weekly-scorecard-heading">Other players</div>
        ${partial.map(row => `<div class="weekly-scorecard-row partial">${formatWeeklyScoreLine({
          displayName: row.display_name,
          correct: row.correct,
          answered: row.answered,
          total: row.total,
          accuracy: row.accuracy,
          coverage: row.coverage,
        })}</div>`).join('')}
      </div>`);
    }
  }
  const forFunComplete = WEEKLY_FOR_FUN_LEADERBOARD?.complete_runs || [];
  const forFunPartial = WEEKLY_FOR_FUN_LEADERBOARD?.partial_runs || [];
  if (forFunComplete.length || forFunPartial.length) {
    const rows = [...forFunComplete, ...forFunPartial];
    sections.push(`<div class="weekly-scorecard-section">
      <div class="weekly-scorecard-heading">Play for fun</div>
      ${rows.map(row => `<div class="weekly-scorecard-row for-fun">${formatWeeklyScoreLine({
        displayName: `${row.display_name} · For fun`,
        correct: row.correct,
        answered: row.answered,
        total: row.total,
        accuracy: row.accuracy,
        coverage: row.coverage,
        rank: row.rank,
      })}</div>`).join('')}
      <p class="weekly-scorecard-note">Separate from the blind-week ranking.</p>
    </div>`);
  }
  host.innerHTML = `<div class="weekly-scorecard">${sections.join('')}</div>`;
}

function weeklyLeaderboardFromRetrospectiveSummary(publication) {
  const rows = publication?.summary?.human_entries;
  if (publication?.round_id !== WEEKLY_ROUND?.round_id
      || !Number.isInteger(publication?.item_count)
      || !Array.isArray(rows)
      || !Array.isArray(publication?.summary?.automated_entries)) {
    throw new Error('Published retrospective summary is invalid.');
  }
  const sorted = [...rows].sort((left, right) => (
    right.correct - left.correct
    || right.accuracy - left.accuracy
    || left.participant.localeCompare(right.participant)
  ));
  const toRun = row => ({
    display_name: row.participant,
    correct: row.correct,
    answered: row.answered,
    total: row.total,
    accuracy: row.accuracy,
    coverage: row.coverage,
  });
  const completeRuns = sorted
    .filter(row => row.complete === true)
    .map((row, index) => ({ ...toRun(row), rank: index + 1 }));
  const partialRuns = sorted
    .filter(row => row.complete !== true)
    .map(toRun);
  return {
    format_version: 'foldarium.weekly-leaderboard/v1',
    round_id: publication.round_id,
    item_count: publication.item_count,
    participant_count: rows.length,
    complete_runs: completeRuns,
    partial_runs: partialRuns,
  };
}

async function loadWeeklyPlayForFunLeaderboard() {
  WEEKLY_FOR_FUN_LEADERBOARD = null;
  if (!WEEKLY_ROUND?.round_id || WEEKLY_ROUND.public_status !== 'revealed') return;
  try {
    const query = new URLSearchParams({ round_id: WEEKLY_ROUND.round_id });
    const response = await fetch(`/api/weekly-play-for-fun-results?${query}`);
    const payload = await response.json().catch(() => null);
    if (!response.ok
        || payload?.format_version !== 'foldarium.weekly-play-for-fun-leaderboard/v1'
        || payload.round_id !== WEEKLY_ROUND.round_id
        || payload.item_count !== WEEKLY_ROUND.item_count
        || !Array.isArray(payload.complete_runs)
        || !Array.isArray(payload.partial_runs)
        || [...payload.complete_runs, ...payload.partial_runs].some(
          row => row?.participation_mode !== 'for_fun',
        )) {
      throw new Error('Play-for-fun leaderboard is invalid.');
    }
    WEEKLY_FOR_FUN_LEADERBOARD = payload;
  } catch (error) {
    console.warn('Play-for-fun leaderboard unavailable:', error.message);
  }
}

async function loadWeeklyLeaderboard({ bundleLeaderboard = null } = {}) {
  WEEKLY_LEADERBOARD_ERROR = '';
  if (bundleLeaderboard != null) {
    WEEKLY_RETROSPECTIVE_SUMMARY = null;
    WEEKLY_FOR_FUN_LEADERBOARD = null;
    try {
      window.foldariumPrivateReview?.validateWeeklyLeaderboard?.(
        bundleLeaderboard,
        { roundId: WEEKLY_ROUND?.round_id },
      );
      WEEKLY_LEADERBOARD = bundleLeaderboard;
    } catch (error) {
      WEEKLY_LEADERBOARD = null;
      WEEKLY_LEADERBOARD_ERROR = error.message;
    }
    renderWeeklyResultsStatus();
    return;
  }
  if (!WEEKLY_ROUND?.round_id || WEEKLY_ROUND.public_status !== 'revealed') {
    WEEKLY_LEADERBOARD = null;
    WEEKLY_FOR_FUN_LEADERBOARD = null;
    WEEKLY_RETROSPECTIVE_SUMMARY = null;
    renderWeeklyResultsStatus();
    return;
  }
  try {
    const response = await fetch('/api/weekly-retrospectives?limit=50');
    const payload = await response.json().catch(() => null);
    if (!response.ok
        || payload?.format_version !== 'foldarium.weekly-retrospective-list/v1'
        || !Array.isArray(payload.publications)) {
      throw new Error('Published retrospective list is unavailable.');
    }
    const publication = payload.publications.find(
      row => row.round_id === WEEKLY_ROUND.round_id,
    );
    if (!publication) throw new Error('Published retrospective is unavailable.');
    WEEKLY_RETROSPECTIVE_SUMMARY = publication.summary;
    WEEKLY_LEADERBOARD = weeklyLeaderboardFromRetrospectiveSummary(publication);
    window.foldariumPrivateReview?.validateWeeklyLeaderboard?.(
      WEEKLY_LEADERBOARD,
      { roundId: WEEKLY_ROUND.round_id },
    );
  } catch (error) {
    WEEKLY_LEADERBOARD = null;
    WEEKLY_RETROSPECTIVE_SUMMARY = null;
    WEEKLY_LEADERBOARD_ERROR = 'Published results are temporarily unavailable.';
    console.warn('Weekly leaderboard unavailable:', error.message);
  }
  await loadWeeklyPlayForFunLeaderboard();
  renderWeeklyResultsStatus();
}

function bumpLocalWeeklyScore(youRight) {
  if (!weeklyResultsRevealActive()) return;
  const itemId = cur?.item?.id;
  if (!itemId || localWeeklyScoredItems.has(itemId)) return;
  localWeeklyScoredItems.add(itemId);
  localWeeklyScore.answered += 1;
  if (youRight) localWeeklyScore.correct += 1;
  renderWeeklyLeaderboard();
}

function startWeeklyCountdown() {
  if (weeklyCountdownTimer) clearInterval(weeklyCountdownTimer);
  weeklyCountdownTimer = null;
  if (!WEEKLY_ONLY || WEEKLY_ROUND?.public_status === 'revealed') return;
  renderWeeklyResultsStatus();
  weeklyCountdownTimer = setInterval(renderWeeklyResultsStatus, 30_000);
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
  if (quizSource === 'weekly') return pool.slice();
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
function beginQuiz(initialQuestionIndex = 0) {
  const prepared = quizSource === 'weekly' ? WEEKLY_PREPARED_SESSION : null;
  ITEMS = prepared?.items || drawSession();
  WEEKLY_ITEM_STATES = new Map();
  WEEKLY_PREFETCHED_CLUSTERS = prepared?.prefetchedClusters || new Map();
  WEEKLY_PREPARED_SESSION = null;
  localWeeklyScore = { correct: 0, answered: 0 };
  localWeeklyScoredItems = new Set();
  weeklyCommentPromptEnabled = true;
  $('#vote-comment-enabled').checked = true;
  if (quizSource === 'rnp' || quizSource === 'weekly') proteinMode = 'crystal';
  rememberView();   // snapshot the starting view as the persisted baseline for this session
  $('#wrap').classList.remove('intro');
  $('#setup').style.display = 'none'; $('#participant-setup').style.display = 'none';
  $('#revealed-weekly-modes').hidden = true;
  $('#start').style.display = 'none'; $('#mode').style.display = '';
  $('#question-head').style.display = ''; $('#ligand').style.display = '';
  $('#instruction').style.display = isRetrospectiveReview() ? 'none' : '';
  $('#view-options').hidden = false;
  $('#instruction').textContent = quizSource === 'weekly'
    ? weeklyViewerInstruction()
    : 'Pick the pose that best fits the binding pocket.';
  $('#protmode').style.display = (quizSource === 'rnp' || quizSource === 'weekly') ? 'none' : '';
  $('#lbl-af3').textContent = oppLabel();
  $('#lock').textContent = quizSource === 'weekly'
    ? (isRetrospectiveReview() ? 'Show result'
      : (WEEKLY_ROUND?.public_status === 'revealed'
        ? 'Submit for-fun answer'
        : 'Record vote'))
    : 'Lock in answer';
  // Read-only Previews should still expose the dialog for visual/interaction
  // testing; only the database-backed Send action remains unavailable.
  $('#suggestion-open').disabled = WEEKLY_ROUND?.public_status === 'revealed'
    || !(remoteSessionId || isReadOnlyPreview());
  startWeeklyThinkingTrace();
  const questionIndex = Math.min(Math.max(0, initialQuestionIndex), Math.max(0, ITEMS.length - 1));
  loadQuestion(questionIndex);
  if (isArchiveRetrospective()) window.foldariumRevealArchiveReview?.();
}

async function resumeWeeklyQuizIfAvailable() {
  if (DEV || PERFORMANCE_RECORDING_REQUESTED || isReadOnlyPreview() || isRetrospectiveReview()
    || quizSource !== 'weekly' || !WEEKLY_ROUND?.round_id) return false;
  const store = window.foldariumWeeklySessionResume;
  const token = store?.read?.();
  if (!token) return false;
  const postReveal = WEEKLY_ROUND.public_status === 'revealed';
  const expectedPhase = postReveal ? 'post_reveal' : 'blind';
  if (token.round_id !== WEEKLY_ROUND.round_id || token.phase !== expectedPhase) {
    store.clear?.();
    return false;
  }
  try {
    const backend = researchBackend();
    if (!backend) throw new Error('Quiz persistence is unavailable.');
    const resumed = await backend.resumeNamedWeeklySession({
      sessionId: token.session_id,
      roundId: token.round_id,
      postReveal,
    });
    remoteSessionId = resumed.sessionId;
    weeklyTraceSessionSeed = {
      nextVisitOrdinal: resumed.nextVisitOrdinal,
      lastVisitStartedAt: resumed.lastVisitStartedAt,
    };
    participantDisplayName = '';
    beginQuiz(token.question_index);
    return true;
  } catch (error) {
    store.clear?.();
    remoteSessionId = null;
    participantDisplayName = '';
    weeklyTraceSessionSeed = null;
    console.warn('Weekly session could not be resumed:', error.message);
    return false;
  }
}

function normalizedParticipantName() {
  return $('#participant-name').value.trim().replace(/\s+/g, ' ');
}

function syncStartGate() {
  const button = $('#start');
  const playForFun = $('#play-for-fun-start');
  if (DEV || isRetrospectiveReview()) {
    button.disabled = false;
    if (playForFun) playForFun.disabled = false;
    return;
  }
  const input = $('#participant-name');
  const displayName = normalizedParticipantName();
  const disabled = !displayName || displayName.length > 80 || !input.checkValidity()
    || (PERFORMANCE_RECORDING_REQUESTED && !performanceDiagnosticsConsented());
  button.disabled = disabled;
  if (playForFun) playForFun.disabled = disabled;
}

function beginStartPerformanceTiming() {
  const item = WEEKLY_PREPARED_SESSION?.items?.[0] || null;
  pendingQuestionPerformanceTiming = viewerPerformance.beginQuestion({
    itemId: item?.id || null,
    questionIndex: 0,
    requestedMode: userView.displayMode,
    clustered: userView.clustered,
    includesStart: true,
  });
  return pendingQuestionPerformanceTiming;
}

async function startQuiz() {
  cancelGridViewerPrewarm();
  if (DEV || isRetrospectiveReview()) {
    beginStartPerformanceTiming();
    remoteSessionId = null;
    participantDisplayName = '';
    beginQuiz();
    return;
  }
  const input = $('#participant-name');
  const button = $('#start');
  const status = $('#name-status');
  const displayName = normalizedParticipantName();
  input.value = displayName;
  if (!input.checkValidity() || !displayName) {
    status.textContent = 'Enter a name.';
    input.focus();
    return;
  }
  if (PERFORMANCE_RECORDING_REQUESTED && !performanceDiagnosticsConsented()) {
    status.textContent = 'Consent is required to record beta performance diagnostics.';
    $('#performance-consent-checkbox')?.focus();
    return;
  }
  const startPerformanceTiming = beginStartPerformanceTiming();
  if (isReadOnlyPreview() || isRetrospectiveReview()) {
    remoteSessionId = null;
    participantDisplayName = displayName;
    beginQuiz();
    return;
  }
  button.disabled = true;
  status.textContent = 'Starting…';
  try {
    const backend = researchBackend();
    if (!backend) throw new Error('Quiz persistence is unavailable.');
    const postReveal = quizSource === 'weekly'
      && WEEKLY_ROUND?.public_status === 'revealed';
    remoteSessionId = await viewerPerformance.measure(
      startPerformanceTiming,
      'named-session-start',
      () => backend.startNamedSession({
        source: quizSource,
        difficulty,
        weeklyRoundId: quizSource === 'weekly' ? WEEKLY_ROUND?.round_id : null,
        displayName,
        initialAppState: quizSource === 'weekly'
          ? {
            ...currentReplayableAppState(),
            leaderboard_opt_in: true,
            leaderboard_name_version: 1,
            play_mode: postReveal ? 'for_fun' : 'blind_competitive',
            play_mode_version: 1,
            performance_diagnostics_opt_in: PERFORMANCE_RECORDING_REQUESTED,
            performance_diagnostics_version: PERFORMANCE_RECORDING_REQUESTED ? 1 : null,
          }
          : currentReplayableAppState(),
        postReveal,
      }),
      { source: quizSource },
    );
    if (!remoteSessionId) throw new Error('The quiz session was not created.');
    participantDisplayName = displayName;
    saveWeeklyResumePosition(0);
    beginQuiz();
  } catch (error) {
    remoteSessionId = null;
    participantDisplayName = '';
    pendingQuestionPerformanceTiming = null;
    viewerPerformance.finishQuestion(startPerformanceTiming, {
      status: 'failed',
      mode: 'starting',
      includesStart: true,
    });
    status.textContent = `Could not start a recorded quiz. ${error.message}`;
  } finally {
    syncStartGate();
  }
}

function fallbackSuggestionContext(appState) {
  let camera = null;
  try { camera = plugin?.canvas3d?.camera?.getSnapshot?.() || null; } catch (e) {}
  return {
    schema_version: 1,
    captured_at: new Date().toISOString(),
    app_state: appState,
    viewer_snapshot: {
      schema_version: 1,
      shared_camera: camera,
      viewer_state_omitted: 'recorder_unavailable',
    },
    deployment: {
      environment: window.FOLDARIUM_SUPABASE?.deploymentEnvironment || null,
      commit: window.FOLDARIUM_SUPABASE?.commitSha || null,
    },
  };
}

function captureSuggestionContext() {
  const appState = currentReplayableAppState();
  try {
    return viewerTraceRecorder?.captureContext?.(appState)
      || fallbackSuggestionContext(appState);
  } catch (error) {
    const context = fallbackSuggestionContext(appState);
    context.viewer_snapshot.viewer_state_omitted = `capture_failed:${error.name || 'Error'}`;
    return context;
  }
}

function openSuggestionDialog() {
  const status = $('#suggestion-status');
  status.textContent = remoteSessionId
    ? ''
    : (isReadOnlyPreview()
      ? 'Read-only Preview: you can inspect this dialog, but Send is disabled.'
      : 'Start a named quiz before sending a suggestion.');
  $('#suggestion-submit').disabled = !remoteSessionId;
  $('#suggestion-dialog').showModal();
  requestAnimationFrame(() => $('#suggestion-text').focus());
}

async function submitSuggestion(event) {
  event.preventDefault();
  const input = $('#suggestion-text');
  const button = $('#suggestion-submit');
  const status = $('#suggestion-status');
  const suggestionText = input.value.trim();
  if (!remoteSessionId) {
    status.textContent = 'Start the quiz first.';
    return;
  }
  if (!input.checkValidity() || !suggestionText) {
    status.textContent = 'Enter a suggestion.';
    input.focus();
    return;
  }
  button.disabled = true;
  status.textContent = 'Saving…';
  try {
    recordAppEvent('suggestion_submitted');
    const contextSnapshot = captureSuggestionContext();
    const backend = researchBackend();
    if (!backend) throw new Error('Suggestion persistence is unavailable.');
    await backend.submitUserSuggestion({
      sessionId: remoteSessionId,
      roundId: quizSource === 'weekly' ? WEEKLY_ROUND?.round_id : null,
      itemId: cur?.item?.id || null,
      suggestionText,
      contextSnapshot,
    });
    input.value = '';
    status.textContent = 'Suggestion saved. Thank you.';
    setTimeout(() => {
      if ($('#suggestion-dialog').open) $('#suggestion-dialog').close();
    }, 650);
  } catch (error) {
    status.textContent = `Suggestion was not saved. ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function onPick(k, exactChoice = null, {
  rebuildCameraSnapshot = null,
  preserveScene = false,
} = {}) {
  if (interactionBlocked()) return;
  invalidatePendingWeeklyVote();
  const answerChoices = displayMode === 'grid' ? allGridEntries().map(entry => entry.choice) : visibleChoices();
  if (k !== 'none' && displayMode === 'one') {
    const selected = cur.revealed ? null : visibleChoices()[k];
    const selectShownPose = () => {
      shownOne = k;
      if (!cur.revealed) {
        reviewChoiceIds(selected).forEach(id => cur.rejectedChoiceIds.delete(id));
        cur.selected = selected;
        cur.selectionExact = !clustered;
        cur.selectedAsCluster = clustered;
        cur.contextChoice = selected;
        cur.poseFocusChoice = selected;
        selectedPaneId = null;
        document.querySelectorAll('#choices .choice').forEach(el => {
          const on = el.dataset.k == k;
          el.classList.toggle('sel', on);
          const tag = el.querySelector('[data-tag]');
        if (tag) tag.textContent = on ? 'Selected ✓' : '';
        });
      }
    };
    if (preserveScene) selectShownPose();
    else await viewerRebuild.enqueue(selectShownPose);
    syncReviewState();
    recordAppEvent('choice_selected');
    return;
  }
  if (cur.revealed) return;  // my-view navigation is meaningful only in one-at-a-time mode
  if (k === 'none') {
    const chooseNone = () => {
      cur.selected = {
        none: true,
        correct: !answerChoices.some(acceptedChoiceCorrect),
        label: 'None of these',
      };
      cur.selectionExact = displayMode === 'grid' || !clustered;
      cur.selectedAsCluster = false;
      cur.contextChoice = null;
      cur.answerChoices = answerChoices;
      selectedPaneId = null;
    };
    if (displayMode === 'all' && cur.item.source === 'weekly') {
      await viewerRebuild.enqueue(chooseNone);
    } else chooseNone();
    document.querySelectorAll('#choices .choice').forEach(el => {
      const on = el.dataset.k === 'none';
      el.classList.toggle('sel', on);
      const tag = el.querySelector('[data-tag]');
      if (tag) tag.textContent = on ? 'Selected ✓' : '';
    });
    $('#lock').disabled = false;
    recordAppEvent('choice_selected');
    return;
  }
  const choice = exactChoice || visibleChoices()[k];
  const choosePose = () => {
    reviewChoiceIds(choice).forEach(id => cur.rejectedChoiceIds.delete(id));
    cur.selected = choice;
    cur.selectionExact = !clustered;
    cur.selectedAsCluster = clustered;
    cur.contextChoice = choice;
    cur.poseFocusChoice = choice;
    cur.answerChoices = answerChoices;
    selectedPaneId = exactChoice
      ? (gridViewers.find(cell => sameChoice(cell.entry.choice, exactChoice))?.paneId || selectedPaneId)
      : null;
    if (displayMode === 'all' && cur.item.source === 'weekly' && rebuildCameraSnapshot) {
      nextCanonicalCameraSnapshot = rebuildCameraSnapshot;
    }
  };
  if (displayMode === 'all' && cur.item.source === 'weekly') {
    await viewerRebuild.enqueue(choosePose);
  } else choosePose();
  document.querySelectorAll('#choices .choice').forEach(el => {
    const on = el.dataset.k == k;
    el.classList.toggle('sel', on);
    const tag = el.querySelector('[data-tag]');
    if (tag) tag.textContent = on ? 'Selected ✓' : '';
  });
  syncGridSelection();
  $('#lock').disabled = false;
  recordAppEvent('choice_selected');
}

function shouldPromptForVoteComment() {
  return quizSource === 'weekly'
    && WEEKLY_ROUND?.public_status !== 'revealed'
    && !isRetrospectiveReview()
    && weeklyCommentPromptEnabled
    && !cur?.voteCommentHandled;
}

function openVoteCommentDialog() {
  $('#vote-comment-status').textContent = '';
  $('#vote-comment-text').value = '';
  $('#vote-comment-dialog').showModal();
  requestAnimationFrame(() => $('#vote-comment-text').focus());
  recordAppEvent('vote_comment_prompted');
}

function skipVoteComment() {
  if (!cur) return;
  cur.voteCommentText = null;
  cur.voteCommentHandled = true;
  weeklyCommentPromptEnabled = false;
  $('#vote-comment-enabled').checked = false;
  $('#vote-comment-dialog').close();
  recordAppEvent('vote_comment_skipped');
  void reveal();
}

async function submitVoteComment(event) {
  event.preventDefault();
  const text = $('#vote-comment-text').value.trim();
  if (!text) { skipVoteComment(); return; }
  cur.voteCommentText = text;
  cur.voteCommentHandled = true;
  weeklyCommentPromptEnabled = true;
  $('#vote-comment-enabled').checked = true;
  $('#vote-comment-dialog').close();
  recordAppEvent('vote_comment_attached');
  void reveal();
}

async function reveal() {
  if (cur.selected == null || cur.revealed || revealRequested || viewerTransitionBusy) return;
  if (shouldPromptForVoteComment()) {
    openVoteCommentDialog();
    return;
  }
  recordAppEvent('lock_requested');
  revealRequested = true;
  $('#lock').disabled = true;
  syncQuestionNavigation();
  try {
    if (quizSource === 'weekly' && !isRetrospectiveReview() && !isReadOnlyPreview()) {
      setVoteStatus('Recording…', 'recording');
      await finalizeReveal();
    } else {
      await revealAfterIdle();
    }
  } finally {
    revealRequested = false;
    if (cur && !cur.revealed) $('#lock').disabled = cur.selected == null;
    syncQuestionNavigation();
  }
}

async function finalizeReveal() {
  if (cur.selected == null || cur.revealed) return;
  const postRevealVote = quizSource === 'weekly'
    && WEEKLY_ROUND?.public_status === 'revealed'
    && !isRetrospectiveReview()
    && !isReadOnlyPreview();
  if (quizSource === 'weekly' && !isRetrospectiveReview() && !isReadOnlyPreview()) {
    const saved = await finalizeWeeklyVote({ postReveal: postRevealVote });
    if (!saved || !postRevealVote) return;
  }
  const viewerTrace = viewerTraceRecorder?.stop({ appState: currentReplayableAppState() }) ?? null;
  await viewerRebuild.enqueue(() => {
    const keepGrid = displayMode === 'grid';
    cur.revealed = true; cur.showAnswer = true;
    if (weeklyResultsRevealActive()) {
      applyAnswerRevealView();
    } else if (!keepGrid) { displayMode = 'all'; clustered = false; }
    syncButtons();
  });
  const picked = cur.selected;
  const af3 = cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
  const youRight = picked.none ? !!picked.correct : acceptedChoiceCorrect(picked);
  const af3Right = !!(af3 && opponentChoiceCorrect(af3));
  score.n++; score.you += youRight; score.af3 += af3Right;
  bumpLocalWeeklyScore(youRight);
  const answerChoices = cur.answerChoices.length ? cur.answerChoices : cur.clusters.map(c => c.rep);
  const nCorrect = answerChoices.filter(acceptedChoiceCorrect).length;
  const opts = answerChoices.length + (difficulty === 'hard' ? 1 : 0);
  score.randExp += (nCorrect || (difficulty === 'hard' ? 1 : 0)) / opts;
  renderRevealedQuestionUi();
  updateScore();
  if (!isRetrospectiveReview() && !postRevealVote) logAnswer(picked, af3, viewerTrace);
  if (postRevealVote) {
    rememberWeeklyItemState();
    void loadWeeklyPlayForFunLeaderboard().then(renderWeeklyLeaderboard);
  }
}

function renderRevealedQuestionUi() {
  const picked = cur.selected;
  if (!picked) return;
  const af3 = cur.clusters.flatMap(c => c.members)
    .find(c => c.af3_sample === cur.item.plddt_pick_sample) || null;
  const youRight = picked.none ? !!picked.correct : acceptedChoiceCorrect(picked);
  const af3Right = !!(af3 && opponentChoiceCorrect(af3));
  renderRevealList(picked, af3);
  $('#lock').style.display = 'none'; $('#choices').style.display = 'none';
  const bestMatch = cur.answerRevealBest ?? bestRawCorrectPose();
  if (isRetrospectiveReview()) {
    const v = $('#verdict');
    v.style.display = 'none';
    v.textContent = '';
    $('#answer-ai').textContent = '';
    const details = $('#answer-details');
    details.hidden = false;
    details.open = true;
    details.dataset.privateReview = 'true';
    $('#next').style.display = 'none';
    $('#myview').style.display = 'none';
    syncXtalRow();
    return;
  }
  const detail = youRight
    ? (picked.none ? 'None of these poses was correct.'
                   : `Pose ${displayedPoseLabel(picked, cur.selectedAsCluster)} is ${picked.rmsd.toFixed(2)} Å from the crystal pose.`)
    : (bestMatch ? `Best crystal match: ${displayedPoseLabel(bestMatch, false)} (${bestMatch.rmsd.toFixed(2)} Å).`
               : 'None of these poses was correct.');
  const afMethod = (cur.item.source === 'rnp' && af3 && af3._method) ? ` (${methodName(af3._method)})` : '';
  const afMsg = af3
    ? `${oppLabel()} picked Pose ${displayedPoseLabel(af3, false)}${afMethod} — ${af3Right ? 'right' : 'wrong'}`
      + (!cur.item.has_correct ? ` (can’t answer “none”)` : '')
    : '';
  const v = $('#verdict'); v.style.display = '';
  const postRevealNote = quizSource === 'weekly' && WEEKLY_ROUND?.public_status === 'revealed'
    ? '<span class="post-reveal-vote-note">Post-reveal vote recorded separately from blind-week results.</span>'
    : '';
  v.innerHTML = `<strong style="color:${youRight ? 'var(--good)' : 'var(--bad)'}">${youRight ? 'Correct' : 'Not quite'}</strong>${detail}${postRevealNote}`;
  $('#answer-ai').textContent = afMsg;
  $('#answer-details').hidden = !cur.showAnswer;
  if (cur.showAnswer) $('#answer-details').open = false;
  $('#next').style.display = ''; $('#next').textContent = idx + 1 < ITEMS.length ? 'Next question →' : 'View final score →';
  $('#myview').style.display = '';
  $('#myview').textContent = cur.showAnswer
    ? '← Back to my view (hide answer)'
    : 'Show answer →';
  if (!cur.showAnswer) $('#choices').style.display = '';
  syncXtalRow();
}

async function finalizeWeeklyVote({ postReveal = false } = {}) {
  const picked = cur.selected;
  const choiceId = picked.none ? null : picked._weeklyChoiceId;
  const verdict = $('#verdict'); verdict.style.display = '';
  if (isReadOnlyPreview()) {
    viewerTraceRecorder?.stop({ appState: currentReplayableAppState() });
    cur.voteCommentHandled = false;
    cur.voteCommentText = null;
    rememberWeeklyItemState();
    if (idx + 1 < ITEMS.length) await loadQuestion(idx + 1);
    else {
      renderUI();
      verdict.style.display = '';
      verdict.innerHTML = '<b>Read-only Preview:</b> this vote was not saved. You can review questions with the arrows.';
    }
    return;
  }
  setVoteStatus('Recording…', 'recording');
  try {
    const backend = researchBackend();
    if (!backend) throw new Error('Weekly quiz persistence is unavailable.');
    if (!cur.pendingWeeklyVote) {
      recordAppEvent('vote_submitted');
      const traceCheckpoint = await weeklyTraceStream?.checkpoint?.('vote') ?? null;
      const continuousTrace = traceCheckpoint
        ? {
            visit_id: traceCheckpoint.visitId,
            through_sequence: traceCheckpoint.throughSequence,
          }
        : null;
      const appState = currentReplayableAppState({ continuousTrace });
      cur.pendingWeeklyVote = {
        voteAttemptId: newVoteAttemptId(),
        sessionId: remoteSessionId,
        roundId: WEEKLY_ROUND.round_id,
        itemId: cur.item.id,
        questionIndex: idx,
        choiceId,
        pickedNone: !!picked.none,
        // A durable continuous batch contains the same recorder entries. Retain the legacy
        // snapshot only as a safety fallback when neither IndexedDB nor the server accepted it.
        viewerTrace: !traceCheckpoint || traceCheckpoint.durable === false
          ? (viewerTraceRecorder?.snapshot?.(appState) ?? null)
          : null,
        appState,
        voteComment: cur.voteCommentText,
        postReveal,
      };
    }
    await backend.submitWeeklyVoteAttempt(cur.pendingWeeklyVote);
  } catch (error) {
    setVoteStatus(`Vote was not recorded. ${error.message}`, 'error');
    return;
  }
  setVoteStatus(idx + 1 < ITEMS.length
    ? 'Vote saved. Loading next question…'
    : 'Vote saved.', 'saved');
  recordAppEvent('vote_recorded');
  if (idx + 1 >= ITEMS.length) recordAppEvent('quiz_completed');
  viewerTraceRecorder?.stop({ appState: currentReplayableAppState() });
  void weeklyTraceStream?.endVisit?.(idx + 1 < ITEMS.length ? 'vote' : 'completion');
  WEEKLY_VOTES.set(cur.item.id, {
    item_id: cur.item.id,
    choice_id: choiceId,
    picked_none: !!picked.none,
  });
  cur.pendingWeeklyVote = null;
  cur.voteCommentHandled = false;
  cur.voteCommentText = null;
  if (postReveal) {
    rememberWeeklyItemState();
    return true;
  }
  rememberWeeklyItemState();
  if (idx + 1 < ITEMS.length) await loadQuestion(idx + 1);
  else {
    renderUI();
    verdict.style.display = '';
    verdict.innerHTML = '<b style="color:var(--good)">All votes recorded.</b> Review or revise them with the arrows.';
  }
  return true;
}

// after reveal: flip between the green/red answer and the original anonymised "my view" to study it
async function toggleAnswer() {
  if (DEV) return toggleAnswerDev();
  if (!cur.revealed || viewerTransitionBusy) return;
  await viewerRebuild.enqueue(
    () => {
      cur.showAnswer = !cur.showAnswer;
      if (cur.showAnswer) {
        if (weeklyResultsRevealActive()) applyAnswerRevealView();
        else if (userView.displayMode === 'grid') { displayMode = 'grid'; clustered = userView.clustered; }
        else { clustered = false; displayMode = 'all'; }
      }
      else { applyUserView(); shownOne = 0; resetCrystalViewState(); }
      syncButtons();
    },
    () => {
      if (cur.showAnswer) {
        renderRevealList(cur.selected, cur.clusters.flatMap(c => c.members).find(c => c.af3_sample === cur.item.plddt_pick_sample) || null);
      } else { renderUI(); }
      $('#myview').textContent = cur.showAnswer ? '← Back to my view (hide answer)' : 'Show answer →';
      syncXtalRow();
    },
  );
}

function renderRevealList(picked, af3) {
  const box = $('#answer-choices'); box.innerHTML = '';
  if ((picked && picked.none) || cur.item.source === 'weekly') {
    const selectedNone = !!(picked && picked.none);
    const noneCorrect = !cur.item.has_correct;
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'choice ' + (noneCorrect ? 'correct' : 'wrong');
    el.style.setProperty('--choice-color', noneCorrect ? 'var(--good)' : 'var(--bad)');
    const voteText = cur.item.source === 'weekly' && !isPrivatePrecloseReview()
      ? `${WEEKLY_TOTALS.get(`${cur.item.id}|none`) || 0} votes` : '';
    const status = [
      selectedNone && !isRetrospectiveReview() ? 'You' : '',
      voteText,
    ].filter(Boolean).join(' · ');
    el.innerHTML = '<span class="sw" style="background:#5a6675;border-style:dashed"></span>'
      + '<span class="nm"><span class="answer-choice-title">None of these</span>'
      + `<span class="answer-choice-status">${status}</span></span>`;
    box.appendChild(el);
  }
  answerRevealEntries().forEach(entry => {
    const c = entry.choice;
    const exact = entry.exact;
    const clusterAccepted = entry.clusterAccepted === true;
    const bestMatch = cur.answerRevealBest ?? bestRawCorrectPose();
    const isBestMatch = !!(bestMatch && sameChoice(c, bestMatch));
    const el = document.createElement('button');
    el.type = 'button';
    el.className = `choice ${exact ? 'correct' : (clusterAccepted ? 'cluster-accepted' : 'wrong')}`;
    el.style.setProperty(
      '--choice-color',
      exact ? 'var(--good)' : (clusterAccepted ? 'var(--gold)' : 'var(--bad)'),
    );
    const label = entry.grouped ? entry.cluster.label : c.label;
    const count = entry.grouped && entry.memberCount > 1
      ? `<span class="answer-choice-count">· ${entry.memberCount} poses</span>` : '';
    const status = isPrivatePrecloseReview() ? '' : [
        isBestMatch ? 'Best match' : '',
        c === picked && !isRetrospectiveReview() ? 'You' : '',
        c === af3 ? 'AI' : '',
        cur.item.source === 'rnp' && c._method ? methodName(c._method) : '',
        cur.item.source === 'weekly' ? `${c._weeklyVoteCount || 0} votes` : '',
      ].filter(Boolean).join(' · ');
    const swColor = exact ? hex(GOOD) : (clusterAccepted ? '#b77900' : hex(BAD));
    el.innerHTML = `<span class="sw" style="background:${swColor}"></span>`
      + `<span class="nm"><span class="answer-choice-title">Pose ${label}</span>${count}`
      + `<span class="answer-choice-status">${status}</span></span>`
      + `<span class="rmsd answer-rmsd">RMSD ${c.rmsd.toFixed(2)} Å</span>`;
    attachPoseInfo(el, weeklyEntryEvidence(entry));
    box.appendChild(el);
  });
}

function answerRevealEntries() {
  if (cur.item.source !== 'weekly') {
    return cur.clusters.flatMap(cluster => cluster.members.map(choice => ({
      choice,
      cluster,
      members: [choice],
      exact: answerViewPoseCorrect(choice),
      clusterAccepted: false,
      grouped: false,
      memberCount: 1,
    }))).sort((left, right) => left.choice.rmsd - right.choice.rmsd);
  }
  if (!clustered) {
    return cur.clusters.flatMap(cluster => cluster.members.map(choice => ({
      choice,
      cluster,
      members: [choice],
      exact: answerViewPoseCorrect(choice),
      clusterAccepted: false,
      grouped: false,
      memberCount: 1,
    })));
  }
  return cur.clusters.flatMap(cluster => {
    const correct = cluster.members
      .filter(answerViewPoseCorrect)
      .sort((left, right) => left.rmsd - right.rmsd);
    if (correct.length) {
      return cluster.members.map(choice => {
        const exact = answerViewPoseCorrect(choice);
        return {
          choice,
          cluster,
          members: [choice],
          exact,
          clusterAccepted: !exact,
          grouped: false,
          memberCount: 1,
        };
      });
    }
    return [{
      choice: cluster.rep,
      cluster,
      members: cluster.members,
      exact: false,
      clusterAccepted: false,
      grouped: true,
      memberCount: cluster.members.length,
    }];
  });
}

function updateScore() {
  const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
  $('#score-summary').textContent = `score ${score.you} / ${score.n}`;
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
    picked_correct: picked.none ? !!picked.correct : acceptedChoiceCorrect(picked),
    picked_rmsd: picked.none ? null : picked.rmsd,
    af3_pick_sample: af3 ? af3.af3_sample : -1,
    af3_correct: !!(af3 && acceptedChoiceCorrect(af3)),
    has_correct: !!cur.item.has_correct, n_clusters: cur.clusters.length, ts: Date.now() / 1000 };
  const log = JSON.parse(localStorage.getItem('poseQuizLog') || '[]');
  log.push(rec); localStorage.setItem('poseQuizLog', JSON.stringify(log));
  researchBackend()?.recordAnswer(remoteSessionId, idx, { ...rec, viewer_trace: viewerTrace });
}

function syncButtons() {
  document.querySelectorAll('#mode button').forEach(b => {
    const on = b.dataset.m === displayMode;
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on));
  });
  renderGridPages();
  // Crystal↔AF3 protein toggle: only meaningful for CAMEO (RnP items carry no per-pose AF3 protein).
  // Centralised here so every redraw path keeps it correct regardless of how we got into play.
  const inPlay = !!cur;
  $('#mode').style.display = inPlay ? '' : 'none';
  if (quizSource === 'rnp' || quizSource === 'weekly') proteinMode = 'crystal';
  $('#protmode').style.display = (inPlay && quizSource !== 'rnp' && quizSource !== 'weekly') ? '' : 'none';
  document.querySelectorAll('#protmode button').forEach(b => {
    const on = b.dataset.p === proteinMode;
    b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on));
  });
  const uc = $('#uncluster');
  uc.textContent = clustered ? 'Uncluster' : 'Recluster';
  uc.classList.toggle('on', !clustered);
  uc.setAttribute('aria-pressed', String(!clustered));
  uc.style.display = cur && cur.clusters.some(c => c.members.length > 1) ? '' : 'none';
  const hb = $('#hbonds');                       // H-bond overlay toggle (mirrors #uncluster styling/gating)
  hb.classList.toggle('on', showHbonds);
  hb.style.display = inPlay && !viewingReleasedCrystal() ? '' : 'none';
  hb.setAttribute('aria-pressed', String(showHbonds));
  hb.disabled = viewingReleasedCrystal();
  const hbondStatus = $('#hbond-status');
  if (hbondStatus) {
    hbondStatus.textContent = retrospectiveAnswerActive() && showHbonds
      ? retrospectiveHbondStatus
      : '';
  }
  const surface = $('#surface');
  surface.classList.toggle('on', showSurface);
  surface.style.display = inPlay && !viewingReleasedCrystal() ? '' : 'none';
  surface.setAttribute('aria-pressed', String(showSurface));
  surface.disabled = viewingReleasedCrystal();
  const proteinEnsemble = $('#protein-ensemble');
  const canShowProteinEnsemble = inPlay && cur.item.source === 'weekly'
    && ENABLE_PROTEIN_ENSEMBLE_EXPERIMENT && clustered
    && cur.clusters.some(cluster => cluster.members.length > 1);
  proteinEnsemble.classList.toggle('on', showProteinEnsemble);
  proteinEnsemble.textContent = showProteinEnsemble ? 'Hide ghost proteins' : 'Ghost proteins';
  proteinEnsemble.style.display = canShowProteinEnsemble ? '' : 'none';
  const modehint = $('#modehint');
  if (modehint) {
    modehint.textContent = '';
    modehint.style.display = 'none';
  }
  syncOneReviewState();
  syncStageBadge();
  document.querySelectorAll(
    '#mode button, #protmode button, #uncluster, #hbonds, #surface, #protein-ensemble',
  ).forEach(control => { control.disabled = viewerTransitionBusy || viewingReleasedCrystal(); });
}

function syncStageBadge() {
  if (DEV) return;
  const badge = $('#badge');
  if (!badge) return;
  if (typeof isPrivatePrecloseReview === 'function'
    && isPrivatePrecloseReview() && showXtal && itemHasReleasedCrystal(cur?.item)
    && !retrospectiveAnswerActive()) {
    badge.style.display = '';
    badge.textContent = `Predictions aligned to crystal protein · blue = experimental reference · RCSB ${
      cur.item.released_crystal.pdb_id
    }`;
    return;
  }
  if (viewingReleasedCrystal()) {
    const released = cur.item.released_crystal;
    badge.style.display = '';
    badge.textContent = isPrivatePrecloseReview()
      ? `Answer overlay · predictions aligned to crystal protein · RCSB ${released.pdb_id}`
      : `Released crystal · ${cur.item.ligand} · RCSB ${released.pdb_id}`;
    return;
  }
  if (retrospectiveAnswerActive()) {
    badge.style.display = displayMode === 'one' ? '' : 'none';
    if (displayMode !== 'one') return;
    if (displayMode === 'one') {
      const choices = retrospectiveNavChoices();
      const choice = choices[Math.min(shownOne, choices.length - 1)];
      if (isXtalReferenceChoice(choice)) {
        badge.textContent = 'Xtal reference · crystal protein · experimental · not scored';
        return;
      }
      if (isTrainingReferenceChoice(choice)) {
        badge.textContent = `${trainingReferenceAnnotation(choice)} · not scored`;
        return;
      }
      const evidence = [
        `${choice.rmsd.toFixed(2)} Å`,
        answerPoseStatus(choice),
        weeklyLigandPlddt(choice),
        weeklyHbondCount(choice),
      ].filter(Boolean);
      badge.textContent = `Pose ${displayedPoseLabel(choice)} · ${evidence.join(' · ')}`;
      return;
    }
  }
  if (cur?.revealed && cur.showAnswer && weeklyResultsRevealActive() && cur.answerRevealBest) {
    badge.style.display = '';
    badge.textContent = `Best crystal match · Pose ${displayedPoseLabel(cur.answerRevealBest, false)} · ${cur.answerRevealBest.rmsd.toFixed(2)} Å`;
    return;
  }
  if (cur?.revealed && cur.showAnswer && weeklyResultsRevealActive() && !cur.answerRevealBest) {
    badge.style.display = '';
    badge.textContent = 'No correct predicted pose';
    return;
  }
  const hideWeeklyOverlayBadge = cur?.item?.source === 'weekly' && displayMode !== 'one';
  badge.style.display = hideWeeklyOverlayBadge ? 'none' : '';
  if (hideWeeklyOverlayBadge) return;
  if (cur?.item?.source === 'weekly' && displayMode === 'one') {
    const choices = retrospectiveNavChoices();
    const choice = choices[Math.min(shownOne, choices.length - 1)];
    if (isXtalReferenceChoice(choice)) {
      badge.textContent = 'Xtal reference · experimental · not scored';
      return;
    }
    if (isTrainingReferenceChoice(choice)) {
      badge.textContent = `${trainingReferenceAnnotation(choice)} · not scored`;
      return;
    }
    const evidence = [weeklyLigandPlddt(choice), weeklyHbondCount(choice)].filter(Boolean);
    badge.textContent = `Pose ${displayedPoseLabel(choice)}${evidence.length ? ` · ${evidence.join(' · ')}` : ''}`;
    return;
  }
  badge.textContent = 'crystal reference hidden · poses anonymised';
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
        if (weeklyResultsRevealActive()) applyAnswerRevealView();
        else if (userView.displayMode === 'grid') { displayMode = 'grid'; clustered = userView.clustered; }
        else { clustered = false; displayMode = 'all'; }
      }
      else { applyUserView(); shownOne = 0; resetCrystalViewState(); }
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
  const revisableForFunSession = quizSource === 'weekly'
    && WEEKLY_ROUND?.public_status === 'revealed';
  if (!revisableForFunSession) researchBackend()?.completeSession(remoteSessionId);
  const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
  $('#ligand').textContent = 'Quiz complete';
  $('#instruction').style.display = 'none'; $('#view-options').hidden = true; $('#answer-details').hidden = true;
  $('#choices').innerHTML = ''; $('#lock').style.display = 'none'; $('#next').style.display = 'none';
  $('#uncluster').style.display = 'none'; $('#mode').style.display = 'none'; $('#protmode').style.display = 'none';
  $('#hbonds').style.display = 'none'; $('#protein-ensemble').style.display = 'none';
  $('#surface').style.display = 'none';
  $('#xtalrow').style.display = 'none'; $('#myview').style.display = 'none';
  $('#verdict').style.display = '';
  if (quizSource === 'weekly') {
    if (isRetrospectiveReview()) {
      $('#verdict').style.display = 'none';
      $('#verdict').textContent = '';
      return;
    }
    $('#verdict').innerHTML = isReadOnlyPreview()
      ? '<b>Read-only Preview complete.</b> No names or votes were saved.'
      : WEEKLY_ROUND?.public_status === 'revealed'
      ? '<b>Weekly results complete.</b> These scores use the Wednesday released coordinates.'
      : '<b>Your weekly votes are saved.</b> Return Wednesday for released-coordinate results.';
    return;
  }
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
  canonicalPoseClickSubscription?.unsubscribe?.();
  canonicalPoseClickSubscription = plugin.behaviors?.interaction?.click
    ?.subscribe(onCanonicalPoseInteraction) || null;
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
      viewerTraceRecorder = window.createViewerTraceRecorder({
        plugin,
        onEntry: entry => weeklyTraceStream?.recordEntry?.(entry),
        // Weekly continuous recording must keep semantic selection/rejection/vote
        // events even after the bounded legacy visual snapshot reaches its cap.
        shouldContinueSemanticStream: () => quizSource === 'weekly' && !!weeklyTraceStream,
      });
    } catch (error) {
      console.warn('Viewer recording disabled:', error.message);
    }
  }
  if (DEV) {                                            // browse/inspection mode banner + page title
    document.title = 'Foldarium · DEV browse';
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
  const normalizeWeekly = (round, voteTotals = WEEKLY_TOTALS) => {
    const blindItems = round?.blind_manifest?.items;
    if (!Array.isArray(blindItems)) return [];
    const revealItems = new Map((round?.reveal_manifest?.items || []).map(item => [item.id, item]));
    return blindItems.map(item => {
      const clusteringAvailable = item.choices.every(choice => (
        typeof choice.cluster_id === 'string'
        && choice.cluster_id.length > 0
        && typeof choice.is_rep === 'boolean'
      ));
      const revealChoices = new Map(
        (revealItems.get(item.id)?.choices || []).map(choice => [choice.id, choice]),
      );
      const choices = item.choices.map((choice, index) => {
        const reveal = revealChoices.get(choice.id) || {};
        return {
          ...choice,
          _weeklyChoiceId: choice.id,
          _weeklyVoteCount: Number(voteTotals.get(`${item.id}|${choice.id}`) || 0),
          af3_sample: index + 1,
          pose_file: choice.pose_uri,
          afprotein_file: choice.protein_uri || item.protein_uri,
          afpocket_file: choice.pocket_uri || item.pocket_uri,
          rmsd: typeof reveal.rmsd === 'number' ? reveal.rmsd : null,
          correct: typeof reveal.correct === 'boolean' ? reveal.correct : null,
          accepted_correct: typeof reveal.accepted_correct === 'boolean'
            ? reveal.accepted_correct : null,
          _method: choice.method || reveal.method || null,
          _methodVersion: choice.method_version || reveal.method_version || null,
          _confidence: choice.confidence || null,
          _sminaScore: choice.smina_score || null,
          _interactionCount: choice.interaction_count || null,
          cluster: choice.cluster_id || `choice-${index}`,
          is_rep: typeof choice.is_rep === 'boolean' ? choice.is_rep : true,
          plddt: choice.confidence?.metric === 'ligand_plddt'
            && Number.isFinite(choice.confidence.value) ? choice.confidence.value : 0,
        };
      });
      const ligand = typeof item.ligand === 'string'
        ? item.ligand : (item.ligand?.component_id || item.ligand?.name || 'ligand');
      const plddtPick = [...choices]
        .filter(choice => choice._confidence?.metric === 'ligand_plddt'
          && Number.isFinite(choice._confidence.value))
        .sort((left, right) => (
          right._confidence.value - left._confidence.value
          || left._weeklyChoiceId.localeCompare(right._weeklyChoiceId)
        ))[0] || null;
      return {
        id: item.id,
        ligand,
        week: item.week,
        protein_file: item.protein_uri || choices[0]?.afprotein_file,
        pocket_file: item.pocket_uri || choices[0]?.afpocket_file,
        afprotein_ref: choices[0]?.afprotein_file,
        afpocket_union: item.pocket_uri || choices[0]?.afpocket_file,
        choices,
        n_clusters: new Set(choices.map(choice => choice.cluster)).size,
        plddt_pick_sample: plddtPick?.af3_sample ?? -1,
        n_heavy: item.ligand?.heavy_atoms || HEAVY_MIN,
        source: 'weekly',
        clustering_available: clusteringAvailable,
        bucket: 'weekly',
        has_correct: choices.some(choice => choice.correct === true),
        easyPlayable: true,
        alignment_warning: item.metadata?.display_alignment || null,
      };
    }).filter(item => item.choices.length && item.protein_file);
  };
  const activateArchiveDetail = (detail, similarityReport = null) => {
    const expectedRoundId = window.FOLDARIUM_ARCHIVE_REVIEW?.round_id;
    if (!detail || detail.format_version !== 'foldarium.weekly-retrospective-detail/v1'
      || detail.round?.round_id !== expectedRoundId
      || detail.blind_manifest?.round_id !== expectedRoundId
      || detail.reveal_manifest?.round_id !== expectedRoundId
      || !Array.isArray(detail.answer_overlays)
      || !Array.isArray(detail.retrospective?.questions)) {
      throw new Error('Archive retrospective detail is invalid.');
    }
    const synthetic = {
      round_id: detail.round.round_id,
      campaign_id: detail.round.campaign_id,
      environment: 'production',
      public_status: 'revealed',
      opens_at: detail.round.opens_at,
      closes_at: detail.round.closes_at,
      revealed_at: detail.round.revealed_at,
      blind_manifest: detail.blind_manifest,
      reveal_manifest: detail.reveal_manifest,
      item_count: detail.round.item_count,
    };
    const totals = new Map();
    for (const question of detail.retrospective.questions) {
      for (const answer of question.human_aggregate?.answers || []) {
        totals.set(
          `${question.item_id}|${answer.picked_none ? 'none' : answer.choice_id}`,
          Number(answer.vote_count || 0),
        );
      }
    }
    WEEKLY_ROUND = synthetic;
    WEEKLY_ARCHIVE_DETAIL = detail;
    WEEKLY_TOTALS = totals;
    WEEKLY_VOTES = new Map();
    WEEKLY_ITEM_STATES = new Map();
    WEEKLY_PREFETCHED_CLUSTERS = new Map();
    WEEKLY_QUESTION_RESULTS = {
      items: detail.retrospective.questions.map(question => ({
        item_id: question.item_id,
        answered_count: question.human_aggregate?.answered_count || 0,
        correct_count: question.human_aggregate?.correct_count ?? null,
        suppressed: question.human_aggregate?.suppressed === true,
        answers: question.human_aggregate?.answers || [],
      })),
    };
    WEEKLY_LEADERBOARD = null;
    WEEKLY_LEADERBOARD_ERROR = '';
    remoteSessionId = null;
    weeklyTraceSessionSeed = null;
    retrospectiveQuestionFilter = 'all';
    retrospectiveSimilaritySort = 'default';
    localWeeklyScore = { correct: 0, answered: 0 };
    localWeeklyScoredItems = new Set();
    displayMode = 'grid';
    clustered = true;
    gridMethodIndex = 0;
    cur = null;
    const normalizedPool = window.foldariumPrivateReview.enrichPrivateWeeklyPool(
      normalizeWeekly(synthetic, totals),
      {
        blind_manifest: detail.blind_manifest,
        reveal_manifest: detail.reveal_manifest,
        answer_overlays: detail.answer_overlays,
      },
    );
    const similarityFor = window.foldariumWeeklyTrainingSimilarity?.weeklySimilarityRecord;
    POOLS.weekly = normalizedPool.map((item, publicationIndex) => ({
      ...item,
      publicationIndex,
      similarity: typeof similarityFor === 'function'
        ? similarityFor(similarityReport, detail.round.blind_week, item.id)
        : null,
    }));
    const banner = $('#archive-review-banner');
    if (banner) {
      banner.hidden = false;
      banner.dataset.active = 'true';
    }
    const back = $('#archive-review-back');
    if (back) back.href = `/weekly/retrospectives/${encodeURIComponent(expectedRoundId)}`;
  };
  const activateArchivePlayForFun = detail => {
    const expectedRoundId = window.FOLDARIUM_ARCHIVE_PLAY?.round_id;
    if (!detail || detail.format_version !== 'foldarium.weekly-retrospective-detail/v1'
      || detail.round?.round_id !== expectedRoundId
      || detail.blind_manifest?.round_id !== expectedRoundId
      || detail.reveal_manifest?.round_id !== expectedRoundId
      || !Array.isArray(detail.retrospective?.questions)) {
      throw new Error('Archive play-for-fun detail is invalid.');
    }
    const synthetic = {
      round_id: detail.round.round_id,
      campaign_id: detail.round.campaign_id,
      environment: 'production',
      public_status: 'revealed',
      opens_at: detail.round.opens_at,
      closes_at: detail.round.closes_at,
      revealed_at: detail.round.revealed_at,
      blind_manifest: detail.blind_manifest,
      reveal_manifest: detail.reveal_manifest,
      item_count: detail.round.item_count,
    };
    const totals = new Map();
    for (const question of detail.retrospective.questions) {
      for (const answer of question.human_aggregate?.answers || []) {
        totals.set(
          `${question.item_id}|${answer.picked_none ? 'none' : answer.choice_id}`,
          Number(answer.vote_count || 0),
        );
      }
    }
    WEEKLY_ROUND = synthetic;
    WEEKLY_ARCHIVE_DETAIL = null;
    WEEKLY_RETROSPECTIVE_SUMMARY = detail.retrospective;
    WEEKLY_TOTALS = totals;
    WEEKLY_VOTES = new Map();
    WEEKLY_ITEM_STATES = new Map();
    WEEKLY_PREFETCHED_CLUSTERS = new Map();
    WEEKLY_QUESTION_RESULTS = null;
    WEEKLY_LEADERBOARD = weeklyLeaderboardFromRetrospectiveSummary({
      round_id: detail.round.round_id,
      item_count: detail.round.item_count,
      summary: detail.retrospective,
    });
    WEEKLY_FOR_FUN_LEADERBOARD = null;
    WEEKLY_LEADERBOARD_ERROR = '';
    remoteSessionId = null;
    weeklyTraceSessionSeed = null;
    localWeeklyScore = { correct: 0, answered: 0 };
    localWeeklyScoredItems = new Set();
    displayMode = 'grid';
    clustered = true;
    gridMethodIndex = 0;
    cur = null;
    POOLS.weekly = normalizeWeekly(synthetic, totals).map((item, publicationIndex) => ({
      ...item,
      publicationIndex,
    }));
    const banner = $('#archive-review-banner');
    const back = $('#archive-review-back');
    if (banner && back) {
      banner.hidden = false;
      banner.dataset.active = 'true';
      back.href = `/weekly/retrospectives/${encodeURIComponent(expectedRoundId)}`;
      back.textContent = 'Play for fun · back to results';
    }
    $('#revealed-weekly-title').textContent = 'Past Weekly is revealed';
    $('#revealed-weekly-subtitle').textContent =
      `Blind week ${detail.round.blind_week} · for-fun scores are kept separate.`;
    $('#participant-name-hint').textContent =
      'Shown on this round’s Play for fun leaderboard.';
    $('#current-retrospective-link').href =
      `/weekly?retrospective_round=${encodeURIComponent(expectedRoundId)}`;
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
  try {
    if (isArchivePlayForFun()) {
      activateArchivePlayForFun(await window.FOLDARIUM_ARCHIVE_DETAIL_READY);
      const backend = researchBackend();
      if (backend) {
        const votes = await backend.getWeeklyVotes(WEEKLY_ROUND.round_id, {
          postReveal: true,
        }).catch(error => {
          console.warn('Play-for-fun vote restoration unavailable:', error.message);
          return [];
        });
        WEEKLY_VOTES = new Map(votes.map(vote => [vote.item_id, vote]));
      }
      void loadWeeklyPlayForFunLeaderboard().then(renderWeeklyResultsStatus);
    } else if (isArchiveRetrospective()) {
      activateArchiveDetail(
        await window.FOLDARIUM_ARCHIVE_DETAIL_READY,
        await window.FOLDARIUM_WEEKLY_TRAINING_SIMILARITY_READY,
      );
      window.foldariumApplyArchiveReviewDetail = async detail => {
        activateArchiveDetail(
          detail,
          await window.FOLDARIUM_WEEKLY_TRAINING_SIMILARITY_READY,
        );
        renderWeeklyResultsStatus();
        showIntro();
        await startQuiz();
      };
    } else {
      const backend = researchBackend();
      WEEKLY_ROUND = await viewerPerformance.measureStartup(
        'weekly-round-rpc',
        () => backend?.getWeeklyRound(),
      ) || null;
      if (WEEKLY_ROUND && backend) {
        const [votes, totals] = await Promise.all([
          backend.getWeeklyVotes(WEEKLY_ROUND.round_id, {
            postReveal: WEEKLY_ROUND.public_status === 'revealed',
          }).catch(error => {
            console.warn('Weekly vote restoration unavailable:', error.message); return [];
          }),
          WEEKLY_ROUND.public_status === 'revealed'
            ? backend.getWeeklyVoteTotals(WEEKLY_ROUND.round_id).catch(error => {
              console.warn('Weekly vote totals unavailable:', error.message); return [];
            })
            : Promise.resolve([]),
        ]);
        WEEKLY_VOTES = new Map(votes.map(vote => [vote.item_id, vote]));
        WEEKLY_TOTALS = new Map(totals.map(total => [
          `${total.item_id}|${total.picked_none ? 'none' : total.choice_id}`,
          Number(total.vote_count) || 0,
        ]));
      }
      POOLS.weekly = normalizeWeekly(WEEKLY_ROUND);
      if (WEEKLY_ROUND?.public_status === 'revealed') {
        void loadWeeklyLeaderboard();
      }
    }
  } catch (error) {
    console.warn('Weekly quiz unavailable:', error.message);
  }
  const weeklyButton = document.querySelector('#quizsrc button[data-q="weekly"]');
  if (weeklyButton) weeklyButton.disabled = !POOLS.weekly.length;
  if (WEEKLY_ONLY) {
    document.title = isPrivatePrecloseReview()
      ? 'Foldarium · Private pre-close review'
      : (isArchiveRetrospective()
        ? 'Foldarium · Archive molecular review'
        : (isArchivePlayForFun()
          ? 'Foldarium · Play for fun'
          : 'Foldarium · Weekly blind'));
    document.querySelectorAll('#quizsrc button').forEach(button => {
      const on = button.dataset.q === 'weekly';
      button.classList.toggle('on', on); button.setAttribute('aria-pressed', String(on));
    });
    renderWeeklyResultsStatus();
    startWeeklyCountdown();
  } else {
    document.querySelectorAll('#quizsrc button').forEach(b => b.onclick = () => {
      if (b.disabled) return;
      quizSource = b.dataset.q;
      if (quizSource === 'weekly') difficulty = 'hard';
      $('#diff').style.display = quizSource === 'weekly' ? 'none' : '';
      document.querySelectorAll('#diff button').forEach(x => {
        const on = x.dataset.d === difficulty;
        x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on));
      });
      document.querySelectorAll('#quizsrc button').forEach(x => {
        const on = x === b;
        x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on));
      });
      showIntro();
    });
    document.querySelectorAll('#diff button').forEach(b => b.onclick = () => {
      difficulty = b.dataset.d;
      document.querySelectorAll('#diff button').forEach(x => {
        const on = x === b;
        x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on));
      });
      showIntro();
    });
  }
  document.querySelectorAll('#mode button').forEach(b => b.onclick = async () => {
    if (viewerControlBlocked()) return;
    const mode = b.dataset.m;
    const wasGrid = displayMode === 'grid';
    await viewerRebuild.enqueue(() => {
      displayMode = mode;
      if (retrospectiveAnswerActive() && displayMode === 'all' && clustered
          && cur.contextChoice && !isFixedReferenceChoice(cur.contextChoice)) {
        const representative = clusterForChoice(cur.contextChoice)?.rep || cur.contextChoice;
        cur.contextChoice = representative;
        cur.poseFocusChoice = representative;
      }
      if (displayMode === 'one') {
        const focus = cur.contextChoice || (!cur.selected?.none ? cur.selected : null);
        const index = focus ? visibleIndexForChoice(focus) : -1;
        shownOne = index >= 0 ? index : 0;
      }
      if (!cur.revealed && wasGrid !== (displayMode === 'grid')) selectedPaneId = null;
      if (!cur.revealed) rememberView();       // record the user's choice (persist across questions)
      syncButtons();
    }, () => { renderUI(); recordAppEvent('display_mode_changed'); });
  });
  document.querySelectorAll('#protmode button').forEach(b => b.onclick = async () => {
    if (viewerControlBlocked()) return;
    const mode = b.dataset.p;
    await viewerRebuild.enqueue(() => {
      proteinMode = mode;
      if (!cur.revealed) rememberView();
      syncButtons();
    });
    recordAppEvent('protein_mode_changed');
  });
  $('#uncluster').onclick = async () => {
    if (viewerControlBlocked()) return;
    await viewerRebuild.enqueue(() => {
      const focusedChoice = poseFocusBeforeClusterToggle();
      const preferredChoice = cur.selected?.none ? null : cur.selected;
      clustered = !clustered;
      if (!cur.revealed && preferredChoice) {
        const cluster = clusterForChoice(preferredChoice);
        const exactChoice = focusedChoice && clusterForChoice(focusedChoice) === cluster
          ? focusedChoice : preferredChoice;
        cur.selected = clustered ? (cluster?.rep || preferredChoice) : exactChoice;
        cur.selectionExact = !clustered;
        cur.selectedAsCluster = clustered;
      }
      restorePoseFocusAfterClusterToggle(focusedChoice);
      if (!cur.revealed) rememberView();
      syncButtons();
      renderUI();
      if (cur.revealed && weeklyResultsRevealActive()) renderRevealedQuestionUi();
    });
    recordAppEvent('cluster_mode_changed');
  };
  $('#hbonds').onclick = async () => {
    if (viewerControlBlocked()) return;
    await viewerRebuild.enqueue(() => {
      showHbonds = !showHbonds;
      if (!cur.revealed) rememberView();       // persist across questions like the other view choices
      syncButtons();
    });
    recordAppEvent('hbonds_toggled');
  };
  $('#protein-ensemble').onclick = async () => {
    if (viewerControlBlocked()) return;
    await viewerRebuild.enqueue(() => {
      showProteinEnsemble = !showProteinEnsemble;
      if (!cur.revealed) rememberView();
      syncButtons();
    });
    recordAppEvent('protein_ensemble_toggled');
  };
  $('#surface').onclick = async () => {
    if (viewerControlBlocked()) return;
    await viewerRebuild.enqueue(() => {
      showSurface = !showSurface;
      if (!cur.revealed) rememberView();
      syncButtons();
    });
    recordAppEvent('surface_toggled');
  };
  $('#one-select').onclick = () => {
    const choice = oneReviewChoice();
    if (retrospectiveAnswerActive()) {
      void setRetrospectiveProteinFrame('xtal');
      return;
    }
    if (!choice || interactionBlocked()) return;
    void onPick(shownOne);
  };
  $('#one-reject').onclick = () => {
    const choice = oneReviewChoice();
    if (retrospectiveAnswerActive()) {
      void setRetrospectiveProteinFrame('folded');
      return;
    }
    if (!choice || interactionBlocked()) return;
    void toggleChoiceRejected(choice);
  };
  $('#lock').onclick = reveal;
  $('#next').onclick = next;
  $('#prev').onclick = prevDev;
  $('#question-prev').onclick = () => {
    const target = isRetrospectiveReview() ? adjacentRetrospectiveQuestionIndex(-1) : idx - 1;
    if (target != null) void navigateWeeklyQuestion(target, 'question_previous');
  };
  $('#question-next').onclick = () => {
    const target = isRetrospectiveReview() ? adjacentRetrospectiveQuestionIndex(1) : idx + 1;
    if (target != null) void navigateWeeklyQuestion(target, 'question_next');
  };
  $('#retrospective-question-filter-select').onchange = event => {
    void setRetrospectiveQuestionFilter(event.target.value);
  };
  $('#retrospective-question-sort-select').onchange = event => {
    void setRetrospectiveSimilaritySort(event.target.value);
  };
  $('#quick-start-open').onclick = () => { openWeeklyQuickStart('manual'); };
  $('#quick-start-dialog').addEventListener('close', () => {
    if (quizSource === 'weekly' && cur) recordAppEvent('quick_start_closed');
  });
  $('#vote-comment-enabled').onchange = event => {
    weeklyCommentPromptEnabled = event.target.checked;
    invalidatePendingWeeklyVote();
    recordAppEvent(weeklyCommentPromptEnabled ? 'vote_comment_enabled' : 'vote_comment_disabled');
  };
  $('#start').onclick = startQuiz;
  $('#play-for-fun-start').onclick = startQuiz;
  $('#participant-name').addEventListener('input', syncStartGate);
  $('#performance-consent-checkbox')?.addEventListener('change', syncStartGate);
  $('#participant-name').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !$('#start').disabled) { event.preventDefault(); startQuiz(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) return;
    recordAppEvent('page_hidden');
    void weeklyTraceStream?.flush?.('visibility');
  });
  window.addEventListener('pagehide', () => {
    recordAppEvent('page_hidden');
    void weeklyTraceStream?.flush?.('visibility');
  });
  $('#suggestion-open').onclick = openSuggestionDialog;
  $('#suggestion-form').addEventListener('submit', submitSuggestion);
  $('#suggestion-cancel').onclick = () => $('#suggestion-dialog').close();
  $('#vote-comment-form').addEventListener('submit', submitVoteComment);
  $('#vote-comment-skip').onclick = skipVoteComment;
  $('#myview').onclick = toggleAnswer;
  $('#showXtal').onchange = async (e) => {
    if (viewerTransitionBusy) return;
    const checked = e.target.checked;
    await viewerRebuild.enqueue(() => {
      if (itemHasReleasedCrystal(cur?.item)) {
        releasedCrystalMode = checked;
        releasedCrystalError = '';
        if (!checked && cur?.revealed && cur?.showAnswer && weeklyResultsRevealActive()) {
          applyAnswerRevealView();
        } else {
          resetCameraOnNextBuild = true;
        }
      } else showXtal = checked;
      syncXtalRow();
      syncButtons();
    });
    recordAppEvent('crystal_reference_toggled');
  };
  document.addEventListener('keydown', async e => {
    if (DEV && cur) {                                   // dev: Up/Down = prev/next item, any mode, no lock needed
      if (e.key === 'ArrowUp') { e.preventDefault(); prevDev(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); nextDev(); return; }
    }
    if (!cur || viewerControlBlocked() || displayMode !== 'one') return;
    if (e.key === 'ArrowRight') {
      await viewerRebuild.enqueue(() => {
        const nav = retrospectiveNavChoices();
        shownOne = (shownOne + 1) % nav.length;
        cur.poseFocusChoice = nav[shownOne];
      }, syncOneReviewState);
      recordAppEvent('pose_navigated');
    }
    if (e.key === 'ArrowLeft') {
      await viewerRebuild.enqueue(() => {
        const nav = retrospectiveNavChoices();
        shownOne = (shownOne - 1 + nav.length) % nav.length;
        cur.poseFocusChoice = nav[shownOne];
      }, syncOneReviewState);
      recordAppEvent('pose_navigated');
    }
  });
  if (!WEEKLY_ONLY && !POOLS.cameo.length && !POOLS.rnp.length) {
    $('#ligand').textContent = 'no quiz items'; return;
  }
  if (!await resumeWeeklyQuizIfAvailable()) {
    showIntro();
    void prepareFirstWeeklyQuestionAssets()
      .then(() => prewarmGridViewerPool())
      .catch(error => console.warn('Weekly preview preparation omitted:', error.message));
  }
  if (isArchiveRetrospective() && POOLS.weekly.length) await startQuiz();
  window.foldariumApplyPrivateReviewBundle = async (bundle) => {
    if (!bundle) {
      window.foldariumPrivateReview?.deactivatePrivateReview?.();
      location.reload();
      return;
    }
    const synthetic = window.foldariumPrivateReview.buildSyntheticReviewRound(bundle);
    if (!synthetic) throw new Error('Private evaluation bundle is invalid.');
    const privateVoteTotals = new Map();
    for (const item of bundle.weekly_question_results?.items || []) {
      for (const answer of item.answers || []) {
        const answerId = answer.picked_none ? 'none' : answer.choice_id;
        privateVoteTotals.set(
          `${item.item_id}|${answerId}`,
          Number(answer.vote_count || 0),
        );
      }
    }
    const pool = window.foldariumPrivateReview.enrichPrivateWeeklyPool(
      normalizeWeekly(synthetic, privateVoteTotals),
      bundle,
    );
    window.foldariumPrivateReview.validatePrivateReviewRendering(bundle, pool);
    WEEKLY_ROUND = synthetic;
    window.foldariumPrivateReview.activatePrivateReview(bundle);
    WEEKLY_VOTES = new Map();
    WEEKLY_TOTALS = privateVoteTotals;
    WEEKLY_ITEM_STATES = new Map();
    WEEKLY_PREFETCHED_CLUSTERS = new Map();
    remoteSessionId = null;
    weeklyTraceSessionSeed = null;
    retrospectiveQuestionFilter = 'all';
    POOLS.weekly = pool;
    WEEKLY_QUESTION_RESULTS = bundle.weekly_question_results || null;
    if (bundle.weekly_leaderboard != null) {
      await loadWeeklyLeaderboard({ bundleLeaderboard: bundle.weekly_leaderboard });
    } else {
      WEEKLY_LEADERBOARD = null;
      WEEKLY_LEADERBOARD_ERROR = '';
    }
    const weeklyButton = document.querySelector('#quizsrc button[data-q="weekly"]');
    if (weeklyButton) weeklyButton.disabled = !POOLS.weekly.length;
    renderWeeklyResultsStatus();
    if (WEEKLY_ONLY) {
      document.title = isPrivatePrecloseReview()
        ? 'Foldarium · Private pre-close review'
        : 'Foldarium · Weekly blind';
    }
    cur = null;
    if (WEEKLY_ONLY) {
      showIntro();
      await startQuiz();
    }
  };
  window.dispatchEvent(new Event('foldarium-private-review-ready'));
}
init().catch(e => { $('#ligand').textContent = 'error: ' + e.message; console.error(e); });
