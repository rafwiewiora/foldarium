import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import {
  SELECTOR_PROMPT_PROFILE_ID,
  SELECTOR_PROMPT_SHA256,
} from '../lib/weekly-selector-prompt.js';

const read = path => readFile(new URL(`../${path}`, import.meta.url), 'utf8');
const HASH = character => character.repeat(64);
const EMPTY_ALLOWLIST_SHA256 = '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';

function block(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `expected ${signature} in app.js`);
  const open = source.indexOf('{', start + signature.length - 1);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return { start, end: index + 1 };
    }
  }
  throw new Error(`unbalanced braces after ${signature}`);
}

function evaluateDeclaration(source, signature, sandbox) {
  const { start, end } = block(source, signature);
  const context = vm.createContext(sandbox);
  return vm.runInContext(`(${source.slice(start, end)})`, context);
}

function submission() {
  return {
    schema_version: 'foldarium.selector-submission/v2',
    submission_id: '00000000-0000-4000-8000-000000000001',
    environment: 'preview',
    round_id: 'weekly-test',
    blind_manifest_sha256: HASH('a'),
    kit_sha256: HASH('b'),
    items: [{
      item_id: 'ITEM01',
      clustered: { selection_kind: 'cluster', cluster_id: 'cluster-a' },
      unclustered: { selection_kind: 'exact', choice_id: 'choice-a1' },
    }],
  };
}

test('weekly intro omits programmatic controls while keeping the results leaderboard', async () => {
  const html = await read('index.html');
  for (const removedId of [
    'programmatic-voting',
    'selector-download-kit',
    'selector-offline-tool',
    'selector-display-name',
    'selector-method-name',
    'selector-method-version',
    'selector-provider',
    'selector-model',
    'selector-model-version',
    'selector-prompt-profile-id',
    'selector-prompt-sha256',
    'selector-tools-sha256',
    'selector-config-sha256',
    'selector-network-policy',
    'selector-network-allowlist-sha256',
    'selector-create-token',
    'selector-copy-token',
    'selector-submission-file',
    'selector-submit-file',
    'selector-api-docs',
    'programmatic-voting-status',
  ]) {
    assert.doesNotMatch(html, new RegExp(`id="${removedId}"`));
  }
  assert.match(html, /id="weekly-selector-leaderboard"/);
});

test('token issuance sends the round-bound v2 identity and provenance contract', async () => {
  const app = await read('app.js');
  const requests = [];
  const statuses = [];
  const fields = {
    '#selector-api-token': { value: '' },
    '#selector-copy-token': { disabled: true },
  };
  const sandbox = {
    SELECTOR_API_TOKEN: '',
    SELECTOR_ROUND_DESCRIPTOR: {
      round_id: 'weekly-test',
      environment: 'preview',
      public_status: 'open',
    },
    readSelectorIdentityFields: () => ({
      displayName: 'Ada',
      methodName: 'rules',
      methodVersion: '2',
      provider: 'example',
      model: 'rules',
      modelVersion: '2',
      promptProfileId: SELECTOR_PROMPT_PROFILE_ID,
      promptSha256: SELECTOR_PROMPT_SHA256,
      toolsSha256: HASH('b'),
      configSha256: HASH('c'),
      networkPolicy: 'none',
      networkAllowlistSha256: EMPTY_ALLOWLIST_SHA256,
    }),
    getBrowserSupabaseAccessToken: async () => 'browser-token',
    setProgrammaticVotingStatus: message => statuses.push(message),
    $: selector => fields[selector] || null,
    fetch: async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          token: 'selector-token',
          round_id: 'weekly-test',
          environment: 'preview',
        }),
      };
    },
  };
  const createSelectorApiToken = evaluateDeclaration(
    app,
    'async function createSelectorApiToken()',
    sandbox,
  );
  await createSelectorApiToken();

  const body = JSON.parse(requests[0].options.body);
  assert.equal(body.round_id, 'weekly-test');
  assert.equal(body.environment, 'preview');
  assert.equal(body.provider, 'example');
  assert.equal(body.method_name, 'rules');
  assert.equal(body.method_version, '2');
  assert.equal(body.model_name, 'rules');
  assert.equal(body.model_version, '2');
  assert.equal(body.prompt_profile_id, SELECTOR_PROMPT_PROFILE_ID);
  assert.equal(body.prompt_sha256, SELECTOR_PROMPT_SHA256);
  assert.equal(body.tools_sha256, HASH('b'));
  assert.deepEqual(body.blindness_attestation, {
    schema_version: 'foldarium.selector-blindness-attestation/v1',
    workspace_policy: 'verified-kit-only',
    network_policy: 'none',
    network_allowlist_sha256: EMPTY_ALLOWLIST_SHA256,
    browser_enabled: false,
    web_search_enabled: false,
    external_retrieval_enabled: false,
    shared_cache_enabled: false,
  });
  assert.equal(requests[0].options.headers.Authorization, 'Bearer browser-token');
  assert.equal(sandbox.SELECTOR_API_TOKEN, 'selector-token');
  assert.match(statuses.at(-1), /will not be shown again/);
});

test('file upload enforces v2 bindings and explicit decisions before sending', async () => {
  const app = await read('app.js');
  assert.match(app, /file\.size > 131_072/);
  const requests = [];
  const statuses = [];
  const file = {
    size: JSON.stringify(submission()).length,
    text: async () => JSON.stringify(submission()),
  };
  const sandbox = {
    SELECTOR_API_TOKEN: 'selector-token',
    SELECTOR_ROUND_DESCRIPTOR: {
      round_id: 'weekly-test',
      environment: 'preview',
      blind_manifest_sha256: HASH('a'),
      kit: { kit_sha256: HASH('b') },
    },
    $: selector => selector === '#selector-submission-file' ? { files: [file] } : null,
    setProgrammaticVotingStatus: message => statuses.push(message),
    fetch: async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          submission_id: submission().submission_id,
          payload_digest: HASH('d'),
          revision_number: 1,
          round_id: 'weekly-test',
          environment: 'preview',
          blind_manifest_sha256: HASH('a'),
          kit_sha256: HASH('b'),
        }),
      };
    },
    SyntaxError,
  };
  const submitSelectorFile = evaluateDeclaration(app, 'async function submitSelectorFile()', sandbox);
  await submitSelectorFile();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/weekly-selector/submissions');
  assert.equal(requests[0].options.headers.Authorization, 'Bearer selector-token');
  assert.match(statuses.at(-1), /Complete dual-mode submission accepted/);

  file.text = async () => JSON.stringify({ ...submission(), schema_version: 'v1' });
  await submitSelectorFile();
  assert.equal(requests.length, 1);
  assert.match(statuses.at(-1), /foldarium\.selector-submission\/v2/);
});

test('selector leaderboard renders independent Cluster and Exact tracks and escapes identity text', async () => {
  const app = await read('app.js');
  const escapeSelectorText = evaluateDeclaration(app, 'function escapeSelectorText(value)', {});
  const formatSelectorScoreLine = evaluateDeclaration(
    app,
    'function formatSelectorScoreLine(row)',
    { escapeSelectorText, Number },
  );
  const rendered = formatSelectorScoreLine({
    participant_type: 'selector',
    identity: {
      display_name: '<Ada>',
      method_name: 'rules',
      method_version: '2',
      provider: 'example',
      model_name: 'rules',
      model_version: '2',
    },
    clustered: { correct: 3, item_count: 5, accuracy: 60, rank: 2 },
    unclustered: { correct: 4, item_count: 5, accuracy: 80, rank: 1 },
  });
  assert.match(rendered, /<b>&lt;Ada&gt;<\/b>/);
  assert.match(rendered, /Cluster #2 3\/5 \(60%\)/);
  assert.match(rendered, /Exact #1 4\/5 \(80%\)/);
  assert.doesNotMatch(rendered, /<Ada>/);

  const benchmark = formatSelectorScoreLine({
    participant_type: 'post_close_benchmark',
    identity: {
      display_name: 'Claude Opus',
      provider: 'anthropic',
      model_name: 'opus',
      model_version: 'claude-opus-exact',
      benchmark: { requested_effort: 'default' },
    },
    clustered: { correct: 3, item_count: 5, accuracy: 60, rank: 2 },
    unclustered: { correct: 4, item_count: 5, accuracy: 80, rank: 1 },
  });
  assert.match(benchmark, /Post-close benchmark · requested default effort/);
});

test('automated leaderboard remains reveal-gated and escapes retrospective identities', async () => {
  const app = await read('app.js');
  const host = { hidden: true, innerHTML: '', replaceChildren() { this.innerHTML = ''; } };
  const escapeSelectorText = evaluateDeclaration(app, 'function escapeSelectorText(value)', {});
  const sandbox = {
    WEEKLY_ONLY: true,
    WEEKLY_ROUND: { round_id: 'weekly-test', public_status: 'open' },
    WEEKLY_RETROSPECTIVE_SUMMARY: null,
    escapeSelectorText,
    $: selector => selector === '#weekly-selector-leaderboard' ? host : null,
  };
  const render = evaluateDeclaration(app, 'function renderWeeklySelectorLeaderboard()', sandbox);
  render();
  assert.equal(host.hidden, true);

  sandbox.WEEKLY_ROUND.public_status = 'revealed';
  sandbox.WEEKLY_RETROSPECTIVE_SUMMARY = {
    automated_entries: [{
      participant: '<script>',
      correct: 1,
      total: 1,
      accuracy: 100,
    }],
  };
  render();
  assert.equal(host.hidden, false);
  assert.match(host.innerHTML, /Automated methods/);
  assert.match(host.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(host.innerHTML, /<script>/);
});

test('results loading rejects legacy envelopes without disrupting Weekly', async () => {
  const app = await read('app.js');
  const calls = [];
  const sandbox = {
    WEEKLY_ONLY: true,
    WEEKLY_ROUND: { round_id: 'weekly-test', public_status: 'revealed' },
    WEEKLY_SELECTOR_RESULTS: null,
    WEEKLY_SELECTOR_RESULTS_ERROR: '',
    renderWeeklySelectorLeaderboard: () => calls.push('render'),
    fetch: async () => ({ ok: true, json: async () => ({ rows: [] }) }),
    encodeURIComponent,
  };
  const load = evaluateDeclaration(app, 'async function loadWeeklySelectorResults()', sandbox);
  await load();
  assert.equal(sandbox.WEEKLY_SELECTOR_RESULTS, null);
  assert.match(sandbox.WEEKLY_SELECTOR_RESULTS_ERROR, /invalid/);
  assert.deepEqual(calls, ['render']);
});

test('programmatic additions do not replace the manual Weekly session and vote flow', async () => {
  const [app, html] = await Promise.all([read('app.js'), read('index.html')]);
  assert.match(app, /startNamedSession\(\{/);
  assert.match(app, /submitWeeklyVoteAttempt\(/);
  assert.match(app, /async function finalizeWeeklyVote\(\{ postReveal = false \} = \{\}\)/);
  assert.match(html, /id="participant-name"/);
  assert.match(html, /id="choices"/);
  assert.match(html, /id="lock"/);
  assert.doesNotMatch(app, /selector-(?:provider|model)[\s\S]{0,200}#lock/);
});
