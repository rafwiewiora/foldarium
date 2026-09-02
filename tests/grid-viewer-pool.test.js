import test from 'node:test';
import assert from 'node:assert/strict';
import { createGridViewerPool } from '../grid-viewer-pool.js';

function viewerCell(name, { reusable = true } = {}) {
  const calls = [];
  const viewer = { dispose: () => calls.push('dispose') };
  const plugin = { clear: async () => calls.push('clear') };
  return {
    cell: { viewer, plugin, host: { name }, reusable },
    calls,
  };
}

test('reuses a cleared viewer without disposing its WebGL context', async () => {
  const pool = createGridViewerPool({ enabled: true, maxSize: 2 });
  const { cell, calls } = viewerCell('pane-1');

  assert.equal(pool.release(cell), true);
  const acquired = await pool.acquire();

  assert.equal(acquired.viewer, cell.viewer);
  assert.equal(acquired.plugin, cell.plugin);
  assert.equal(acquired.host, cell.host);
  assert.equal(acquired.source, 'recycled');
  assert.deepEqual(calls, ['clear']);
  assert.equal(pool.size(), 0);
});

test('accepts a fresh prewarmed viewer without clearing it', async () => {
  const pool = createGridViewerPool({ enabled: true, maxSize: 1 });
  const { cell, calls } = viewerCell('prewarmed');

  assert.equal(pool.add(cell), true);
  const acquired = await pool.acquire();

  assert.equal(acquired.viewer, cell.viewer);
  assert.equal(acquired.source, 'prewarmed');
  assert.deepEqual(calls, []);
});

test('disposes excess, unfinished, and disabled viewer cells', async () => {
  const pool = createGridViewerPool({ enabled: true, maxSize: 1 });
  const first = viewerCell('first');
  const excess = viewerCell('excess');
  const unfinished = viewerCell('unfinished', { reusable: false });

  assert.equal(pool.release(first.cell), true);
  assert.equal(pool.release(excess.cell), false);
  assert.equal(pool.release(unfinished.cell), false);
  assert.deepEqual(excess.calls, ['dispose']);
  assert.deepEqual(unfinished.calls, ['dispose']);
  pool.drain();
  assert.deepEqual(first.calls, ['clear', 'dispose']);

  const disabled = createGridViewerPool({ enabled: false });
  const fresh = viewerCell('disabled');
  const prewarmed = viewerCell('disabled-prewarm');
  assert.equal(disabled.release(fresh.cell), false);
  assert.equal(disabled.add(prewarmed.cell), false);
  assert.deepEqual(fresh.calls, ['dispose']);
  assert.deepEqual(prewarmed.calls, ['dispose']);
});

test('disposes a viewer whose state cannot be cleared and tries the next slot', async () => {
  const pool = createGridViewerPool({ enabled: true, maxSize: 2 });
  const broken = viewerCell('broken');
  broken.cell.plugin.clear = async () => {
    broken.calls.push('clear');
    throw new Error('state clear failed');
  };
  const healthy = viewerCell('healthy');

  pool.release(broken.cell);
  pool.release(healthy.cell);
  const acquired = await pool.acquire();

  assert.equal(acquired.viewer, healthy.cell.viewer);
  assert.deepEqual(broken.calls, ['clear', 'dispose']);
  assert.deepEqual(healthy.calls, ['clear']);
});
