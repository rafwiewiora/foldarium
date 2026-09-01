import test from 'node:test';
import assert from 'node:assert/strict';
import {
  connectionAllowsPrefetch,
  createStructurePrefetcher,
  initialQuestionAssetPaths,
} from '../structure-prefetch.js';

test('selects only the next question initial one-at-a-time assets', () => {
  const item = {
    protein_file: 'protein.pdb',
    pocket_file: 'pocket.pdb',
    choices: [
      {
        pose_file: 'pose-1.pdb',
        afprotein_file: 'folded-1.pdb',
        afpocket_file: 'folded-pocket-1.pdb',
      },
      { pose_file: 'pose-2.pdb', afprotein_file: 'folded-2.pdb' },
    ],
  };
  assert.deepEqual(initialQuestionAssetPaths(item), [
    'protein.pdb',
    'pocket.pdb',
    'pose-1.pdb',
    'folded-1.pdb',
    'folded-pocket-1.pdb',
  ]);
  assert.deepEqual(initialQuestionAssetPaths(item, item.choices[1]), [
    'protein.pdb',
    'pocket.pdb',
    'pose-2.pdb',
    'folded-2.pdb',
  ]);
});

test('deduplicates URLs and does not fetch completed assets twice', async () => {
  const calls = [];
  const prefetcher = createStructurePrefetcher({
    fetchImpl: async url => {
      calls.push(url);
      return { ok: true, arrayBuffer: async () => new TextEncoder().encode(url).buffer };
    },
  });

  await prefetcher.prefetch(['protein.pdb', 'protein.pdb', 'pose.pdb']);
  await prefetcher.prefetch(['protein.pdb', 'pose.pdb']);

  assert.deepEqual(calls.sort(), ['pose.pdb', 'protein.pdb']);
  assert.equal(prefetcher.text('protein.pdb'), 'protein.pdb');
  assert.equal(prefetcher.text('missing.pdb'), null);
});

test('bounds concurrent structure requests', async () => {
  let active = 0;
  let maximumActive = 0;
  const releases = [];
  const prefetcher = createStructurePrefetcher({
    concurrency: 2,
    fetchImpl: async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise(resolve => releases.push(resolve));
      active -= 1;
      return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) };
    },
  });

  const pending = prefetcher.prefetch(['a', 'b', 'c']);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(maximumActive, 2);
  releases.splice(0).forEach(resolve => resolve());
  await new Promise(resolve => setImmediate(resolve));
  releases.splice(0).forEach(resolve => resolve());
  await pending;
});

test('cancels stale work and allows a later navigation to retry', async () => {
  const calls = [];
  const prefetcher = createStructurePrefetcher({
    concurrency: 1,
    fetchImpl: (url, { signal }) => new Promise((resolve, reject) => {
      calls.push(url);
      signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      }, { once: true });
      if (url === 'new.pdb') {
        resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) });
      }
    }),
  });

  const stale = prefetcher.prefetch(['old.pdb']);
  await new Promise(resolve => setImmediate(resolve));
  const current = prefetcher.prefetch(['new.pdb']);

  assert.equal((await stale).skipped, 'stale');
  assert.deepEqual((await current).fetched, ['new.pdb']);
  assert.deepEqual(calls, ['old.pdb', 'new.pdb']);
});

test('opts out for Save-Data and slow connections', async () => {
  assert.equal(connectionAllowsPrefetch({ saveData: true, effectiveType: '4g' }), false);
  assert.equal(connectionAllowsPrefetch({ saveData: false, effectiveType: '2g' }), false);
  assert.equal(connectionAllowsPrefetch({ saveData: false, effectiveType: '3g' }), true);

  let fetched = false;
  const prefetcher = createStructurePrefetcher({
    connection: { saveData: true, effectiveType: '4g' },
    fetchImpl: async () => {
      fetched = true;
      return { ok: true, arrayBuffer: async () => new ArrayBuffer(0) };
    },
  });
  assert.equal((await prefetcher.prefetch(['pose.pdb'])).skipped, 'connection');
  assert.equal(fetched, false);
});
