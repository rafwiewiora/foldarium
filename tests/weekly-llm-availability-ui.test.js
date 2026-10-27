import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

function declaration(source, signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const open = source.indexOf('{', start + signature.length - 1);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    else if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unbalanced ${signature}`);
}
function node() {
  return { children: [], textContent: '', append(...children) { this.children.push(...children); } };
}
function text(node) {
  return node.textContent + (node.children || []).map(text).join(' ');
}
async function renderContext() {
  const source = await readFile(new URL('../weekly-retrospectives.js', import.meta.url), 'utf8');
  const context = vm.createContext({
    document: { createElement: node, createTextNode: value => ({ textContent: value }) },
    state: { detailScope: 'full' },
    formatDate: () => 'Fixture week', buildOutcomeSummary: node,
    featuredResultText: row => row.participant,
  });
  for (const signature of [
    'function element(', 'function roundHref(', 'function appendLlmBenchmarkAvailability(',
    'function makeRoundRow(', 'function activeFeaturedCohort(',
    'function answerLine(', 'export function formatAccuracy(', 'function renderAutomatedLeaderboard(',
  ]) vm.runInContext(declaration(source, signature).replace(/^export /, ''), context);
  return context;
}
const missing = 'No LLM benchmark is available for this round.';
const baseline = { participant: 'Smina', participant_kind: 'baseline', correct: 1, total: 2, accuracy: 50 };
const llm = { participant: 'A model', participant_kind: 'llm', correct: 0, total: 2, accuracy: 0 };

test('archive rows explicitly identify baseline-only rounds, including the compact chooser', async () => {
  const context = await renderContext();
  for (const compact of [false, true]) {
    for (const entries of [[], [baseline], [baseline, { ...baseline, participant: 'Ligand pLDDT' }]]) {
      const row = context.makeRoundRow({ round_id: 'fixture', summary: { automated_entries: entries } }, compact);
      assert.ok(text(row).includes(missing));
    }
    const row = context.makeRoundRow({ round_id: 'fixture', summary: { automated_entries: [baseline, llm], automated_winner: baseline } }, compact);
    assert.equal(text(row).includes(missing), false, 'a baseline winner does not imply the LLM benchmark is absent');
    const unknown = context.makeRoundRow({ round_id: 'fixture', summary: {} }, compact);
    assert.equal(text(unknown).includes(missing), false, 'missing metadata is not proof of an absent benchmark');
  }
});

test('detail absence note uses the full round even when featured results omit an available LLM', async () => {
  const context = await renderContext();
  for (const scope of ['full', 'featured']) {
    context.state.detailScope = scope;
    const detail = { retrospective: { automated_entries: [baseline] }, featured_cohort: { participants: [baseline] } };
    const host = node();
    context.renderAutomatedLeaderboard(host, detail);
    assert.ok(text(host).includes(missing));
    detail.retrospective.automated_entries.push(llm);
    const available = node();
    context.renderAutomatedLeaderboard(available, detail);
    assert.equal(text(available).includes(missing), false);
  }
});
