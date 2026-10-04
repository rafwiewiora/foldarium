import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import {
  featuredQuestionScope,
  scopedWeeklyQuestionIndexes,
} from '../weekly-featured-questions.js';

function roundFixture() {
  return {
    blind_manifest_sha256: 'a'.repeat(64),
    blind_manifest: { items: ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(id => ({ id })) },
    featured_questions: {
      schema_version: 1, policy: 'foldarium-weekly-question-draw/v1', mode: 'uniform',
      seed: 'weekly-fixture', blind_manifest_sha256: 'a'.repeat(64),
      candidate_population_sha256: 'b'.repeat(64), selection_sha256: 'c'.repeat(64),
      source_item_count: 7, candidate_count: 7, requested_question_count: 5,
      selected_question_count: 5, item_ids: ['b', 'c', 'd', 'e', 'g'],
    },
  };
}

test('featured scope binds to the full manifest and preserves canonical vote indices', () => {
  const round = roundFixture();
  const before = structuredClone(round);
  const scope = featuredQuestionScope(round);
  assert.equal(scope.mode, 'featured');
  assert.deepEqual(scopedWeeklyQuestionIndexes(round.blind_manifest.items, scope), [1, 2, 3, 4, 6]);
  assert.deepEqual(scopedWeeklyQuestionIndexes(round.blind_manifest.items, { mode: 'all' }), [0, 1, 2, 3, 4, 5, 6]);
  assert.deepEqual(round, before, 'full scientific manifest is never sliced or rewritten');
});

test('invalid or absent selection markers leave the full quiz available', () => {
  assert.equal(featuredQuestionScope({}), null);
  for (const mutate of [
    round => { round.featured_questions.blind_manifest_sha256 = 'd'.repeat(64); },
    round => { round.featured_questions.item_ids[0] = 'missing'; },
    round => { round.featured_questions.item_ids[0] = 'c'; },
    round => { round.featured_questions.source_item_count = 6; },
    round => { round.featured_questions.selected_question_count = 4; },
    round => { round.featured_questions.requested_question_count = 6; },
    round => { round.featured_questions.policy = 'unrecognized'; },
    round => { round.featured_questions.mode = 'post-reveal-answers'; },
  ]) {
    const round = roundFixture();
    mutate(round);
    assert.equal(featuredQuestionScope(round), null);
  }
  assert.throws(() => scopedWeeklyQuestionIndexes([{ id: 'a' }], {
    mode: 'featured', item_ids: ['missing'],
  }), /unavailable/);
});

function declaration(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1);
  const open = source.indexOf('{', start + signature.length - 1);
  let depth = 0;
  for (let index = open; index < source.length; index++) {
    if (source[index] === '{') depth++;
    if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
  }
  throw new Error('Missing declaration');
}

test('featured navigation skips optional questions without renumbering persisted questions', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const round = roundFixture();
  const context = vm.createContext({
    ITEMS: round.blind_manifest.items,
    quizSource: 'weekly', idx: 4, retrospectiveQuestionFilter: 'all',
    weeklyQuestionScope: featuredQuestionScope(round),
    isRetrospectiveReview: () => false,
    window: { foldariumFeaturedQuestions: { scopedWeeklyQuestionIndexes } },
  });
  for (const signature of [
    'function weeklyScopeQuestionIndexes(items = ITEMS)',
    'function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter)',
    'function nextSessionQuestionIndex()',
  ]) vm.runInContext(declaration(app, signature), context);
  assert.equal(vm.runInContext('nextSessionQuestionIndex()', context), 6);
  context.idx = 6;
  assert.equal(vm.runInContext('nextSessionQuestionIndex()', context), null);
  context.weeklyQuestionScope = { mode: 'all' };
  context.idx = 4;
  assert.equal(vm.runInContext('nextSessionQuestionIndex()', context), 5);
  assert.match(app, /questionIndex: idx/);
  assert.match(app, /question_index: cur \? idx : null/);
  assert.match(app, /if \(quizSource === 'weekly'\) return pool\.slice\(\)/);
});

test('the fifth featured vote submits its full ordinal and ends with a revisable partial-round summary', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const round = roundFixture();
  const verdict = { style: {}, textContent: '' };
  const submissions = [];
  const events = [];
  const context = vm.createContext({
    ITEMS: round.blind_manifest.items,
    quizSource: 'weekly', idx: 6, retrospectiveQuestionFilter: 'all',
    weeklyQuestionScope: featuredQuestionScope(round),
    isRetrospectiveReview: () => false,
    window: { foldariumFeaturedQuestions: { scopedWeeklyQuestionIndexes } },
    cur: { item: { id: 'g' }, selected: { _weeklyChoiceId: 'pose-g', none: false } },
    WEEKLY_ROUND: { round_id: 'full-round' },
    WEEKLY_VOTES: new Map(['b', 'c', 'd', 'e'].map(id => [id, {}])),
    remoteSessionId: 'session-1', viewerTraceRecorder: null, weeklyTraceStream: null,
    $: () => verdict,
    isReadOnlyPreview: () => false,
    setVoteStatus: () => {},
    researchBackend: () => ({
      submitWeeklyVoteAttempt: async vote => submissions.push(vote),
      completeSession: () => assert.fail('featured completion must remain revisable'),
    }),
    currentReplayableAppState: () => ({ question_index: 6, item_id: 'g' }),
    newVoteAttemptId: () => 'vote-1',
    recordAppEvent: event => events.push(event),
    rememberWeeklyItemState: () => {}, renderUI: () => {},
    loadQuestion: () => assert.fail('the fifth featured question must not advance to an optional question'),
  });
  for (const signature of [
    'function weeklyScopeQuestionIndexes(items = ITEMS)',
    'function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter)',
    'function nextSessionQuestionIndex()',
    'async function finalizeWeeklyVote({ postReveal = false } = {})',
  ]) vm.runInContext(declaration(app, signature), context);
  await vm.runInContext('finalizeWeeklyVote()', context);
  assert.equal(submissions.length, 1);
  assert.equal(submissions[0].questionIndex, 6);
  assert.equal(submissions[0].itemId, 'g');
  assert.match(verdict.textContent, /5 of 5 featured votes saved/);
  assert.ok(events.includes('featured_questions_finished'));
  assert.ok(!events.includes('quiz_completed'));
  assert.equal(context.ITEMS.length, 7);
});
