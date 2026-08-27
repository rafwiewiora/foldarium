import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(root, '..', 'fixtures', 'parity.json'), 'utf8'));

function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value && typeof value === 'object') {
    return Object.keys(value).sort().reduce((accumulator, key) => {
      accumulator[key] = sortDeep(value[key]);
      return accumulator;
    }, {});
  }
  return value;
}

function stableJson(value) {
  return JSON.stringify(sortDeep(value));
}

test('parity fixture matches canonical digest expectations', () => {
  const normalized = sortDeep(fixture.submission);
  const digest = createHash('sha256').update(stableJson(normalized), 'utf8').digest('hex');
  assert.equal(digest, fixture.expected_digest);
  assert.equal(normalized.items.length, 2);
});

test('wasm mapper validates fixture when pkg is built', async (t) => {
  let wasm;
  try {
    wasm = await import('../pkg/weekly_selector_wasm.js');
  } catch (error) {
    t.skip(`wasm package not built: ${error.message}`);
    return;
  }

  const validated = JSON.parse(
    wasm.validateSubmission(
      stableJson(fixture.manifest),
      stableJson(fixture.submission),
    ),
  );
  assert.deepEqual(validated.items, fixture.submission.items);
  const digest = wasm.digestSubmission(
    stableJson(fixture.manifest),
    stableJson(fixture.submission),
  );
  assert.equal(digest, fixture.expected_digest);

  const template = JSON.parse(wasm.buildTemplate(stableJson(fixture.manifest)));
  assert.deepEqual(template, [
    {
      item_id: 'item-a',
      clustered: { selection_kind: 'none' },
      unclustered: { selection_kind: 'none' },
    },
    {
      item_id: 'item-b',
      clustered: { selection_kind: 'none' },
      unclustered: { selection_kind: 'none' },
    },
  ]);

  const malformedNone = structuredClone(fixture.submission);
  malformedNone.items[0].clustered = {
    selection_kind: 'none',
    cluster_id: 'cluster-x',
  };
  assert.throws(
    () => wasm.validateSubmission(
      stableJson(fixture.manifest),
      stableJson(malformedNone),
    ),
    /unknown key|cluster\/none decision/,
  );

  const crossItem = structuredClone(fixture.submission);
  crossItem.items[0].unclustered.choice_id = 'choice-4';
  assert.throws(
    () => wasm.validateSubmission(
      stableJson(fixture.manifest),
      stableJson(crossItem),
    ),
    /choice_id is not valid for item item-a/,
  );

  const noncanonicalOrder = structuredClone(fixture.submission);
  noncanonicalOrder.items.reverse();
  assert.throws(
    () => wasm.validateSubmission(
      stableJson(fixture.manifest),
      stableJson(noncanonicalOrder),
    ),
    /canonical item order/,
  );

  const built = JSON.parse(wasm.buildSubmission(
    stableJson(fixture.manifest),
    fixture.submission.submission_id,
    stableJson([...fixture.submission.items].reverse()),
  ));
  assert.deepEqual(built, fixture.submission);
});
