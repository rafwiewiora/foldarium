import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { answerCorrectnessLabel, formatAccuracy, humanAnswerSummary, questionOutcome, targetMethodOutcomes } from '../weekly-retrospectives.js';
import { buildRevealAnswerKey } from '../lib/weekly-results.js';

function declaration(source, signature) {
  const start = source.indexOf(signature), open = source.indexOf('{', start + signature.length - 1);
  assert.ok(start >= 0);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error('missing declaration');
}
async function appContext(signatures, sandbox) {
  const source = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const context = vm.createContext(sandbox);
  for (const signature of ['function weeklyItemUnscorable(item = cur?.item)', ...signatures]) {
    vm.runInContext(declaration(source, signature), context);
  }
  return context;
}
const item = { source: 'weekly', id: 'incomplete', evaluation_status: 'unscorable', choices: [{ id: 'pose', correct: null, rmsd: null }] };

test('retrospective unscorable question never synthesizes a winning None or enables answer colors', async () => {
  const context = await appContext(['function applyAnswerRevealView()', 'function applyRetrospectiveAnswer()'], {
    cur: { item, selected: { none: true, correct: true }, showAnswer: true },
    isRetrospectiveReview: () => true, allItemChoices: () => item.choices,
    resetCrystalViewState() {}, syncXtalRow() {},
  });
  assert.equal(context.applyRetrospectiveAnswer(), true);
  assert.equal(context.cur.selected, null);
  assert.equal(context.cur.showAnswer, false);
  assert.equal(context.cur.answerRevealBest, null);
  assert.equal(context.cur.revealed, true);
  assert.equal(context.cur.answerChoices.length, 1);
});

test('unscorable prior None is preserved with null correctness and cannot increment local score', async () => {
  const context = await appContext(['function restoreWeeklyPriorVote(questionState, prior, clusters)', 'function bumpLocalWeeklyScore(youRight)'], {
    cur: { item }, weeklyResultsRevealActive: () => true,
    localWeeklyScoredItems: new Set(), localWeeklyScore: { answered: 0, correct: 0 },
    acceptedChoiceCorrect: () => false, renderWeeklyLeaderboard() { throw new Error('must not score'); },
  });
  const state = { item };
  assert.equal(context.restoreWeeklyPriorVote(state, { picked_none: true }, [{ members: item.choices }]), true);
  assert.equal(state.selected.none, true);
  assert.equal(state.selected.correct, null);
  context.bumpLocalWeeklyScore(true);
  assert.equal(context.localWeeklyScore.answered, 0);
  assert.equal(context.localWeeklyScore.correct, 0);
  assert.equal(context.localWeeklyScoredItems.size, 0);
});

test('unscorable question renders without a selected answer or stale correct verdict', async () => {
  const nodes = new Map();
  const context = await appContext(['function unscorableReferenceMessage(item = cur?.item)', 'function renderRevealedQuestionUi()'], {
    cur: { item: { ...item, reference_disposition: { observed_heavy_atoms: 36, expected_heavy_atoms: 71 } }, selected: null },
    $: key => { if (!nodes.has(key)) nodes.set(key, { style: {}, dataset: { state: 'correct' } }); return nodes.get(key); },
    isRetrospectiveReview: () => true, nextSessionQuestionIndex: () => null, syncXtalRow() {},
  });
  context.renderRevealedQuestionUi();
  assert.match(nodes.get('#verdict').textContent, /36 of 71/);
  assert.equal(nodes.get('#verdict').dataset.state, undefined);
  assert.equal(nodes.get('#myview').style.display, 'none');
  assert.equal(nodes.get('#answer-details').hidden, true);
});

test('archive outcomes, model outcomes and null accuracy never turn exclusions into losses or None wins', () => {
  assert.equal(questionOutcome({ human_aggregate: { correct_count: null } }, item), 'unscorable');
  assert.equal(humanAnswerSummary({ answered_count: 2, correct_count: null }), '2 selections · not scored');
  assert.equal(answerCorrectnessLabel(null), 'not scored');
  assert.equal(formatAccuracy(null), 'Not scored');
  assert.deepEqual(targetMethodOutcomes({ choices: [{ id: 'pose', method: 'boltz2' }] }, item), [{ method: 'boltz2', oracle_success: null, top1_success: null }]);
});

test('legacy Aug8 scoring fails closed on an audited unscorable item', () => {
  const reference_sha256 = 'a'.repeat(64);
  const reveal = { items: [{ id: 'q', evaluation_status: 'unscorable', reference_disposition: {
    policy: 'foldarium.released-reference-disposition/v1', code: 'insufficient_reference_coverage', component_id: 'DRG',
    expected_heavy_atoms: 20, observed_heavy_atoms: 10, explicitly_unobserved_heavy_atoms: 10,
    reference_coverage: .5, minimum_reference_coverage: .8, reference_sha256,
  }, choices: [{ id: 'p', rmsd: null, correct: null, accepted_correct: null, reference_sha256 }] }] };
  assert.throws(() => buildRevealAnswerKey(reveal), /Legacy weekly results/);
});

test('weekly UI accepts explicitly scoped v2 for-fun results and rejects inconsistent populations', async () => {
  let payload = { format_version: 'foldarium.weekly-play-for-fun-leaderboard/v2', round_id: 'round', item_count: 2,
    scorable_item_count: 0, excluded_item_count: 2, complete_runs: [], partial_runs: [{ participation_mode: 'for_fun', correct: 0, answered: 0, total: 0, accuracy: null }] };
  const context = await appContext(['async function loadWeeklyPlayForFunLeaderboard()'], {
    cur: null, WEEKLY_FOR_FUN_LEADERBOARD: null, WEEKLY_ROUND: { round_id: 'round', public_status: 'revealed', item_count: 2 },
    URLSearchParams, console: { warn() {} }, fetch: async () => ({ ok: true, json: async () => payload }),
  });
  await context.loadWeeklyPlayForFunLeaderboard();
  assert.equal(context.WEEKLY_FOR_FUN_LEADERBOARD, payload);
  payload = { ...payload, scorable_item_count: 2 };
  await context.loadWeeklyPlayForFunLeaderboard();
  assert.equal(context.WEEKLY_FOR_FUN_LEADERBOARD, null);
});


test('missing disposition cannot turn null correctness into a successful None answer', () => {
  assert.throws(() => buildRevealAnswerKey({ items: [{ id: 'q', choices: [
    { id: 'p', correct: null, accepted_correct: null },
  ] }] }, { allowUnscorable: true }), /boolean correctness/);
});
