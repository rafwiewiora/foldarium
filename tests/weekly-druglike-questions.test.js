import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import {
  druglikeQuestionScope, loadDruglikeQuestionScope, parseWeeklyQuestionScope,
  scopedWeeklyQuestionIndexes,
} from '../weekly-featured-questions.js';
import { createWeeklySessionResumeStore } from '../weekly-session-resume.js';

function fixture() {
  const indexes = [1, 9, 16, 27, 38];
  const round = { round_id: 'weekly-fixture', blind_manifest_sha256: 'a'.repeat(64),
    blind_manifest: { items: Array.from({ length: 40 }, (_, i) => ({ id: `item-${i}` })) } };
  const marker = { round_id: round.round_id, blind_manifest_sha256: round.blind_manifest_sha256,
    policy: 'foldarium.medchem-presentation-preference/v1', selection_sha256: 'b'.repeat(64),
    source_item_count: 40, selected_question_count: 5, item_ids: indexes.map(i => `item-${i}`) };
  return { round, marker, indexes, document: { schema: 'foldarium.weekly-question-recommendations/v1', recommendations: [marker] } };
}
function declaration(source, signature) {
  const start = source.indexOf(signature); assert.notEqual(start, -1, signature);
  const open = source.indexOf('{', start + signature.length - 1); let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unbalanced ${signature}`);
}
async function install(signatures, sandbox) {
  const source = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const context = vm.createContext(sandbox);
  for (const signature of signatures) vm.runInContext(declaration(source, signature), context);
  return { context, source };
}

test('drug-like recommendations bind only to their exact round and full blind population', () => {
  const { round, document, indexes } = fixture();
  const before = structuredClone(round);
  const scope = druglikeQuestionScope(round, document);
  assert.equal(scope.mode, 'druglike');
  assert.deepEqual(scopedWeeklyQuestionIndexes(round.blind_manifest.items, scope), indexes);
  assert.deepEqual(round, before, 'no full manifest or original assignment is rewritten');
  for (const change of [
    marker => { marker.round_id = 'another-round'; },
    marker => { marker.blind_manifest_sha256 = 'c'.repeat(64); },
    marker => { marker.source_item_count = 39; },
    marker => { marker.selected_question_count = 4; },
    marker => { marker.item_ids[0] = 'missing'; },
    marker => { marker.item_ids[0] = marker.item_ids[1]; },
    marker => { marker.item_ids = []; marker.selected_question_count = 0; },
    marker => { marker.selection_sha256 = ['b'.repeat(64)]; },
    marker => { marker.policy = 'unrecognized'; },
  ]) {
    const copy = structuredClone(document); change(copy.recommendations[0]);
    assert.equal(druglikeQuestionScope(round, copy), null);
  }
  const ambiguous = structuredClone(document); ambiguous.recommendations.push(ambiguous.recommendations[0]);
  assert.equal(druglikeQuestionScope(round, ambiguous), null);
});

test('optional same-origin recommendation fetch fails back safely and cannot hold startup indefinitely', async () => {
  const { round, document } = fixture(); let calls = 0;
  const scope = await loadDruglikeQuestionScope(round, { fetchImpl: async (url, options) => {
    calls++; assert.equal(url, '/weekly-question-recommendations.json'); assert.equal(options.cache, 'no-cache');
    return { ok: true, json: async () => document };
  } });
  assert.equal(calls, 1); assert.equal(scope.mode, 'druglike');
  for (const fetchImpl of [
    async () => { throw new Error('offline'); },
    async () => ({ ok: false }),
    async () => ({ ok: true, json: async () => { throw new Error('invalid JSON'); } }),
    async () => ({ ok: true, json: async () => ({ ...document, recommendations: [] }) }),
  ]) assert.equal(await loadDruglikeQuestionScope(round, { fetchImpl }), null);
  assert.equal(await loadDruglikeQuestionScope(round, { timeoutMs: 5,
    fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')))),
  }), null);
});

test('sampler resume keeps its own identity and preserves old featured and all-question sessions', () => {
  const { round, document } = fixture(); const scope = druglikeQuestionScope(round, document);
  let token = null; const storage = { getItem: () => token, setItem: (_k, value) => { token = value; } };
  const store = createWeeklySessionResumeStore(storage);
  store.save({ sessionId: 'session', roundId: round.round_id, questionIndex: 27, scope });
  assert.deepEqual(store.read().scope, scope);
  scope.item_ids.push('changed'); assert.equal(store.read().scope.item_ids.length, 5);
  const original = { mode: 'featured', blind_manifest_sha256: round.blind_manifest_sha256, item_ids: ['item-0'] };
  store.save({ sessionId: 'existing', roundId: round.round_id, questionIndex: 0, scope: original });
  assert.deepEqual(store.read().scope, original);
  assert.deepEqual(parseWeeklyQuestionScope({ mode: 'all' }), { mode: 'all' });
});

test('real arrow handlers traverse all five sampler items in both directions and retain full ordinals', async () => {
  const { round, document, indexes } = fixture(); const items = round.blind_manifest.items;
  const elements = new Map([['#question-nav', { style: {} }], ['#question-prev', {}], ['#question-next', {}]]);
  const visited = []; let context;
  const result = await install([
    'function weeklyScopeQuestionIndexes(items = ITEMS)', 'function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter)',
    'function adjacentRetrospectiveQuestionIndex(direction)', 'async function navigateWeeklyQuestion(', 'function syncQuestionNavigation()',
  ], {
    ITEMS: items, cur: { item: items[1] }, quizSource: 'weekly', idx: 1,
    weeklyQuestionScope: druglikeQuestionScope(round, document), retrospectiveQuestionFilter: 'all',
    viewerTransitionBusy: false, revealRequested: false, isRetrospectiveReview: () => false,
    window: { foldariumFeaturedQuestions: { scopedWeeklyQuestionIndexes } }, $: key => elements.get(key),
    syncWeeklyQuestionScope() {}, syncRetrospectiveQuestionFilter() {}, syncWeeklyGuideContent() {}, rememberWeeklyItemState() {}, recordAppEvent() {},
    async loadQuestion(index) { visited.push(index); context.idx = index; context.cur = { item: items[index] }; context.syncQuestionNavigation(); },
  });
  context = result.context;
  for (const key of ['#question-prev', '#question-next']) vm.runInContext(declaration(result.source, `$('${key}').onclick = () =>`), context);
  context.syncQuestionNavigation();
  for (let i = 0; i < indexes.length; i++) {
    assert.equal(context.idx, indexes[i]); assert.equal(elements.get('#question-prev').disabled, i === 0);
    assert.equal(elements.get('#question-next').disabled, i === 4);
    if (i < 4) await elements.get('#question-next').onclick();
  }
  for (const index of indexes.slice(0, -1).reverse()) { await elements.get('#question-prev').onclick(); assert.equal(context.idx, index); }
  assert.deepEqual(visited, [9, 16, 27, 38, 27, 16, 9, 1]);
  assert.equal(context.ITEMS.length, 40);
});

test('a sampler fifth vote saves its canonical item but does not complete the full-round session or original assignment', async () => {
  const { round, document } = fixture(); const scope = druglikeQuestionScope(round, document);
  const verdict = { style: {}, textContent: '' }, submissions = [], events = [];
  const { context } = await install([
    'function weeklyScopeQuestionIndexes(items = ITEMS)', 'function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter)',
    'function nextSessionQuestionIndex()', 'async function finalizeWeeklyVote({ postReveal = false } = {})',
  ], {
    ITEMS: round.blind_manifest.items, quizSource: 'weekly', idx: 38,
    weeklyQuestionScope: scope, retrospectiveQuestionFilter: 'all', isRetrospectiveReview: () => false,
    window: { foldariumFeaturedQuestions: { scopedWeeklyQuestionIndexes } },
    cur: { item: { id: 'item-38' }, selected: { _weeklyChoiceId: 'pose', none: false } },
    WEEKLY_ROUND: round, WEEKLY_VOTES: new Map(scope.item_ids.slice(0, 4).map(id => [id, {}])), remoteSessionId: 'session',
    viewerTraceRecorder: null, weeklyTraceStream: null, $: () => verdict, isReadOnlyPreview: () => false, setVoteStatus() {},
    researchBackend: () => ({ submitWeeklyVoteAttempt: async vote => submissions.push(vote), completeSession: () => assert.fail('partial round stays revisable') }),
    currentReplayableAppState: () => ({ question_index: 38 }), newVoteAttemptId: () => 'vote', recordAppEvent: event => events.push(event),
    rememberWeeklyItemState() {}, renderUI() {}, loadQuestion: () => assert.fail('sampler ends at five'),
  });
  await context.finalizeWeeklyVote();
  assert.equal(submissions[0].questionIndex, 38); assert.equal(submissions[0].itemId, 'item-38');
  assert.match(verdict.textContent, /5 of 5 drug-like sampler votes saved/);
  assert.ok(events.includes('druglike_picks_finished')); assert.ok(!events.includes('featured_questions_finished'));
  assert.ok(!events.includes('quiz_completed'));
});

test('sampler results use full-round scores even if the original featured cohort exists and was selected earlier', async () => {
  const host = { hidden: false, innerHTML: '', replaceChildren() {} };
  const { context } = await install(['function renderWeeklyLeaderboard()'], {
    WEEKLY_ONLY: true, WEEKLY_ROUND: { public_status: 'revealed' }, isRetrospectiveReview: () => false,
    isPrivatePrecloseReview: () => false, isArchiveRetrospective: () => false,
    WEEKLY_LEADERBOARD_ERROR: '', WEEKLY_LEADERBOARD: { scorable_item_count: 40, complete_runs: [], partial_runs: [] },
    WEEKLY_FOR_FUN_LEADERBOARD: null,
    WEEKLY_FEATURED_RESULTS: { assignment_total: 5, participants: [{ participant: 'Original cohort only', participant_kind: 'human' }] },
    weeklyQuestionScope: { mode: 'druglike' }, weeklyResultsScope: 'featured', ITEMS: [], participantDisplayName: '',
    localWeeklyScore: { answered: 0, correct: 0 }, bindWeeklyResultsScope() {},
    $: key => key === '#wrap' ? { classList: { contains: () => true } } : host,
  });
  context.renderWeeklyLeaderboard();
  assert.match(host.innerHTML, /Drug-like sampler.*results cover the full round/);
  assert.doesNotMatch(host.innerHTML, /Original cohort only|Featured players|data-weekly-result-scope/);
  context.weeklyQuestionScope = { mode: 'featured' };
  context.WEEKLY_FEATURED_RESULTS.participants = [];
  context.renderWeeklyLeaderboard();
  assert.match(host.innerHTML, /Featured players/);
});

test('fresh startup promotes the sampler while actual resume retains an existing original assignment', async () => {
  const { round, document } = fixture(); const sampler = druglikeQuestionScope(round, document);
  const original = { mode: 'featured', blind_manifest_sha256: round.blind_manifest_sha256, item_ids: ['item-0', 'item-7'] };
  let started = null, resumed = 0;
  const { context } = await install(['async function initializeWeeklyQuestionScopes()', 'async function resumeWeeklyQuizIfAvailable()'], {
    WEEKLY_ROUND: { ...round, public_status: 'open' }, weeklyQuestionScope: { mode: 'all' }, weeklyFeaturedScope: null,
    weeklyDruglikeScope: null, weeklyResultsScope: 'featured', POOLS: { weekly: round.blind_manifest.items },
    isRetrospectiveReview: () => false, DEV: false, PERFORMANCE_RECORDING_REQUESTED: false,
    isReadOnlyPreview: () => false, quizSource: 'weekly',
    window: {
      foldariumFeaturedQuestions: { featuredQuestionScope: () => original,
        loadDruglikeQuestionScope: async () => sampler, scopedWeeklyQuestionIndexes },
      foldariumWeeklySessionResume: {
        read: () => ({ round_id: round.round_id, session_id: 'existing', phase: 'blind', question_index: 7, scope: original }),
        clear: () => assert.fail('valid original session must remain available'),
      },
    },
    researchBackend: () => ({ resumeNamedWeeklySession: async () => { resumed++; return { sessionId: 'existing' }; } }),
    beginQuiz: index => { started = index; },
  });
  await context.initializeWeeklyQuestionScopes();
  assert.equal(context.weeklyQuestionScope, sampler, 'a fresh participant starts in the curated sampler');
  assert.equal(context.weeklyFeaturedScope, original, 'the canonical assignment remains separate');
  assert.equal(await context.resumeWeeklyQuizIfAvailable(), true);
  assert.equal(context.weeklyQuestionScope, original, 'an existing participant retains their chosen assignment');
  assert.equal(context.weeklyDruglikeScope, sampler);
  assert.equal(resumed, 1); assert.equal(started, 7);
});

test('switching sampler, original assignment and full round retains votes and canonical positions', async () => {
  const { round, document } = fixture(); const sampler = druglikeQuestionScope(round, document);
  const original = { mode: 'featured', blind_manifest_sha256: round.blind_manifest_sha256, item_ids: ['item-0', 'item-5'] };
  const saved = new Map([['item-1', { choice_id: 'saved-pose', picked_none: false }]]);
  const before = structuredClone([...saved]); const positions = []; let context;
  ({ context } = await install(['function weeklyScopeQuestionIndexes(items = ITEMS)', 'async function setWeeklyQuestionScope(mode)'], {
    ITEMS: round.blind_manifest.items, quizSource: 'weekly', idx: 1, cur: { item: round.blind_manifest.items[1] },
    weeklyQuestionScope: sampler, weeklyFeaturedScope: original, weeklyDruglikeScope: sampler, weeklyResultsScope: 'full',
    viewerTransitionBusy: false, revealRequested: false, WEEKLY_VOTES: saved,
    isRetrospectiveReview: () => false, window: { foldariumFeaturedQuestions: { scopedWeeklyQuestionIndexes } },
    syncWeeklyQuestionScope() {}, recordAppEvent() {}, rememberWeeklyItemState() {},
    saveWeeklyResumePosition: index => positions.push(index),
    async loadQuestion(index) { context.idx = index; context.cur = { item: round.blind_manifest.items[index] }; },
  }));
  await context.setWeeklyQuestionScope('all'); assert.equal(context.idx, 1);
  await context.setWeeklyQuestionScope('featured'); assert.equal(context.idx, 0); assert.equal(context.weeklyQuestionScope, original);
  await context.setWeeklyQuestionScope('druglike'); assert.equal(context.idx, 1); assert.equal(context.weeklyQuestionScope, sampler);
  assert.equal(context.weeklyResultsScope, 'full');
  assert.deepEqual(positions, [1, 0, 1]); assert.deepEqual([...saved], before);
  assert.equal(context.ITEMS.length, 40);
  context.viewerTransitionBusy = true;
  await context.setWeeklyQuestionScope('all'); assert.equal(context.weeklyQuestionScope, sampler);
});

test('shipped current-round recommendation file activates the intended sampler against its public manifest identity', async () => {
  const document = JSON.parse(await readFile(new URL('../weekly-question-recommendations.json', import.meta.url), 'utf8'));
  const round = JSON.parse(await readFile(new URL('./fixtures/druglike-current-round.json', import.meta.url), 'utf8'));
  const scope = druglikeQuestionScope(round, document);
  assert.ok(scope, 'the deployed data file must not silently fall back to the old assignment');
  assert.equal(scope.mode, 'druglike');
  assert.equal(scope.policy, 'foldarium.medchem-presentation-preference/v1');
  assert.equal(scope.selection_sha256, '7aef929bf7495614678fe5995d231c0bb31ae818bcd87354413b6886d11b0a00');
  assert.deepEqual(scope.item_ids, ['27WI', '28FN', '9QY6', '9SYP', '9YUB']);
  assert.equal(scopedWeeklyQuestionIndexes(round.blind_manifest.items, scope).length, 5);
  const fixtureText = JSON.stringify(round);
  assert.doesNotMatch(fixtureText, /rmsd|correct|smiles|coordinate|private_index|reveal_manifest/);
});
