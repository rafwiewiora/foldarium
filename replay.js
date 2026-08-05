import { playViewerTrace } from './replay-player.js';

const VIEWER_OPTIONS = {
  layoutIsExpanded: false, layoutShowControls: false, layoutShowRemoteState: false,
  layoutShowSequence: false, layoutShowLog: false, layoutShowLeftPanel: false,
  viewportShowExpand: false, viewportShowControls: false, viewportShowSettings: false,
  viewportShowSelectionMode: false, viewportShowAnimation: false, viewportShowTrajectoryControls: false,
};

let replayPassword = '';

function isAbortError(error) {
  return error?.name === 'AbortError';
}

async function settlePlayback(playback) {
  try {
    await playback;
  } catch (error) {
    if (!isAbortError(error)) throw error;
  }
}

export function createReplayController({ plugin, playTrace = playViewerTrace }) {
  let generation = 0;
  let active = null;

  return {
    async play(trace) {
      const requestedGeneration = ++generation;
      const previous = active;
      if (previous) {
        previous.controller.abort();
        await settlePlayback(previous.playback);
      }
      if (requestedGeneration !== generation) return false;

      await plugin.clear();
      if (requestedGeneration !== generation) return false;

      const controller = new AbortController();
      const playback = Promise.resolve().then(() => playTrace(plugin, trace, {
        signal: controller.signal,
      }));
      const current = { controller, playback };
      active = current;

      try {
        await playback;
        return true;
      } catch (error) {
        if (isAbortError(error)) return false;
        throw error;
      } finally {
        if (active === current) active = null;
      }
    },

    async stop() {
      generation += 1;
      const previous = active;
      if (!previous) return;
      previous.controller.abort();
      await settlePlayback(previous.playback);
      if (active === previous) active = null;
    },
  };
}

async function initReplayPage() {
  const passwordInput = document.getElementById('replay-password');
  const connectButton = document.getElementById('connect');
  const sessionSelect = document.getElementById('sessions');
  const answerSelect = document.getElementById('answers');
  const playButton = document.getElementById('play');
  const stopButton = document.getElementById('stop');
  const status = document.getElementById('status');
  const answersById = new Map();

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.dataset.error = isError ? 'true' : 'false';
  }

  function replaceOptions(select, rows, labelFor) {
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = rows.length ? 'Select…' : 'None available';
    select.appendChild(placeholder);
    for (const row of rows) {
      const option = document.createElement('option');
      option.value = String(row.id);
      option.textContent = labelFor(row);
      select.appendChild(option);
    }
    select.disabled = rows.length === 0;
  }

  async function requestReplay(payload) {
    const response = await fetch('/api/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: replayPassword, ...payload }),
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error('Replay request failed');
    }
    if (!response.ok) throw new Error(data?.error || 'Replay request failed');
    if (!Array.isArray(data)) throw new Error('Replay request failed');
    return data;
  }

  function sessionLabel(session) {
    const source = String(session.source || 'unknown');
    const difficulty = String(session.difficulty || 'unknown');
    const date = new Date(session.started_at);
    const started = Number.isNaN(date.getTime()) ? 'unknown date' : date.toLocaleString();
    return `${started} · ${source} · ${difficulty}`;
  }

  function answerLabel(answer) {
    const question = Number(answer.question_index) + 1;
    const item = String(answer.item_id || 'unknown item');
    const pick = answer.picked_none ? 'none' : `sample ${answer.picked_sample ?? 'unknown'}`;
    const result = answer.picked_correct ? 'correct' : 'wrong';
    return `Question ${question} · ${item} · ${pick} · ${result}`;
  }

  let viewer;
  try {
    viewer = await molstar.Viewer.create('viewer', VIEWER_OPTIONS);
  } catch {
    setStatus('Could not initialize the molecular viewer.', true);
    return;
  }
  const replayController = createReplayController({ plugin: viewer.plugin });

  async function loadAnswers() {
    await replayController.stop();
    answersById.clear();
    playButton.disabled = true;
    stopButton.disabled = true;
    if (!sessionSelect.value) {
      replaceOptions(answerSelect, [], answerLabel);
      setStatus('Select a session.');
      return;
    }

    setStatus('Loading traced answers…');
    try {
      const answers = await requestReplay({
        action: 'answers',
        session_id: sessionSelect.value,
      });
      for (const answer of answers) answersById.set(String(answer.id), answer);
      replaceOptions(answerSelect, answers, answerLabel);
      setStatus(answers.length ? 'Select an answer to replay.' : 'This session has no traced answers.');
    } catch (error) {
      replaceOptions(answerSelect, [], answerLabel);
      setStatus(error.message, true);
    }
  }

  async function connect() {
    replayPassword = passwordInput.value;
    passwordInput.value = '';
    if (!replayPassword) {
      setStatus('Enter the replay password.', true);
      return;
    }

    connectButton.disabled = true;
    setStatus('Loading recent sessions…');
    try {
      const sessions = await requestReplay({ action: 'sessions' });
      replaceOptions(sessionSelect, sessions, sessionLabel);
      replaceOptions(answerSelect, [], answerLabel);
      setStatus(sessions.length ? 'Select a session.' : 'No replay sessions are available.');
    } catch (error) {
      replaceOptions(sessionSelect, [], sessionLabel);
      replaceOptions(answerSelect, [], answerLabel);
      setStatus(error.message, true);
    } finally {
      connectButton.disabled = false;
    }
  }

  connectButton.addEventListener('click', () => { void connect(); });
  passwordInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      void connect();
    }
  });
  sessionSelect.addEventListener('change', () => { void loadAnswers(); });
  answerSelect.addEventListener('change', () => {
    playButton.disabled = !answersById.has(answerSelect.value);
  });
  playButton.addEventListener('click', () => {
    const answer = answersById.get(answerSelect.value);
    if (!answer) return;
    playButton.disabled = true;
    stopButton.disabled = false;
    setStatus('Playing answer trace…');
    void replayController.play(answer.viewer_trace).then(completed => {
      if (completed) setStatus('Playback complete.');
    }).catch(error => {
      setStatus(error.message, true);
    }).finally(() => {
      playButton.disabled = false;
      stopButton.disabled = true;
    });
  });
  stopButton.addEventListener('click', () => {
    void replayController.stop().then(() => {
      setStatus('Playback stopped.');
      playButton.disabled = !answersById.has(answerSelect.value);
      stopButton.disabled = true;
    }).catch(error => {
      setStatus(error.message, true);
    });
  });

  setStatus('Enter the replay password to connect.');
}

if (typeof document !== 'undefined') {
  void initReplayPage();
}
