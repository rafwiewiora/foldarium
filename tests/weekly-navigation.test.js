import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const readApp = () => readFile(new URL('../app.js', import.meta.url), 'utf8');

function declaration(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected ${signature} in app.js`);
  const open = source.indexOf('{', start + signature.length - 1);
  assert.notEqual(open, -1, `expected a block after ${signature}`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unbalanced braces after ${signature}`);
}

function installDeclarations(source, signatures, sandbox) {
  const context = vm.createContext(sandbox);
  for (const signature of signatures) {
    vm.runInContext(declaration(source, signature), context);
  }
  return context;
}

function navigationElements() {
  return new Map([
    ['#question-nav', { style: { display: '' } }],
    ['#question-prev', { disabled: false }],
    ['#question-next', { disabled: false }],
    ['#lock', { disabled: false }],
    ['#verdict', { style: { display: '' }, textContent: '' }],
  ]);
}

test('restored Weekly votes preserve exact, cluster, and none provenance', async () => {
  const app = await readApp();
  const exactChoice = { _weeklyChoiceId: 'choice-exact', correct: true };
  const clusterChoice = { _weeklyChoiceId: 'choice-cluster', correct: false };
  const clusters = [{ members: [exactChoice, clusterChoice] }];
  const context = installDeclarations(app, [
    'function restoreWeeklyPriorVote(questionState, prior, clusters)',
  ], {
    acceptedChoiceCorrect: choice => choice.correct,
  });

  const exactState = {};
  assert.equal(context.restoreWeeklyPriorVote(exactState, {
    choice_id: 'choice-exact',
    picked_none: false,
    selection_kind: 'exact',
  }, clusters), true);
  assert.equal(exactState.selected, exactChoice);
  assert.equal(exactState.selectionExact, true);
  assert.equal(exactState.selectedAsCluster, false);

  const clusterState = {};
  context.restoreWeeklyPriorVote(clusterState, {
    choice_id: 'choice-cluster',
    picked_none: false,
    selection_kind: 'cluster',
  }, clusters);
  assert.equal(clusterState.selected, clusterChoice);
  assert.equal(clusterState.selectionExact, false);
  assert.equal(clusterState.selectedAsCluster, true);

  const noneState = {};
  context.restoreWeeklyPriorVote(noneState, {
    choice_id: null,
    picked_none: true,
    selection_kind: 'none',
  }, clusters);
  assert.equal(noneState.selected.none, true);
  assert.equal(noneState.selectionExact, true);
  assert.equal(noneState.selectedAsCluster, false);
});

test('weekly vote completion releases question arrows after the next question rebuild', async () => {
  const app = await readApp();

  for (const commentState of [
    { name: 'comment disabled', enabled: false, handled: false },
    { name: 'comment submitted', enabled: true, handled: true },
  ]) {
    const elements = navigationElements();
    const blockedSnapshots = [];
    let context;
    const sandbox = {
      cur: { selected: { _weeklyChoiceId: 'choice-a' }, revealed: false,
        voteCommentHandled: commentState.handled },
      quizSource: 'weekly',
      viewerTransitionBusy: false,
      revealRequested: false,
      weeklyCommentPromptEnabled: commentState.enabled,
      idx: 14,
      ITEMS: Array.from({ length: 29 }, (_, index) => ({ id: `item-${index}` })),
      WEEKLY_ROUND: { public_status: 'open' },
      isPrivatePrecloseReview: () => false,
      isRetrospectiveReview: () => false,
      isReadOnlyPreview: () => false,
      syncRetrospectiveQuestionFilter() {},
      syncWeeklyGuideContent() {},
      retrospectiveQuestionIndexes: () => sandbox.ITEMS.map((_, index) => index),
      $: selector => elements.get(selector),
      setVoteStatus(message, state) {
        const verdict = elements.get('#verdict');
        verdict.style.display = '';
        verdict.textContent = message;
        verdict.dataset = { state };
      },
      recordAppEvent() {},
      openVoteCommentDialog() {
        assert.fail(`${commentState.name} should proceed directly to recording`);
      },
      shouldPromptForVoteComment() {
        return sandbox.weeklyCommentPromptEnabled && !sandbox.cur.voteCommentHandled;
      },
      async finalizeReveal() {
        // Successful voting advances to question 16. Its viewer rebuild finishes
        // before reveal() clears the outer revealRequested guard.
        context.viewerTransitionBusy = true;
        context.syncQuestionNavigation();
        context.idx = 15;
        context.viewerTransitionBusy = false;
        context.syncQuestionNavigation();
        blockedSnapshots.push([
          elements.get('#question-prev').disabled,
          elements.get('#question-next').disabled,
        ]);
      },
      async revealAfterIdle() {},
    };
    context = installDeclarations(app, [
      'function syncQuestionNavigation()',
      'async function reveal()',
    ], sandbox);

    context.syncQuestionNavigation();
    assert.deepEqual([
      elements.get('#question-prev').disabled,
      elements.get('#question-next').disabled,
    ], [false, false], `${commentState.name}: middle-question arrows begin enabled`);

    await context.reveal();

    assert.deepEqual(blockedSnapshots, [[true, true]],
      `${commentState.name}: navigation stays blocked until voting and rebuild finish`);
    assert.deepEqual([
      elements.get('#question-prev').disabled,
      elements.get('#question-next').disabled,
    ], [false, false], `${commentState.name}: both arrows are resynchronized after advancing`);
  }
});

test('retrospective question filters combine pose availability and player success', async () => {
  const app = await readApp();
  const items = [
    { id: 'pose-solved', choices: [{ correct: true }] },
    { id: 'pose-unsolved', choices: [{ correct: true }, { correct: false }] },
    { id: 'none-solved', choices: [{ correct: false }] },
    { id: 'none-unsolved', choices: [{ correct: false }] },
  ];
  const context = installDeclarations(app, [
    'function weeklyItemHasCorrectPose(item)',
    'function weeklyQuestionResultForItem(item)',
    'function retrospectiveQuestionMatches(item, filter = retrospectiveQuestionFilter)',
    'function retrospectiveQuestionIndexes(filter = retrospectiveQuestionFilter)',
  ], {
    ITEMS: items,
    WEEKLY_QUESTION_RESULTS: {
      items: [
        { item_id: 'pose-solved', correct_count: 1 },
        { item_id: 'pose-unsolved', correct_count: 0 },
        { item_id: 'none-solved', correct_count: 2 },
        { item_id: 'none-unsolved', correct_count: 0 },
      ],
    },
    isPrivatePrecloseReview: () => true,
    isRetrospectiveReview: () => true,
  });

  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('pose')), [0, 1]);
  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('none')), [2, 3]);
  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('pose-solved')), [0]);
  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('pose-unsolved')), [1]);
  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('none-solved')), [2]);
  assert.deepEqual(Array.from(context.retrospectiveQuestionIndexes('none-unsolved')), [3]);
});

test('weekly navigation skips unanswered questions and restores revisable per-question state', async () => {
  const app = await readApp();
  const item0 = { id: 'item-0', source: 'weekly' };
  const item1 = { id: 'item-1', source: 'weekly' };
  const initialState = {
    item: item0,
    selected: null,
    rejectedChoiceIds: new Set(['choice-a']),
    voteCommentHandled: false,
    voteCommentText: null,
  };
  const events = [];
  let context;
  const sandbox = {
    cur: initialState,
    quizSource: 'weekly',
    viewerTransitionBusy: false,
    revealRequested: false,
    idx: 0,
    ITEMS: [item0, item1],
    shownOne: 2,
    gridMethodIndex: 1,
    WEEKLY_ITEM_STATES: new Map(),
    recordAppEvent: action => events.push(action),
    async loadQuestion(index) {
      context.idx = index;
      const item = context.ITEMS[index];
      context.cur = context.WEEKLY_ITEM_STATES.get(item.id) || {
        item,
        selected: null,
        rejectedChoiceIds: new Set(),
        voteCommentHandled: false,
        voteCommentText: null,
      };
    },
  };
  context = installDeclarations(app, [
    'function rememberWeeklyItemState()',
    'async function navigateWeeklyQuestion(',
  ], sandbox);

  await context.navigateWeeklyQuestion(1, 'question_next');
  assert.equal(context.idx, 1, 'an unanswered question can be skipped');
  assert.equal(context.WEEKLY_ITEM_STATES.get(item0.id), initialState);
  assert.equal(initialState.savedShownOne, 2);
  assert.equal(initialState.savedGridPage, 1);

  const revisedSelection = { _weeklyChoiceId: 'choice-b' };
  context.cur.selected = revisedSelection;
  context.cur.voteCommentText = 'revised note';
  context.cur.rejectedChoiceIds.add('choice-c');
  const revisedState = context.cur;
  await context.navigateWeeklyQuestion(0, 'question_previous');

  assert.equal(context.cur, initialState, 'returning restores the original question object');
  assert.deepEqual([...context.cur.rejectedChoiceIds], ['choice-a']);
  await context.navigateWeeklyQuestion(1, 'question_next');
  assert.equal(context.cur, revisedState, 'a later visit restores the revised question object');
  assert.equal(context.cur.selected, revisedSelection);
  assert.equal(context.cur.voteCommentText, 'revised note');
  assert.deepEqual([...context.cur.rejectedChoiceIds], ['choice-c']);
  assert.deepEqual(events, ['question_next', 'question_previous', 'question_next']);
});

test('comment preference changes invalidate only the pending vote, not question navigation', async () => {
  const app = await readApp();
  const signature = "$('#vote-comment-enabled').onchange = event =>";
  const assignment = declaration(app, signature);
  const body = assignment.slice(assignment.indexOf('{'));
  const events = [];
  let invalidations = 0;
  const context = vm.createContext({
    weeklyCommentPromptEnabled: true,
    invalidatePendingWeeklyVote: () => { invalidations += 1; },
    recordAppEvent: action => events.push(action),
  });
  const changeCommentPreference = vm.runInContext(`(event => ${body})`, context);

  changeCommentPreference({ target: { checked: false } });
  changeCommentPreference({ target: { checked: true } });

  assert.equal(context.weeklyCommentPromptEnabled, true);
  assert.equal(invalidations, 2);
  assert.deepEqual(events, ['vote_comment_disabled', 'vote_comment_enabled']);
});
