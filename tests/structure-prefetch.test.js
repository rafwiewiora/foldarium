import test from 'node:test';
import assert from 'node:assert/strict';
import {
  connectionAllowsPrefetch,
  createStructurePrefetcher,
  gridQuestionAssetPaths,
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

test('selects every structure shown on the next clustered Grid page', () => {
  const item = { protein_file: 'shared-protein.pdb', pocket_file: 'shared-pocket.pdb' };
  const clusters = [
    {
      rep: {
        pose_file: 'pose-1.pdb',
        afprotein_file: 'protein-1.pdb',
        afpocket_file: 'pocket-1.pdb',
      },
      members: [
        {
          pose_file: 'pose-1.pdb',
          afprotein_file: 'protein-1.pdb',
          afpocket_file: 'pocket-1.pdb',
        },
        { pose_file: 'pose-2.pdb', afprotein_file: 'protein-2.pdb' },
      ],
    },
    {
      rep: { pose_file: 'pose-3.pdb' },
      members: [{ pose_file: 'pose-3.pdb' }],
    },
  ];

  assert.deepEqual(gridQuestionAssetPaths(item, clusters), [
    'protein-1.pdb',
    'pocket-1.pdb',
    'pose-1.pdb',
    'pose-2.pdb',
    'shared-protein.pdb',
    'shared-pocket.pdb',
    'pose-3.pdb',
  ]);
  assert.deepEqual(gridQuestionAssetPaths(item, clusters, {
    showProteinEnsemble: true,
  }), [
    'protein-1.pdb',
    'pocket-1.pdb',
    'pose-1.pdb',
    'pose-2.pdb',
    'protein-2.pdb',
    'shared-protein.pdb',
    'shared-pocket.pdb',
    'pose-3.pdb',
  ]);
});

test('selects only raw poses shown on the requested unclustered Grid page', () => {
  const item = { protein_file: 'shared-protein.pdb', pocket_file: 'shared-pocket.pdb' };
  const clusters = [{
    rep: null,
    members: [
      {
        pose_file: 'pose-1.pdb',
        afprotein_file: 'protein-1.pdb',
        afpocket_file: 'pocket-1.pdb',
      },
      {
        pose_file: 'pose-2.pdb',
        afprotein_file: 'protein-2.pdb',
        afpocket_file: 'pocket-2.pdb',
      },
    ],
  }];

  assert.deepEqual(gridQuestionAssetPaths(item, clusters, {
    page: 1,
    pageSize: 1,
    clustered: false,
  }), [
    'protein-2.pdb',
    'pocket-2.pdb',
    'pose-2.pdb',
    'shared-protein.pdb',
    'shared-pocket.pdb',
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

test('bounds retained prefetched bytes and evicts the oldest asset', async () => {
  const calls = [];
  const bodies = {
    first: new Uint8Array([1, 2, 3, 4]),
    second: new Uint8Array([5, 6, 7, 8]),
  };
  const prefetcher = createStructurePrefetcher({
    maxBytes: 6,
    fetchImpl: async url => {
      calls.push(url);
      return { ok: true, arrayBuffer: async () => bodies[url].buffer };
    },
  });

  await prefetcher.prefetch(['first', 'second']);
  assert.equal(prefetcher.text('first'), null);
  assert.notEqual(prefetcher.text('second'), null);
  assert.equal(prefetcher.cachedBytes, 4);

  await prefetcher.prefetch(['first']);
  assert.deepEqual(calls, ['first', 'second', 'first']);
  assert.notEqual(prefetcher.text('first'), null);
  assert.equal(prefetcher.text('second'), null);
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

test('preserves active work and prioritizes nearer questions added during navigation', async () => {
  const calls = [];
  const releases = new Map();
  const prefetcher = createStructurePrefetcher({
    concurrency: 1,
    fetchImpl: (url, { signal }) => new Promise((resolve, reject) => {
      calls.push(url);
      signal.addEventListener('abort', () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      }, { once: true });
      releases.set(url, () => resolve({
        ok: true,
        arrayBuffer: async () => new TextEncoder().encode(url).buffer,
      }));
    }),
  });

  const earlier = prefetcher.prefetch(['old-1.pdb', 'old-2.pdb'], { priority: 1 });
  await new Promise(resolve => setImmediate(resolve));
  const nearer = prefetcher.prefetch(['nearer.pdb'], { priority: 10 });
  releases.get('old-1.pdb')();
  await new Promise(resolve => setImmediate(resolve));
  releases.get('nearer.pdb')();
  await new Promise(resolve => setImmediate(resolve));
  releases.get('old-2.pdb')();

  assert.equal((await earlier).skipped, null);
  assert.deepEqual((await nearer).fetched, ['nearer.pdb']);
  assert.deepEqual(calls, ['old-1.pdb', 'nearer.pdb', 'old-2.pdb']);
});

test('foreground consumers reuse an in-flight prefetch without a duplicate request', async () => {
  let release;
  const calls = [];
  const prefetcher = createStructurePrefetcher({
    fetchImpl: url => new Promise(resolve => {
      calls.push(url);
      release = () => resolve({
        ok: true,
        arrayBuffer: async () => new TextEncoder().encode('shared').buffer,
      });
    }),
  });

  const pending = prefetcher.prefetch(['shared.pdb']);
  await new Promise(resolve => setImmediate(resolve));
  const foreground = prefetcher.textWhenReady('shared.pdb');
  release();

  await pending;
  assert.equal(await foreground, 'shared');
  assert.deepEqual(calls, ['shared.pdb']);
});

test('explicit cancellation aborts queued work but later requests can retry', async () => {
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

  const cancelled = prefetcher.prefetch(['old.pdb']);
  await new Promise(resolve => setImmediate(resolve));
  prefetcher.cancel();
  const current = prefetcher.prefetch(['new.pdb']);

  assert.equal((await cancelled).skipped, 'cancelled');
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
