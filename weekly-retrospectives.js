import {
  fetchWeeklyTrainingSimilarityReport,
  sortWeeklySimilarityRows,
  weeklySimilarityRecord,
} from './weekly-training-similarity.js';

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
  return {
    view: !roundId && params.get('view') === 'all-time' ? 'all-time' : 'archive',
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

const state = {
  route: typeof location === 'undefined'
    ? { view: 'archive', roundId: null }
    : archiveRoute(location.pathname, location.search),
  publications: [],
  nextCursor: null,
  detail: null,
  adminDetail: null,
  questionFilter: 'all',
  questionSort: 'default',
  similarityReport: null,
  ranking: 'total_correct',
  participantKind: '',
  adminAllTimeAvailable: false,
};

function setActiveTab() {
  const archive = document.getElementById('archive-tab');
  const allTime = document.getElementById('all-time-tab');
  archive.toggleAttribute('aria-current', state.route.view === 'archive');
  allTime.toggleAttribute('aria-current', state.route.view === 'all-time');
  document.getElementById('archive-view').hidden = state.route.view !== 'archive';
  document.getElementById('all-time-view').hidden = state.route.view !== 'all-time';
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

function renderQuestionRow(row) {
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
  if (!rows.length) list.append(element('p', 'empty', 'No player results.'));
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
  const molecular = element('a', '', 'Open molecular review');
  molecular.href = `/weekly?retrospective_round=${encodeURIComponent(round.round_id)}`;
  const back = element('a', '', 'Back to archive');
  back.href = '/weekly/retrospectives';
  actions.append(molecular, back);
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
  const controls = element('div', 'question-controls');
  renderQuestionFilters(controls, rows);
  renderQuestionSortControls(controls, rows);
  questions.append(controls);
  const list = element('div', 'question-list');
  const visibleRows = rows.filter(
    row => state.questionFilter === 'all' || row.outcome === state.questionFilter,
  );
  sortWeeklySimilarityRows(visibleRows, state.questionSort)
    .forEach(row => list.append(renderQuestionRow(row)));
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
    }
    const [archive, similarityReport, detail = null, adminDetail = null] = await Promise.all(requests);
    state.publications = archive.publications || [];
    state.nextCursor = archive.next_cursor || null;
    state.similarityReport = similarityReport;
    state.detail = detail;
    state.adminDetail = adminDetail;
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
  setPressed('ranking-view', '[data-ranking]', state.ranking);
  setPressed('participant-filter', '[data-kind]', state.participantKind);
}

async function startArchive() {
  setActiveTab();
  bindControls();
  if (state.route.view === 'all-time') await loadAllTime();
  else await loadArchive();
}

if (typeof document !== 'undefined') void startArchive();
