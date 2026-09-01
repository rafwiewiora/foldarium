import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  ALLOWED_ROUND_ID,
  PRIVATE_EVALUATION_FORMAT_VERSION,
  activatePrivateReview,
  buildSyntheticReviewRound,
  clearLegacyStoredBundle,
  deactivatePrivateReview,
  fetchPrivateEvaluationBundle,
  isPreviewDeployment,
  isTrustedBundle,
  mountPrivateReviewUi,
  selectRetrospectiveAnswer,
  validatePrivateReviewRendering,
  validateWeeklyLeaderboard,
  validateWeeklyQuestionResults,
} from '../weekly-private-review.js';
import { buildFixture, buildIncompletePoolFixture } from './private-evaluation-fixtures.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');

class MemoryStorage {
  constructor() { this.map = new Map(); }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null; }
  setItem(key, value) { this.map.set(key, String(value)); }
  removeItem(key) { this.map.delete(key); }
}

async function trustedBundleFromFetch(bundle) {
  return fetchPrivateEvaluationBundle({
    fetchImpl: async () => ({
      ok: true,
      json: async () => bundle,
    }),
  });
}

test('private review bundle round identity is fixed to the production beta catch-up round', () => {
  assert.equal(ALLOWED_ROUND_ID, 'weekly-2026-08-08-beta-v5-global-tm-29');
});

test('retrospective answer selection focuses the closest accepted raw pose', () => {
  const accepted = selectRetrospectiveAnswer({
    choices: [
      { id: 'wrong', correct: false, rmsd: 0.2 },
      { id: 'accepted-far', correct: true, rmsd: 1.7 },
      { id: 'accepted-best', correct: true, rmsd: 0.8 },
    ],
  });
  assert.equal(accepted.id, 'accepted-best');
  assert.equal(selectRetrospectiveAnswer({
    choices: [{ id: 'wrong', correct: false, rmsd: 0.2 }],
  }), null);
});

test('client bundle validation requires v5 format version', async () => {
  const { bundle } = buildFixture();
  assert.equal(bundle.format_version, PRIVATE_EVALUATION_FORMAT_VERSION);
  const legacy = structuredClone(bundle);
  legacy.format_version = 'foldarium.weekly-private-evaluation/v1';
  await assert.rejects(
    () => trustedBundleFromFetch(legacy),
    /Private evaluation bundle is invalid/,
  );
});

test('arbitrary or tampered bundles cannot activate private review', async () => {
  deactivatePrivateReview();
  const { bundle } = buildFixture();
  assert.equal(activatePrivateReview(bundle), null);
  assert.equal(buildSyntheticReviewRound(bundle), null);
  assert.notEqual(globalThis.FOLDARIUM_PRIVATE_REVIEW?.active, true);

  const tampered = structuredClone(bundle);
  tampered.reveal_manifest_sha256 = 'ff'.repeat(32);
  assert.equal(activatePrivateReview(tampered), null);
  assert.equal(buildSyntheticReviewRound(tampered), null);
});

test('only bundles returned by fetchPrivateEvaluationBundle can activate private review', async () => {
  const { bundle } = buildFixture();
  const trusted = await trustedBundleFromFetch(bundle);
  assert.equal(isTrustedBundle(trusted), true);
  const state = activatePrivateReview(trusted);
  assert.equal(state.active, true);
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW.evaluation_id, bundle.evaluation_id);
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW.reveal_digest_tail, bundle.reveal_manifest_sha256.slice(-8));
  deactivatePrivateReview();
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW.active, false);
});

test('buildSyntheticReviewRound constructs an in-memory production review round from trusted bundles', async () => {
  const { bundle } = buildFixture();
  const trusted = await trustedBundleFromFetch(bundle);
  const synthetic = buildSyntheticReviewRound(trusted);
  assert.equal(synthetic.public_status, 'open');
  assert.equal(synthetic.round_id, ALLOWED_ROUND_ID);
  assert.equal(synthetic.blind_manifest, bundle.blind_manifest);
  assert.equal(synthetic.reveal_manifest, bundle.reveal_manifest);
  assert.equal(synthetic.reveal_manifest_sha256, bundle.reveal_manifest_sha256);
  assert.equal(buildSyntheticReviewRound({ round_id: 'other' }), null);
});

test('validatePrivateReviewRendering rejects incomplete pools before activation', async () => {
  const trusted = await trustedBundleFromFetch(buildFixture().bundle);
  const cases = [
    ['item-count', /item count mismatch/],
    ['protein', /missing a protein asset/],
    ['pose', /missing a pose asset/],
    ['rmsd', /missing a reveal score/],
    ['correct', /missing correctness/],
    ['choice-id', /choice identities differ/],
    ['released-crystal', /reference_uri does not match the blind target id/],
  ];
  for (const [missing, pattern] of cases) {
    const { bundle, pool } = buildIncompletePoolFixture({ missing });
    const caseTrusted = await trustedBundleFromFetch(bundle);
    assert.throws(
      () => validatePrivateReviewRendering(caseTrusted, pool),
      pattern,
    );
  }
  assert.doesNotThrow(() => validatePrivateReviewRendering(trusted, buildIncompletePoolFixture().pool));
});

test('preview-only control stays hidden outside preview deployments', () => {
  const document = {
    getElementById(id) {
      return ({
        'private-review-control': { hidden: true },
        'private-review-banner': { hidden: true },
        'private-review-form': { addEventListener() {}, reset() {} },
        'private-review-status': { textContent: '', dataset: {} },
        'private-review-password': { value: '' },
        'private-review-clear': { addEventListener() {} },
      })[id] || null;
    },
  };
  const hidden = mountPrivateReviewUi({ config: { deploymentEnvironment: 'production' }, document });
  assert.equal(hidden.visible, false);
  assert.equal(document.getElementById('private-review-control').hidden, true);
  assert.equal(isPreviewDeployment({ deploymentEnvironment: 'production' }), false);
  assert.equal(isPreviewDeployment({ deploymentEnvironment: 'preview' }), true);
});

test('weekly shell exposes preview-only private review UI and production guards in app.js', async () => {
  const [html, app, review] = await Promise.all([
    read('index.html'), read('app.js'), read('weekly-private-review.js'),
  ]);
  assert.match(html, /id="private-review-control"/);
  assert.match(html, /id="private-review-banner"/);
  assert.match(html, />\s*Retrospective\s*</);
  assert.match(html, /#private-review-banner\[data-active="true"\]\{display:block\}/);
  assert.doesNotMatch(html, /correct prediction green|crystal ligand magenta/);
  assert.doesNotMatch(html, /private-review-password|Private evaluation password|Load private evaluation/);
  assert.doesNotMatch(review, /FOLDARIUM_PREVIEW_PRIVATE_EVALUATION_PASSWORD|Invalid password/);
  assert.match(html, /weekly-private-review\.js/);
  assert.match(app, /const isPrivatePrecloseReview = \(\) => window\.FOLDARIUM_PRIVATE_REVIEW\?\.active === true/);
  assert.match(app, /const isRetrospectiveReview = \(\) => isPrivatePrecloseReview\(\) \|\| isArchiveRetrospective\(\)/);
  assert.match(app, /!isPrivatePrecloseReview\(\)/);
  assert.match(app, /window\.foldariumApplyPrivateReviewBundle/);
  assert.match(app, /buildSyntheticReviewRound/);
  assert.match(app, /validatePrivateReviewRendering/);
  assert.match(app, /enrichPrivateWeeklyPool/);
  assert.match(app, /const privateVoteTotals = new Map\(\)/);
  assert.match(app, /normalizeWeekly\(synthetic, privateVoteTotals\)/);
  assert.match(app, /WEEKLY_TOTALS = privateVoteTotals/);
  assert.match(app, /DEV \|\| isRetrospectiveReview\(\)\) \{\s*button\.disabled = false;[\s\S]*return;\s*\}/);
  assert.match(app, /participant-setup'\)\.style\.display = DEV \|\| isRetrospectiveReview\(\) \? 'none' : ''/);
  assert.match(app, /quickStart\.hidden = !visible/);
  assert.match(app, /textContent = 'Scoring rules'/);
  assert.match(app, /Clusters use 2\.0 Å/);
  assert.match(app, /representative is the medoid/);
  assert.match(app, /Selector\/API ballots can submit independent cluster and exact-pose decisions/);
  assert.match(app, /Scoring uses 1\.5 Å/);
  assert.match(app, /Yellow marks a pose outside 1\.5 Å that belongs to a correct cluster/);
  assert.match(html, /id="retrospective-question-filter-select"/);
  assert.match(app, /Correct pose · someone right/);
  assert.match(app, /No pose · nobody chose None/);
  assert.match(app, /adjacentRetrospectiveQuestionIndex/);
  assert.match(app, /`\$\{pool\.length\} retrospective questions\.`/);
  assert.match(app, /buildReleasedCrystalScene/);
  assert.match(app, /View released crystal structure/);
  assert.match(app, /Open in RCSB ↗/);
  assert.match(app, /releasedCrystalMode/);
  assert.match(app, /applyAnswerRevealView/);
  assert.match(app, /renderWeeklyLeaderboard/);
  assert.match(app, /\/api\/weekly-retrospectives\?limit=50/);
  assert.match(app, /Best match/);
  assert.match(app, /applyRetrospectiveAnswer/);
  assert.doesNotMatch(app, /Crystal answer|Green means exact-correct|Private review complete/);
  assert.match(app, /if \(isRetrospectiveReview\(\)\) \{[\s\S]*v\.style\.display = 'none'/);
  assert.match(app, /details\.hidden = false[\s\S]*details\.open = true[\s\S]*details\.dataset\.privateReview = 'true'/);
  assert.match(html, /#answer-details\[data-private-review="true"\]>summary/);
  assert.match(app, /foldarium-private-review-ready/);
  assert.match(html, /id="xtal-label"/);
  assert.match(html, /id="rcsb-link"/);
  assert.match(html, /id="xtal-status"/);
  assert.match(html, /applyPrivateReviewBundleWhenReady/);
  assert.match(html, /foldarium-private-review-ready/);
  assert.match(html, /app\.js\?v=2026090101/);
  assert.match(app, /if \(WEEKLY_ONLY\) \{\s*showIntro\(\);\s*await startQuiz\(\);\s*\}/);
  assert.doesNotMatch(app, /Private pre-close review loaded|Answers stay non-public/);
  assert.doesNotMatch(app, /readStoredBundle/);
  assert.doesNotMatch(app, /if \(privateBundle\)/);
  assert.match(app, /if \(!isRetrospectiveReview\(\) && !postRevealVote\) logAnswer/);
  assert.doesNotMatch(app, /applyPrivateReviewRound/);
});

test('private review mode hides vote totals in reveal rendering', async () => {
  const app = await read('app.js');
  assert.match(app, /cur\.item\.source === 'weekly' && !isPrivatePrecloseReview\(\)/);
});

test('validateWeeklyLeaderboard rejects tampered private bundle leaderboard payloads', () => {
  const leaderboard = {
    format_version: 'foldarium.weekly-leaderboard/v1',
    round_id: ALLOWED_ROUND_ID,
    item_count: 29,
    participant_count: 1,
    complete_runs: [{
      display_name: 'Ada',
      correct: 20,
      answered: 29,
      total: 29,
      accuracy: 69,
      coverage: 100,
      rank: 1,
    }],
    partial_runs: [],
  };
  assert.doesNotThrow(() => validateWeeklyLeaderboard(leaderboard));
  const leaked = structuredClone(leaderboard);
  leaked.complete_runs[0].session_id = 'secret';
  assert.throws(() => validateWeeklyLeaderboard(leaked), /forbidden field/);
});

test('validateWeeklyQuestionResults binds names and counts to revealed choices', () => {
  const revealManifest = {
    items: [{
      id: 'ITEM01',
      choices: [
        { id: 'choice-a', accepted_correct: true },
        { id: 'choice-b', accepted_correct: false },
      ],
    }],
  };
  const results = {
    format_version: 'foldarium.weekly-question-results/v2',
    round_id: ALLOWED_ROUND_ID,
    item_count: 1,
    items: [{
      item_id: 'ITEM01',
      answered_count: 2,
      correct_count: 1,
      correct_display_names: ['Ada'],
      answers: [{
        choice_id: 'choice-a',
        picked_none: false,
        selection_kind: 'cluster',
        correct: true,
        vote_count: 1,
        display_names: ['Ada'],
      }, {
        choice_id: 'choice-b',
        picked_none: false,
        selection_kind: 'exact',
        correct: false,
        vote_count: 1,
        display_names: ['Grace'],
      }],
    }],
  };
  assert.equal(validateWeeklyQuestionResults(results, { revealManifest }), results);
  assert.throws(
    () => validateWeeklyQuestionResults({
      ...results,
      items: [{
        ...results.items[0],
        correct_count: 2,
        correct_display_names: ['Ada', 'Grace'],
      }],
    }, { revealManifest }),
    /totals are inconsistent/,
  );
});

function reviewDocument() {
  const nodes = {
    'private-review-control': { hidden: true },
    'private-review-banner': { hidden: true, dataset: {} },
    'private-review-status': { textContent: '', dataset: {} },
  };
  return { nodes, document: { getElementById: id => nodes[id] || null } };
}

test('legacy sessionStorage bundles are cleared before automatic review loading', async () => {
  const storage = new MemoryStorage();
  const { bundle } = buildFixture();
  storage.setItem('foldariumPrivateEvalBundle', JSON.stringify(bundle));
  const { document } = reviewDocument();
  const originalSessionStorage = globalThis.sessionStorage;
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: storage,
  });
  try {
    deactivatePrivateReview();
    const mounted = mountPrivateReviewUi({
      config: { deploymentEnvironment: 'preview' },
      document,
      fetchImpl: async () => ({ ok: true, json: async () => bundle }),
    });
    await mounted.ready;
    assert.equal(storage.getItem('foldariumPrivateEvalBundle'), null);
    assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW?.active, true);
  } finally {
    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true,
      value: originalSessionStorage,
    });
  }
  clearLegacyStoredBundle(storage);
  assert.equal(storage.getItem('foldariumPrivateEvalBundle'), null);
});

test('mountPrivateReviewUi automatically activates after rendering succeeds', async () => {
  const { bundle } = buildFixture();
  let loaded = null;
  const { nodes, document } = reviewDocument();
  deactivatePrivateReview();
  const mounted = mountPrivateReviewUi({
    config: { deploymentEnvironment: 'preview' },
    document,
    fetchImpl: async () => ({
      ok: true,
      json: async () => bundle,
    }),
    onBundleLoaded: value => {
      loaded = value;
      activatePrivateReview(value);
    },
  });
  await mounted.ready;
  assert.equal(isTrustedBundle(loaded), true);
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW?.active, true);
  assert.equal(nodes['private-review-banner'].hidden, false);
  assert.equal(nodes['private-review-control'].hidden, true);
});

test('mountPrivateReviewUi stays inactive when onBundleLoaded throws', async () => {
  const { bundle } = buildFixture();
  const { nodes, document } = reviewDocument();
  deactivatePrivateReview();
  const mounted = mountPrivateReviewUi({
    config: { deploymentEnvironment: 'preview' },
    document,
    fetchImpl: async () => ({ ok: true, json: async () => bundle }),
    onBundleLoaded: () => { throw new Error('rendering failed'); },
  });
  await mounted.ready;
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW?.active, false);
  assert.equal(nodes['private-review-banner'].hidden, true);
  assert.match(nodes['private-review-status'].textContent, /rendering failed/);
});

test('mountPrivateReviewUi without callback activates after fetch validation', async () => {
  const { bundle } = buildFixture();
  const { nodes, document } = reviewDocument();
  deactivatePrivateReview();
  const mounted = mountPrivateReviewUi({
    config: { deploymentEnvironment: 'preview' },
    document,
    fetchImpl: async () => ({ ok: true, json: async () => bundle }),
  });
  await mounted.ready;
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW?.active, true);
  assert.equal(nodes['private-review-banner'].hidden, false);
});

test('deactivatePrivateReview clears automatic review state without persisting bundles', async () => {
  const { bundle } = buildFixture();
  const trusted = await trustedBundleFromFetch(bundle);
  activatePrivateReview(trusted);
  deactivatePrivateReview();
  assert.equal(globalThis.FOLDARIUM_PRIVATE_REVIEW.active, false);
});
