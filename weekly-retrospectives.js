import {
  fetchWeeklyTrainingSimilarityReport,
  sortWeeklySimilarityRows,
  weeklySimilarityRecord,
} from './weekly-training-similarity.js';
import {
  aggregateMethodStats,
  methodName,
  methodTrend,
  scoreMethodPoses,
  validateMethodStats,
} from './method-performance.js';

export const OUTCOME_FILTERS = Object.freeze([
  ['pose-solved', 'Pose · human correct'],
  ['pose-unsolved', 'Pose · no human correct'],
  ['none-solved', 'None · human correct'],
  ['none-unsolved', 'None · no human correct'],
]);

const OUTCOME_LABELS = new Map(OUTCOME_FILTERS);

export function archiveRoute(pathname, search = '') {
  const match = /^\/weekly\/retrospectives(?:\/([^/]+))?\/?$/.exec(pathname);
  if (!match) return { view: 'archive', roundId: null };
  let roundId = null;
  if (match[1]) {
    try {
      roundId = decodeURIComponent(match[1]);
    } catch {
      roundId = null;
    }
  }
  if (roundId && !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(roundId)) roundId = null;
  const params = new URLSearchParams(search);
  const requestedView = params.get('view');
  return {
    view: !roundId && new Set(['all-time', 'cofolding']).has(requestedView)
      ? requestedView : 'archive',
    roundId,
  };
}

export function questionOutcome(question, revealItem) {
  if (question?.human_aggregate?.suppressed === true) return 'suppressed';
  const hasPose = revealItem?.choices?.some(choice => choice.correct === true) === true;
  const solved = Number(question?.human_aggregate?.correct_count) > 0;
  return `${hasPose ? 'pose' : 'none'}-${solved ? 'solved' : 'unsolved'}`;
}

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function formatDate(value, options = { month: 'short', day: 'numeric', year: 'numeric' }) {
  const date = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value,
  );
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat(undefined, options).format(date)
    : 'Unknown date';
}

function formatKind(value) {
  return value === 'llm' ? 'LLM' : value === 'baseline' ? 'Baseline' : 'Human';
}

export function buildOutcomeRail(outcomes, documentRef = document) {
  const rail = documentRef.createElement('div');
  rail.className = 'outcome-rail';
  rail.setAttribute('aria-label', 'Human outcomes as a share of questions');
  const knownTotal = OUTCOME_FILTERS.reduce((sum, [key]) => (
    sum + (Number(outcomes?.[key.replace('-', '_')]) || 0)
  ), 0);
  const total = Math.max(1, knownTotal + (Number(outcomes?.suppressed) || 0));
  for (const [key, label] of OUTCOME_FILTERS) {
    const count = Number(outcomes?.[key.replace('-', '_')] || 0);
    const lane = documentRef.createElement('div');
    lane.className = 'rail-lane';
    lane.dataset.outcome = key;
    const name = documentRef.createElement('span');
    name.textContent = label.replace('Correct pose · ', 'Pose ').replace('None · ', 'None ');
    const track = documentRef.createElement('span');
    track.className = 'rail-track';
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-label', label);
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', String(knownTotal));
    track.setAttribute('aria-valuenow', String(count));
    track.title = `${count} of ${knownTotal} questions`;
    const fill = documentRef.createElement('span');
    fill.className = 'rail-fill';
    fill.style.width = `${100 * count / total}%`;
    track.append(fill);
    const value = documentRef.createElement('span');
    value.textContent = String(count);
    lane.append(name, track, value);
    rail.append(lane);
  }
  return rail;
}

function buildOutcomeSummary(outcomes) {
  const summary = element('div', 'outcome-summary');
  summary.append(element('span', 'outcome-caption', 'Human outcomes · share of questions'));
  summary.append(buildOutcomeRail(outcomes));
  const hidden = Number(outcomes?.suppressed) || 0;
  if (hidden) {
    summary.append(element(
      'span',
      'outcome-hidden',
      `${hidden} ${hidden === 1 ? 'question' : 'questions'} hidden for privacy`,
    ));
  }
  return summary;
}

async function api(parameters = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== null && value !== undefined && value !== false && value !== '') {
      query.set(key, value === true ? '1' : String(value));
    }
  }
  const response = await fetch(`/api/weekly-retrospectives${query.size ? `?${query}` : ''}`);
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.error || 'Retrospectives are unavailable');
  return payload;
}

async function playForFunLeaderboard(roundId) {
  const query = new URLSearchParams({ round_id: roundId });
  const response = await fetch(`/api/weekly-play-for-fun-results?${query}`);
  const payload = await response.json().catch(() => null);
  if (!response.ok
      || payload?.format_version !== 'foldarium.weekly-play-for-fun-leaderboard/v1'
      || payload.round_id !== roundId) {
    throw new Error(payload?.error || 'Play-for-fun results are unavailable');
  }
  return payload;
}

const state = {
  route: typeof location === 'undefined'
    ? { view: 'archive', roundId: null }
    : archiveRoute(location.pathname, location.search),
  publications: [],
  nextCursor: null,
  detail: null,
  adminDetail: null,
  playForFunLeaderboard: null,
  questionFilter: 'all',
  questionSort: 'default',
  similarityReport: null,
  ranking: 'total_correct',
  participantKind: '',
  adminAllTimeAvailable: false,
  cofoldingView: 'overall',
  cofoldingMethod: null,
  methodData: null,
};

function setActiveTab() {
  const archive = document.getElementById('archive-tab');
  const allTime = document.getElementById('all-time-tab');
  const cofolding = document.getElementById('cofolding-tab');
  archive.toggleAttribute('aria-current', state.route.view === 'archive');
  allTime.toggleAttribute('aria-current', state.route.view === 'all-time');
  cofolding.toggleAttribute('aria-current', state.route.view === 'cofolding');
  document.getElementById('archive-view').hidden = state.route.view !== 'archive';
  document.getElementById('all-time-view').hidden = state.route.view !== 'all-time';
  document.getElementById('cofolding-view').hidden = state.route.view !== 'cofolding';
}

function roundHref(roundId) {
  return `/weekly/retrospectives/${encodeURIComponent(roundId)}`;
}

function makeRoundRow(publication, compact = false) {
  const row = element('a', compact ? 'chooser-round' : 'round-row');
  row.href = roundHref(publication.round_id);
  const summary = publication.summary || {};
  const date = element('div', 'round-date');
  date.append(
    element('strong', '', `Blind week · ${formatDate(publication.blind_week)}`),
    element('span', '', `${publication.item_count} questions · ${publication.choice_count} poses`),
  );
  const meta = element('div', 'round-meta');
  meta.append(
    element('span', '', `${summary.human_participant_count || 0} human participants`),
  );
  if (summary.automated_winner) {
    const winner = element('span', 'winner');
    winner.append(
      element('strong', '', summary.automated_winner.participant),
      document.createTextNode(
        ` ${summary.automated_winner.correct}/${summary.automated_winner.total}`,
      ),
    );
    meta.append(winner);
  }
  if (compact) {
    row.append(date, meta);
    return row;
  }
  row.append(
    date,
    meta,
    buildOutcomeSummary(summary.outcomes || {}),
    element('span', 'explore', 'Explore questions →'),
  );
  return row;
}

function renderRoundLists() {
  const list = clear(document.getElementById('round-list'));
  const chooser = clear(document.getElementById('round-chooser-list'));
  list.removeAttribute('aria-busy');
  if (!state.publications.length) {
    list.append(element('p', 'empty', 'No published weeks yet.'));
    chooser.append(element('p', 'empty', 'No published weeks yet.'));
    return;
  }
  for (const publication of state.publications) {
    list.append(makeRoundRow(publication));
    chooser.append(makeRoundRow(publication, true));
  }
  const more = document.getElementById('load-more');
  more.hidden = !state.nextCursor;
}

function overviewCell(label, value) {
  const cell = element('div');
  cell.append(element('span', '', label), element('strong', '', value));
  return cell;
}

function choiceLabel(choiceId, pickedNone, blindItem) {
  if (pickedNone) return 'None';
  const index = blindItem?.choices?.findIndex(choice => choice.id === choiceId) ?? -1;
  return index >= 0 ? `Pose ${index + 1}` : 'Unknown pose';
}

function detailQuestionRows(detail) {
  const retrospectiveQuestions = detail.retrospective?.questions || [];
  const revealById = new Map((detail.reveal_manifest?.items || []).map(item => [item.id, item]));
  const blindById = new Map((detail.blind_manifest?.items || []).map(item => [item.id, item]));
  const blindWeek = detail.round?.blind_week;
  return retrospectiveQuestions.map((question, index) => {
    const revealItem = revealById.get(question.item_id);
    const blindItem = blindById.get(question.item_id);
    return {
      question,
      revealItem,
      blindItem,
      index,
      outcome: questionOutcome(question, revealItem),
      similarity: weeklySimilarityRecord(
        state.similarityReport,
        blindWeek,
        question.item_id,
      ),
    };
  });
}

function renderQuestionFilters(host, rows) {
  const filters = element('div', 'filter-row');
  filters.setAttribute('aria-label', 'Filter question outcomes');
  const options = [['all', 'All'], ...OUTCOME_FILTERS];
  for (const [value, label] of options) {
    const button = element('button', '', label);
    button.type = 'button';
    button.dataset.filter = value;
    const count = value === 'all' ? rows.length : rows.filter(row => row.outcome === value).length;
    button.textContent = `${label} ${count}`;
    button.disabled = count === 0;
    button.setAttribute('aria-pressed', String(state.questionFilter === value));
    button.addEventListener('click', () => {
      state.questionFilter = value;
      renderDetail();
    });
    filters.append(button);
  }
  host.append(filters);
}

function renderQuestionSortControls(host, rows) {
  if (!rows.some(row => row.similarity)) return;
  const sort = element('div', 'segmented similarity-sort');
  sort.setAttribute('aria-label', 'Sort questions by training similarity');
  for (const [value, label] of [
    ['default', 'Default'],
    ['novel-first', 'Novel first'],
    ['familiar-first', 'Familiar first'],
  ]) {
    const button = element('button', '', label);
    button.type = 'button';
    button.dataset.sort = value;
    button.setAttribute('aria-pressed', String(state.questionSort === value));
    button.addEventListener('click', () => {
      state.questionSort = value;
      renderDetail();
    });
    sort.append(button);
  }
  host.append(sort);
}

function ligandLabel(item, index) {
  const ligand = typeof item?.ligand === 'string'
    ? item.ligand : item?.ligand?.component_id || item?.ligand?.name;
  return ligand ? `Question ${index + 1} · ${ligand}` : `Question ${index + 1}`;
}

function answerLine(label, value) {
  const line = element('div', 'answer-line');
  line.append(element('b', '', label), element('span', '', value));
  return line;
}

function buildSimilarityMeta(similarity) {
  if (!similarity) return null;
  const meta = element('div', 'similarity-meta');
  meta.dataset.classification = similarity.classification;
  const classification = similarity.classification[0].toUpperCase()
    + similarity.classification.slice(1);
  const score = Number.isFinite(similarity.train_shape_overlap)
    ? similarity.train_shape_overlap.toFixed(4)
    : similarity.classification === 'novel' ? 'No usable analog' : 'Unavailable';
  const source = similarity.train_pdb && similarity.train_het
    ? `${similarity.train_pdb.toUpperCase()} + ${similarity.train_het.toUpperCase()}`
    : 'Unavailable';
  meta.append(
    answerLine('Classification', classification),
    answerLine('Score', score),
    answerLine('Source PDB + ligand', source),
  );
  return meta;
}

export function humanAnswerSummary(human) {
  const answered = Number(human?.answered_count) || 0;
  return answered ? `${Number(human?.correct_count) || 0}/${answered} correct` : 'No answers';
}

export function targetMethodOutcomes(blindItem, revealItem, methods = null) {
  const revealChoices = new Map(
    (revealItem?.choices || []).map(choice => [choice.id, choice]),
  );
  const posesByMethod = new Map();
  for (const choice of blindItem?.choices || []) {
    if (typeof choice.method !== 'string' || !choice.method || !revealChoices.has(choice.id)) {
      continue;
    }
    const poses = posesByMethod.get(choice.method) || [];
    poses.push({
      id: choice.id,
      correct: revealChoices.get(choice.id).correct === true,
      confidence: choice.confidence,
    });
    posesByMethod.set(choice.method, poses);
  }
  const methodIds = methods || [...posesByMethod.keys()].sort();
  return methodIds.map(method => {
    const poses = posesByMethod.get(method) || [];
    if (!poses.length) return { method, oracle_success: null, top1_success: null };
    const score = scoreMethodPoses(poses);
    return {
      method,
      oracle_success: score.oracle_success,
      top1_success: score.top1_success,
    };
  });
}

function methodOutcomeCell(value, metric, method) {
  const cell = element('td');
  const mark = element(
    'span',
    value == null ? 'method-outcome missing' : `method-outcome ${value ? 'correct' : 'wrong'}`,
    value == null ? '—' : value ? '✓' : '×',
  );
  const result = value == null ? 'not evaluated' : value ? 'correct' : 'wrong';
  mark.setAttribute('aria-label', `${methodName(method)} ${metric}: ${result}`);
  mark.title = `${methodName(method)} ${metric}: ${result}`;
  cell.append(mark);
  return cell;
}

function buildTargetMethodTable(row, methods) {
  const outcomes = targetMethodOutcomes(row.blindItem, row.revealItem, methods);
  if (!outcomes.length) return null;
  const table = element('table', 'target-method-table');
  const caption = element('caption', 'sr-only', 'Cofolding method raw-pose outcomes');
  const head = element('thead');
  const header = element('tr');
  ['Method', 'Oracle', 'Top-1'].forEach(label => header.append(element('th', '', label)));
  head.append(header);
  const body = element('tbody');
  for (const outcome of outcomes) {
    const line = element('tr');
    line.append(
      element('th', '', methodName(outcome.method)),
      methodOutcomeCell(outcome.oracle_success, 'oracle', outcome.method),
      methodOutcomeCell(outcome.top1_success, 'top-1', outcome.method),
    );
    body.append(line);
  }
  table.append(caption, head, body);
  return table;
}

function renderQuestionRow(row, methods) {
  const node = element('article', 'question-row');
  node.dataset.outcome = row.outcome;
  const title = element('div', 'question-title');
  const identity = element('div', 'question-identity');
  identity.append(element('strong', '', ligandLabel(row.blindItem, row.index)));
  if (/^[0-9][A-Za-z0-9]{3}$/.test(row.question.item_id || '')) {
    const pdbId = row.question.item_id.toUpperCase();
    const link = element('a', 'question-pdb-link', `PDB ${pdbId} ↗`);
    link.href = `https://www.rcsb.org/structure/${encodeURIComponent(pdbId)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.setAttribute('aria-label', `Open PDB ${pdbId} in RCSB`);
    identity.append(link);
  }
  title.append(
    identity,
    element(
      'span',
      `outcome-tag${row.outcome === 'suppressed' ? ' suppressed' : ''}`,
      row.outcome === 'suppressed' ? 'Human aggregate hidden' : OUTCOME_LABELS.get(row.outcome),
    ),
  );
  const answers = element('div', 'answer-block');
  const human = row.question.human_aggregate;
  answers.append(answerLine(
    'Human players',
    humanAnswerSummary(human),
  ));
  for (const answer of human.answers || []) {
    answers.append(answerLine(
      `↳ ${choiceLabel(answer.choice_id, answer.picked_none, row.blindItem)}`,
      answer.display_names.join(', '),
    ));
  }
  const automatedEntries = row.question.automated_entries || [];
  if (automatedEntries.length) {
    answers.append(element('div', 'answer-section-label', 'Automated methods'));
  }
  for (const automated of automatedEntries) {
    answers.append(answerLine(
      automated.participant,
      `${choiceLabel(automated.choice_id, automated.picked_none, row.blindItem)} · ${automated.correct ? 'correct' : 'wrong'}`,
    ));
  }
  node.append(title);
  const similarity = buildSimilarityMeta(row.similarity);
  if (similarity) node.append(similarity);
  const methodTable = buildTargetMethodTable(row, methods);
  if (methodTable) node.append(methodTable);
  node.append(answers);
  return node;
}

function renderAutomatedLeaderboard(host, detail) {
  const section = element('section', 'detail-section');
  section.append(element('h3', '', 'Weekly automated leaderboard'));
  const list = element('div', 'admin-list');
  const rows = [...(detail.retrospective?.automated_entries || [])].sort((left, right) => (
    right.correct - left.correct || right.accuracy - left.accuracy
  ));
  rows.forEach((row, index) => {
    list.append(answerLine(
      `${index + 1}. ${row.participant}`,
      `${row.correct}/${row.total} · ${Math.round(row.accuracy)}%`,
    ));
  });
  section.append(list);
  host.append(section);
}

function renderHumanLeaderboard(host, detail) {
  const section = element('section', 'detail-section');
  section.append(element('h3', '', 'Weekly player leaderboard'));
  const list = element('div', 'admin-list');
  const rows = [...(detail.retrospective?.human_entries || [])].sort(
    (left, right) => right.correct - left.correct
      || left.participant.localeCompare(right.participant),
  );
  rows.forEach((row, index) => {
    list.append(answerLine(
      `${index + 1}. ${row.participant}`,
      `${row.correct}/${row.total} · ${Math.round(row.accuracy)}%`,
    ));
  });
  if (rows.length) {
    list.prepend(element('div', 'answer-section-label', 'Blind-week players'));
  } else {
    list.append(element('p', 'empty', 'No blind-week player results.'));
  }
  const forFunRows = [
    ...(state.playForFunLeaderboard?.complete_runs || []),
    ...(state.playForFunLeaderboard?.partial_runs || []),
  ];
  list.append(element('div', 'answer-section-label', 'Play for fun'));
  for (const row of forFunRows) {
    list.append(answerLine(
      `${row.display_name} · For fun`,
      `${row.correct}/${row.total} · ${Math.round(row.accuracy)}%`,
    ));
  }
  if (!forFunRows.length) {
    list.append(element('p', 'empty compact', 'No play-for-fun results yet.'));
  }
  section.append(list);
  host.append(section);
}

function renderAdminDetail(host, detail, admin) {
  if (!admin) return;
  const panel = element('section', 'admin-panel');
  panel.append(element('h3', '', 'Admin preview · pseudonymous human responses'));
  const participants = element('div', 'admin-list');
  [...(admin.retrospective?.participants || [])]
    .sort((left, right) => right.correct - left.correct || left.participant.localeCompare(right.participant))
    .forEach((row, index) => {
      const line = element('div', 'admin-line');
      line.append(
        element('span', '', `${index + 1}. ${row.participant} · ${formatKind(row.participant_kind)}`),
        element('span', '', `${row.correct}/${row.total} · ${Math.round(row.accuracy)}%`),
      );
      participants.append(line);
    });
  panel.append(participants);
  const blindById = new Map((detail.blind_manifest?.items || []).map(item => [item.id, item]));
  (admin.retrospective?.questions || []).forEach((question, index) => {
    const group = element('div', 'admin-question');
    group.append(element('strong', '', `Question ${index + 1}`));
    const item = blindById.get(question.item_id);
    for (const response of question.responses || []) {
      group.append(answerLine(
        response.participant,
        `${choiceLabel(response.choice_id, response.picked_none, item)} · ${response.correct ? 'correct' : 'wrong'}`,
      ));
    }
    panel.append(group);
  });
  host.append(panel);
}

function renderDetail() {
  const host = clear(document.getElementById('round-detail'));
  if (!state.detail) return;
  const detail = state.detail;
  const round = detail.round;
  host.hidden = false;
  document.getElementById('archive-layout').classList.add('has-detail');
  document.getElementById('choose-round').hidden = false;

  const head = element('div', 'detail-head');
  head.append(
    element('p', 'eyebrow', 'Blind week'),
    element('h2', '', formatDate(round.blind_week, {
      weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    })),
  );
  const actions = element('div', 'detail-actions');
  const playForFun = element('a', 'play-for-fun', 'Play for fun');
  playForFun.href = `/weekly?retrospective_round=${encodeURIComponent(round.round_id)}&play_for_fun=1`;
  const molecular = element('a', 'molecular-review', 'Open molecular review');
  molecular.href = `/weekly?retrospective_round=${encodeURIComponent(round.round_id)}`;
  const back = element('a', '', 'Back to archive');
  back.href = '/weekly/retrospectives';
  actions.append(playForFun, molecular, back);
  head.append(actions);

  const overview = element('div', 'overview');
  overview.append(
    overviewCell('Questions', round.item_count),
    overviewCell('Predicted poses', round.choice_count),
    overviewCell('Human participants', detail.retrospective?.human_aggregate?.participant_count || 0),
  );
  host.append(head, overview);
  renderHumanLeaderboard(host, detail);
  renderAutomatedLeaderboard(host, detail);

  const questions = element('section', 'detail-section');
  questions.append(element('h3', '', 'Question outcomes'));
  const rows = detailQuestionRows(detail);
  const methods = [...new Set(
    (detail.blind_manifest?.items || []).flatMap(
      item => (item.choices || []).map(choice => choice.method).filter(Boolean),
    ),
  )].sort();
  const controls = element('div', 'question-controls');
  renderQuestionFilters(controls, rows);
  renderQuestionSortControls(controls, rows);
  questions.append(controls);
  const list = element('div', 'question-list');
  const visibleRows = rows.filter(
    row => state.questionFilter === 'all' || row.outcome === state.questionFilter,
  );
  sortWeeklySimilarityRows(visibleRows, state.questionSort)
    .forEach(row => list.append(renderQuestionRow(row, methods)));
  questions.append(list);
  host.append(questions);
  renderAdminDetail(host, detail, state.adminDetail);
}

async function loadArchive() {
  const list = document.getElementById('round-list');
  list.setAttribute('aria-busy', 'true');
  list.append(element('p', 'empty', 'Loading published weeks…'));
  try {
    const requests = [
      api({ limit: 20 }),
      fetchWeeklyTrainingSimilarityReport().catch(() => null),
    ];
    if (state.route.roundId) {
      requests.push(api({ round_id: state.route.roundId }));
      requests.push(api({ admin: true, round_id: state.route.roundId }).catch(() => null));
      requests.push(playForFunLeaderboard(state.route.roundId).catch(() => null));
    }
    const [
      archive,
      similarityReport,
      detail = null,
      adminDetail = null,
      forFunLeaderboard = null,
    ] = await Promise.all(requests);
    state.publications = archive.publications || [];
    state.nextCursor = archive.next_cursor || null;
    state.similarityReport = similarityReport;
    state.detail = detail;
    state.adminDetail = adminDetail;
    state.playForFunLeaderboard = forFunLeaderboard;
    renderRoundLists();
    renderDetail();
  } catch (error) {
    clear(list).append(element('p', 'error', error.message));
  }
}

async function loadMore() {
  if (!state.nextCursor) return;
  const button = document.getElementById('load-more');
  button.disabled = true;
  try {
    const archive = await api({ limit: 20, cursor: state.nextCursor });
    const seen = new Set(state.publications.map(row => row.round_id));
    state.publications.push(...(archive.publications || []).filter(row => !seen.has(row.round_id)));
    state.nextCursor = archive.next_cursor || null;
    renderRoundLists();
  } catch (error) {
    button.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function setPressed(containerId, selector, value) {
  document.querySelectorAll(`#${containerId} ${selector}`).forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.ranking === value
      || button.dataset.kind === value));
  });
}

function renderAllTime(payload) {
  const table = clear(document.getElementById('all-time-table'));
  const rows = payload?.participants || [];
  const header = element('div', 'ranking-row header');
  ['Rank', 'Participant', 'Weeks', 'Correct', 'Accuracy'].forEach(label => {
    header.append(element('span', '', label));
  });
  table.append(header);
  if (!rows.length) {
    table.append(element('p', 'empty', 'No qualifying results.'));
    return;
  }
  for (const row of rows) {
    const line = element('div', 'ranking-row');
    const participant = element('div');
    participant.append(element('span', 'participant', row.participant));
    if (row.provisional) participant.append(element('span', 'provisional', 'Provisional'));
    participant.append(element('div', 'kind', formatKind(row.participant_kind)));
    line.append(
      element('span', 'rank', `#${row.rank}`),
      participant,
      element('span', 'metric', `${row.complete_weeks}/${row.weeks_participated} complete`),
      element('span', 'metric', `${row.total_correct}/${row.total_questions}`),
      element('span', 'metric', row.weighted_average_accuracy == null
        ? '—' : `${row.weighted_average_accuracy}%`),
    );
    table.append(line);
  }
}

function methodRate(rate, successes, total) {
  const metric = element('span', 'metric', rate == null ? '—' : `${Math.round(rate)}%`);
  if (total) metric.append(element('span', 'metric-detail', `${successes}/${total}`));
  return metric;
}

function renderCofoldingOverall(methods) {
  const table = clear(document.getElementById('cofolding-overall'));
  const header = element('div', 'method-ranking-row header');
  ['Rank', 'Method', 'Targets', 'Oracle', 'Top-1'].forEach(label => {
    header.append(element('span', '', label));
  });
  table.append(header);
  for (const [index, row] of methods.entries()) {
    const line = element('div', 'method-ranking-row');
    line.append(
      element('span', 'rank', `#${index + 1}`),
      element('span', 'participant', methodName(row.method)),
      element('span', 'metric', row.targets),
      methodRate(row.oracle_rate, row.oracle_successes, row.targets),
      methodRate(row.top1_rate, row.top1_successes, row.top1_evaluated),
    );
    table.append(line);
  }
}

function svgElement(name, attributes = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function formatMethodWeek(value) {
  return formatDate(value, { month: 'short', day: 'numeric' });
}

function renderCofoldingTrend() {
  const host = clear(document.getElementById('cofolding-weekly'));
  const rows = methodTrend(state.methodData, state.cofoldingMethod);
  if (!rows.length) {
    host.append(element('p', 'empty', 'No weekly method results.'));
    return;
  }
  const head = element('div', 'method-trend-head');
  const title = element('h2', '', `${methodName(state.cofoldingMethod)} weekly success`);
  title.id = 'method-trend-title';
  const legend = element('div', 'method-legend');
  for (const [label, className] of [
    ['Oracle', 'method-swatch'],
    ['Top-1 ligand pLDDT', 'method-swatch top1'],
  ]) {
    const item = element('span');
    const swatch = element('i', className);
    swatch.setAttribute('aria-hidden', 'true');
    item.append(swatch, label);
    legend.append(item);
  }
  head.append(title, legend);
  host.append(head);

  const compact = window.matchMedia('(max-width: 620px)').matches;
  const width = compact ? 360 : 1000;
  const height = compact ? 230 : 300;
  const left = compact ? 40 : 52;
  const right = compact ? 30 : 24;
  const top = 18;
  const bottom = compact ? 34 : 42;
  const innerWidth = width - left - right;
  const innerHeight = height - top - bottom;
  const x = index => left + (rows.length === 1
    ? innerWidth / 2 : (innerWidth * index) / (rows.length - 1));
  const y = value => top + innerHeight * (1 - value / 100);
  const chart = svgElement('svg', {
    class: 'method-chart',
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-labelledby': 'method-trend-title method-trend-description',
  });
  const description = svgElement('desc', { id: 'method-trend-description' });
  description.textContent = 'Weekly oracle and top-1 raw-pose success rates from zero to one hundred percent.';
  chart.append(description);
  for (const tick of [0, 25, 50, 75, 100]) {
    chart.append(svgElement('line', {
      class: 'method-chart-grid',
      x1: left,
      x2: width - right,
      y1: y(tick),
      y2: y(tick),
    }));
    const label = svgElement('text', {
      class: 'method-chart-axis',
      x: left - 9,
      y: y(tick) + 4,
      'text-anchor': 'end',
    });
    label.textContent = `${tick}%`;
    chart.append(label);
  }
  rows.forEach((row, index) => {
    const label = svgElement('text', {
      class: 'method-chart-axis',
      x: x(index),
      y: height - 14,
      'text-anchor': 'middle',
    });
    label.textContent = formatMethodWeek(row.week);
    chart.append(label);
  });
  for (const [key, lineClass, pointClass, label] of [
    ['oracle_rate', 'method-chart-oracle', 'method-point-oracle', 'Oracle'],
    ['top1_rate', 'method-chart-top1', 'method-point-top1', 'Top-1'],
  ]) {
    const visible = rows
      .map((row, index) => row[key] == null ? null : `${x(index)},${y(row[key])}`)
      .filter(Boolean);
    chart.append(svgElement('polyline', {
      class: lineClass,
      points: visible.join(' '),
    }));
    rows.forEach((row, index) => {
      if (row[key] == null) return;
      const point = svgElement('circle', {
        class: pointClass,
        cx: x(index),
        cy: y(row[key]),
        r: 4,
      });
      const tooltip = svgElement('title');
      tooltip.textContent = `${formatMethodWeek(row.week)} · ${label} ${Math.round(row[key])}%`;
      point.append(tooltip);
      chart.append(point);
    });
  }
  host.append(chart);

  const details = element('details', 'method-data');
  details.append(element('summary', '', 'View data table'));
  const table = element('div', 'method-data-table');
  const header = element('div', 'method-data-row header');
  ['Week', 'Targets', 'Oracle', 'Top-1'].forEach(label => header.append(element('span', '', label)));
  table.append(header);
  for (const row of rows) {
    const line = element('div', 'method-data-row');
    line.append(
      element('span', '', formatMethodWeek(row.week)),
      element('span', '', row.targets),
      element('span', '', `${Math.round(row.oracle_rate)}% · ${row.oracle_successes}/${row.targets}`),
      element('span', '', row.top1_rate == null
        ? '—' : `${Math.round(row.top1_rate)}% · ${row.top1_successes}/${row.top1_evaluated}`),
    );
    table.append(line);
  }
  details.append(table);
  host.append(details);
}

function setCofoldingView(view) {
  state.cofoldingView = view;
  document.querySelectorAll('#cofolding-ranking-view [data-cofolding-view]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.cofoldingView === view));
  });
  document.getElementById('cofolding-overall').hidden = view !== 'overall';
  document.getElementById('cofolding-weekly').hidden = view !== 'weekly';
  document.getElementById('cofolding-method-filter').hidden = view !== 'weekly';
  if (view === 'weekly' && state.methodData) renderCofoldingTrend();
}

function renderCofoldingMethodFilter(methods) {
  const filter = clear(document.getElementById('cofolding-method-filter'));
  for (const row of methods) {
    const button = element('button', '', methodName(row.method));
    button.type = 'button';
    button.dataset.method = row.method;
    button.setAttribute('aria-pressed', String(row.method === state.cofoldingMethod));
    button.addEventListener('click', () => {
      state.cofoldingMethod = row.method;
      filter.querySelectorAll('[data-method]').forEach(candidate => {
        candidate.setAttribute('aria-pressed', String(candidate === button));
      });
      renderCofoldingTrend();
    });
    filter.append(button);
  }
}

async function loadCofolding() {
  const status = document.getElementById('cofolding-status');
  status.textContent = 'Loading method performance…';
  try {
    const response = await fetch('/weekly_method_stats.json?v=20260830');
    if (!response.ok) throw new Error('Method performance is unavailable');
    state.methodData = await response.json();
    validateMethodStats(state.methodData);
    const methods = aggregateMethodStats(state.methodData);
    state.cofoldingMethod = methods[0]?.method || null;
    renderCofoldingOverall(methods);
    renderCofoldingMethodFilter(methods);
    setCofoldingView(state.cofoldingView);
    status.textContent = '';
  } catch (error) {
    state.methodData = null;
    status.textContent = error.message;
    clear(document.getElementById('cofolding-overall'));
    clear(document.getElementById('cofolding-weekly'));
  }
}

async function loadAllTime() {
  const status = document.getElementById('all-time-status');
  status.textContent = 'Loading rankings…';
  const humanButton = document.querySelector('#participant-filter [data-kind="human"]');
  try {
    const payload = await api({
      all_time: true,
      ranking: state.ranking,
      participant_kind: state.participantKind || null,
    });
    humanButton.disabled = false;
    humanButton.title = 'Show player pseudonyms';
    status.textContent = '';
    renderAllTime(payload);
  } catch (error) {
    status.textContent = error.message;
    clear(document.getElementById('all-time-table'));
  }
}

async function loadCurrentView() {
  setActiveTab();
  if (state.route.view === 'all-time') await loadAllTime();
  else if (state.route.view === 'cofolding') {
    if (state.methodData) setCofoldingView(state.cofoldingView);
    else await loadCofolding();
  } else await loadArchive();
}

function navigateToView(view, href) {
  state.route = { view, roundId: null };
  history.pushState(null, '', href);
  void loadCurrentView();
}

function bindControls() {
  document.getElementById('load-more').addEventListener('click', loadMore);
  const chooser = document.getElementById('round-chooser');
  document.getElementById('choose-round').addEventListener('click', () => chooser.showModal());
  document.getElementById('close-round-chooser').addEventListener('click', () => chooser.close());
  chooser.addEventListener('click', event => {
    if (event.target === chooser) chooser.close();
  });
  document.querySelectorAll('#ranking-view [data-ranking]').forEach(button => {
    button.addEventListener('click', () => {
      state.ranking = button.dataset.ranking;
      setPressed('ranking-view', '[data-ranking]', state.ranking);
      void loadAllTime();
    });
  });
  document.querySelectorAll('#participant-filter [data-kind]').forEach(button => {
    button.addEventListener('click', () => {
      if (button.disabled) return;
      state.participantKind = button.dataset.kind;
      setPressed('participant-filter', '[data-kind]', state.participantKind);
      void loadAllTime();
    });
  });
  document.querySelectorAll('#cofolding-ranking-view [data-cofolding-view]').forEach(button => {
    button.addEventListener('click', () => setCofoldingView(button.dataset.cofoldingView));
  });
  document.querySelectorAll('.archive-tabs a[data-view]').forEach(link => {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigateToView(link.dataset.view, link.href);
    });
  });
  window.addEventListener('popstate', () => {
    const route = archiveRoute(location.pathname, location.search);
    if (route.roundId) {
      location.reload();
      return;
    }
    state.route = route;
    void loadCurrentView();
  });
  setPressed('ranking-view', '[data-ranking]', state.ranking);
  setPressed('participant-filter', '[data-kind]', state.participantKind);
  setCofoldingView(state.cofoldingView);
}

async function startArchive() {
  bindControls();
  await loadCurrentView();
}

if (typeof document !== 'undefined') void startArchive();
