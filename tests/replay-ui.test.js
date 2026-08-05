import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createReplayController } from '../replay.js';

function abortablePlayback(signals) {
  return (_plugin, _trace, { signal }) => new Promise((resolve, reject) => {
    signals.push({ signal, resolve });
    signal.addEventListener('abort', () => {
      reject(new DOMException('Viewer replay aborted', 'AbortError'));
    }, { once: true });
  });
}

test('starting another answer aborts the current playback before clearing the viewer', async () => {
  const events = [];
  const signals = [];
  const plugin = {
    clear: async () => { events.push(`clear:${signals.length}`); },
  };
  const controller = createReplayController({
    plugin,
    playTrace: abortablePlayback(signals),
  });

  const first = controller.play({ answer: 1 });
  await new Promise(resolve => setImmediate(resolve));
  const second = controller.play({ answer: 2 });
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(signals[0].signal.aborted, true);
  assert.deepEqual(events, ['clear:0', 'clear:1']);
  assert.equal(signals.length, 2);

  const stopped = controller.stop();
  await stopped;
  await Promise.all([first, second]);
  assert.equal(signals[1].signal.aborted, true);
});

test('replay page keeps credentials ephemeral and renders server data without innerHTML', async () => {
  const [html, script] = await Promise.all([
    readFile(new URL('../replay.html', import.meta.url), 'utf8'),
    readFile(new URL('../replay.js', import.meta.url), 'utf8'),
  ]);

  for (const id of ['replay-password', 'connect', 'sessions', 'answers', 'play', 'stop', 'status', 'viewer']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /molstar@4\.6\.0\/build\/viewer\/molstar\.css/);
  assert.match(html, /molstar@4\.6\.0\/build\/viewer\/molstar\.js/);
  assert.match(html, /<script type="module" src="replay\.js"><\/script>/);

  assert.match(script, /let replayPassword = '';/);
  assert.match(script, /\.textContent =/);
  assert.doesNotMatch(script, /innerHTML|localStorage|sessionStorage|document\.cookie/);
  assert.match(script, /action: 'sessions'/);
  assert.match(script, /action: 'answers'/);
});
