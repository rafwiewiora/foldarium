import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as replayModule from '../replay.js';
import {
  createLatestRequestGuard,
  createReplayController,
  createSessionListLoader,
  formatAnswerLabel,
  formatSessionLabel,
  replaceSelectOptions,
} from '../replay.js';

function validTrace(label) {
  return {
    version: 1,
    molstar_version: '4.6.0',
    snapshots: [{ t_ms: 0, kind: 'state', snapshot: { label } }],
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

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

  const first = controller.play(validTrace('answer-1'));
  await new Promise(resolve => setImmediate(resolve));
  const second = controller.play(validTrace('answer-2'));
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(signals[0].signal.aborted, true);
  assert.deepEqual(events, ['clear:0', 'clear:1']);
  assert.equal(signals.length, 2);

  const stopped = controller.stop();
  await stopped;
  await Promise.all([first, second]);
  assert.equal(signals[1].signal.aborted, true);
});

test('rejects an invalid trace before clearing or starting viewer playback', async () => {
  let clears = 0;
  let plays = 0;
  const controller = createReplayController({
    plugin: { clear: async () => { clears += 1; } },
    playTrace: async () => { plays += 1; },
  });

  await assert.rejects(
    controller.play({ version: 2, molstar_version: '4.6.0', snapshots: [] }),
    /Unsupported viewer trace/,
  );
  assert.equal(clears, 0);
  assert.equal(plays, 0);
});

function replayUiHarness(replayController) {
  assert.equal(
    typeof replayModule.createReplayPlaybackUi,
    'function',
    'expected replay UI playback coordinator export',
  );
  let controls = {};
  const statuses = [];
  const ui = replayModule.createReplayPlaybackUi({
    replayController,
    hasSelectedAnswer: () => true,
    setControls(next) {
      controls = next;
    },
    setStatus(message, isError = false) {
      statuses.push({ message, isError });
    },
  });
  return {
    ui,
    statuses,
    controls: () => controls,
  };
}

test('an older play completion cannot disable Stop or replace status for newer playback', async () => {
  const plays = [];
  const harness = replayUiHarness({
    play(trace) {
      const completion = deferred();
      plays.push({ trace, completion });
      return completion.promise;
    },
    stop: async () => {},
  });

  const first = harness.ui.play(validTrace('answer-1'));
  const second = harness.ui.play(validTrace('answer-2'));
  plays[0].completion.resolve(true);
  await first;

  assert.deepEqual(harness.controls(), { playDisabled: true, stopDisabled: false });
  assert.deepEqual(harness.statuses.at(-1), {
    message: 'Playing answer trace…',
    isError: false,
  });

  plays[1].completion.resolve(true);
  await second;
  assert.deepEqual(harness.controls(), { playDisabled: false, stopDisabled: true });
  assert.deepEqual(harness.statuses.at(-1), {
    message: 'Playback complete.',
    isError: false,
  });
});

test('changing answers stops playback immediately and ignores its stale completion', async () => {
  const playback = deferred();
  const stopping = deferred();
  let stopCalls = 0;
  const harness = replayUiHarness({
    play: () => playback.promise,
    stop() {
      stopCalls += 1;
      return stopping.promise;
    },
  });

  const playing = harness.ui.play(validTrace('answer-1'));
  const changing = harness.ui.selectionChanged(true);
  assert.equal(stopCalls, 1);
  assert.deepEqual(harness.controls(), { playDisabled: true, stopDisabled: true });

  playback.resolve(true);
  await playing;
  assert.deepEqual(harness.controls(), { playDisabled: true, stopDisabled: true });
  assert.deepEqual(harness.statuses.at(-1), {
    message: 'Playing answer trace…',
    isError: false,
  });

  stopping.resolve();
  await changing;
  assert.deepEqual(harness.controls(), { playDisabled: false, stopDisabled: true });
});

test('an invalid replacement trace leaves controls attached to active playback', async () => {
  const playback = deferred();
  let playCalls = 0;
  const harness = replayUiHarness({
    play() {
      playCalls += 1;
      return playback.promise;
    },
    stop: async () => {},
  });

  const playing = harness.ui.play(validTrace('answer-1'));
  const accepted = await harness.ui.play({
    version: 2,
    molstar_version: '4.6.0',
    snapshots: [],
  });

  assert.equal(accepted, false);
  assert.equal(playCalls, 1);
  assert.deepEqual(harness.controls(), { playDisabled: true, stopDisabled: false });
  assert.deepEqual(harness.statuses.at(-1), {
    message: 'Unsupported viewer trace',
    isError: true,
  });

  playback.resolve(true);
  await playing;
  assert.deepEqual(harness.controls(), { playDisabled: false, stopDisabled: true });
});

test('ignores an out-of-order session-answer response and aborts its request', async () => {
  const guard = createLatestRequestGuard();
  const oldResponse = deferred();
  const newResponse = deferred();
  let oldSignal;

  const oldRequest = guard.run(signal => {
    oldSignal = signal;
    return oldResponse.promise;
  });
  const newRequest = guard.run(() => newResponse.promise);

  assert.equal(oldSignal.aborted, true);
  newResponse.resolve(['new-session-answer']);
  assert.deepEqual(await newRequest, {
    accepted: true,
    value: ['new-session-answer'],
  });

  oldResponse.resolve(['stale-session-answer']);
  assert.deepEqual(await oldRequest, { accepted: false });
});

test('concurrent Connect submissions apply only the latest session list', async () => {
  const oldResponse = deferred();
  const newResponse = deferred();
  const applied = [];
  let oldSignal;
  let requestIndex = 0;
  const loader = createSessionListLoader({
    requestSessions(signal) {
      requestIndex += 1;
      if (requestIndex === 1) {
        oldSignal = signal;
        return oldResponse.promise;
      }
      return newResponse.promise;
    },
    applySessions(sessions) {
      applied.push(sessions);
    },
  });

  const oldConnect = loader.load();
  const newConnect = loader.load();
  assert.equal(oldSignal.aborted, true);

  newResponse.resolve(['new-session']);
  assert.equal(await newConnect, true);
  oldResponse.resolve(['stale-session']);
  assert.equal(await oldConnect, false);
  assert.deepEqual(applied, [['new-session']]);
});

test('reconnecting during playback prevents the old operation from changing new session UI', async () => {
  assert.equal(
    typeof replayModule.createReplayConnectHandler,
    'function',
    'expected replay Connect coordinator export',
  );
  const playback = deferred();
  const sessions = deferred();
  const events = [];
  const answers = new Map([['old-answer', {}]]);
  let rememberedPassword = '';
  let controls = {};
  let connectDisabled = false;
  let status = { message: '', isError: false };
  let sessionRows = [];
  const setStatus = (message, isError = false) => {
    status = { message, isError };
  };
  const playbackUi = replayModule.createReplayPlaybackUi({
    replayController: {
      play: () => playback.promise,
      stop() {
        events.push('stop-playback');
      },
    },
    hasSelectedAnswer: () => answers.has('old-answer'),
    setControls(next) {
      controls = next;
    },
    setStatus,
  });
  const sessionLoader = createSessionListLoader({
    requestSessions() {
      events.push(`load-sessions-with-${answers.size}-answers`);
      return sessions.promise;
    },
    applySessions(rows) {
      sessionRows = rows;
      setStatus('Select a session.');
    },
  });
  const connect = replayModule.createReplayConnectHandler({
    sessionLoader,
    answerRequests: { cancel: () => events.push('cancel-answer-request') },
    playbackUi,
    readPassword: () => 'replacement-password',
    rememberPassword(value) {
      rememberedPassword = value;
    },
    clearPasswordInput: () => events.push('clear-password-input'),
    clearAnswerState() {
      answers.clear();
      events.push('clear-answers');
    },
    clearSessionState: () => {},
    setConnectDisabled(value) {
      connectDisabled = value;
    },
    setStatus,
  });

  const oldPlayback = playbackUi.play(validTrace('old-answer'));
  const reconnecting = connect();
  await new Promise(resolve => setImmediate(resolve));

  assert.deepEqual(events, [
    'cancel-answer-request',
    'clear-password-input',
    'stop-playback',
    'clear-answers',
    'load-sessions-with-0-answers',
  ]);
  assert.equal(rememberedPassword, 'replacement-password');

  sessions.resolve(['new-session']);
  assert.equal(await reconnecting, true);
  const newSessionUi = {
    connectDisabled,
    controls,
    sessionRows,
    status,
  };
  assert.deepEqual(newSessionUi, {
    connectDisabled: false,
    controls: { playDisabled: true, stopDisabled: true },
    sessionRows: ['new-session'],
    status: { message: 'Select a session.', isError: false },
  });

  playback.resolve(true);
  await oldPlayback;
  assert.deepEqual({
    connectDisabled,
    controls,
    sessionRows,
    status,
  }, newSessionUi);
});

test('displays replay identifiers and answer time as option text', () => {
  const session = {
    id: '00000000-0000-4000-8000-000000000001',
    user_id: '<img src=x onerror=alert(1)>',
    source: 'cameo',
    difficulty: 'easy',
    started_at: '2026-08-05T12:00:00.000Z',
  };
  const answer = {
    id: 'answer-1',
    question_index: 0,
    item_id: '<script>alert(1)</script>',
    picked_none: false,
    picked_sample: 2,
    picked_correct: true,
    answered_at: '2026-08-05T12:05:00.000Z',
  };
  const select = {
    children: [],
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); },
  };
  const documentImpl = {
    createElement() {
      return {
        value: '',
        textContent: '',
        set innerHTML(_value) {
          assert.fail('option rendering must not use innerHTML');
        },
      };
    },
  };

  replaceSelectOptions(select, [session], formatSessionLabel, documentImpl);
  assert.match(select.children[1].textContent, new RegExp(session.id));
  assert.match(select.children[1].textContent, /<img src=x onerror=alert\(1\)>/);

  replaceSelectOptions(select, [answer], formatAnswerLabel, documentImpl);
  assert.match(select.children[1].textContent, /<script>alert\(1\)<\/script>/);
  assert.match(select.children[1].textContent, /Aug|2026|12:05/);
});

test('replay page exposes the required controls and pinned Molstar version', async () => {
  const html = await readFile(new URL('../replay.html', import.meta.url), 'utf8');

  for (const id of ['replay-password', 'connect', 'sessions', 'answers', 'play', 'stop', 'status', 'viewer']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /molstar@4\.6\.0\/build\/viewer\/molstar\.css/);
  assert.match(html, /molstar@4\.6\.0\/build\/viewer\/molstar\.js/);
  assert.match(html, /<script type="module" src="replay\.js"><\/script>/);
});
