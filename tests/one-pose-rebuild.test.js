import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createExclusiveViewerRebuild } from '../one-pose-rebuild.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('an immediate reveal waits for the one-pose rebuild and final capture', async () => {
  const rebuild = deferred();
  const events = [];
  let lockDisabled = false;
  let captured = false;
  const exclusiveRebuild = createExclusiveViewerRebuild({
    async rebuild() {
      events.push('rebuild');
      await rebuild.promise;
      captured = true;
      events.push('capture');
    },
    setBusy(busy) {
      lockDisabled = busy;
      events.push(busy ? 'lock' : 'unlock');
    },
  });

  const pick = exclusiveRebuild.run();
  assert.equal(exclusiveRebuild.pending, true);
  assert.equal(lockDisabled, true);
  assert.equal(exclusiveRebuild.pending ? null : captured, null);
  assert.equal(await exclusiveRebuild.run(), false);

  rebuild.resolve();
  assert.equal(await pick, true);
  assert.deepEqual(events, ['lock', 'rebuild', 'capture', 'unlock']);
  assert.equal(exclusiveRebuild.pending, false);
  assert.equal(lockDisabled, false);
  assert.equal(exclusiveRebuild.pending ? null : captured, true);
});

test('quiz wires one-pose picks and reveal through the exclusive rebuild', async () => {
  const [app, html] = await Promise.all([
    readFile(new URL('../app.js', import.meta.url), 'utf8'),
    readFile(new URL('../index.html', import.meta.url), 'utf8'),
  ]);

  assert.match(html, /window\.createExclusiveViewerRebuild = rebuildModule\.createExclusiveViewerRebuild;/);
  assert.match(app, /onePoseRebuild = window\.createExclusiveViewerRebuild\(/);
  assert.match(app, /if \(onePoseRebuild\?\.pending\) return;/);
  assert.match(app, /shownOne = k;\s*await onePoseRebuild\.run\(\);/);
});

test('every pre-answer viewer control uses the exclusive rebuild before answer lock', async () => {
  const app = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  const initStart = app.indexOf('async function init()');
  const controls = app.slice(
    app.indexOf("document.querySelectorAll('#mode button').forEach(b => b.onclick", initStart),
    app.indexOf("$('#lock').onclick"),
  );
  const keyboard = app.slice(
    app.indexOf("document.addEventListener('keydown'"),
    app.indexOf('if (!POOLS.cameo.length'),
  );

  assert.equal((controls.match(/if \(locked\(\) \|\| onePoseRebuild\?\.pending\) return;/g) || []).length, 4);
  assert.equal((controls.match(/await onePoseRebuild\.run\(\)/g) || []).length, 4);
  assert.doesNotMatch(controls, /await buildLayer\(\)/);
  assert.match(keyboard, /ArrowRight[\s\S]*?await onePoseRebuild\.run\(\)/);
  assert.match(keyboard, /ArrowLeft[\s\S]*?await onePoseRebuild\.run\(\)/);
  assert.doesNotMatch(keyboard, /await buildLayer\(\)/);
});
