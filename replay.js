import { playViewerTrace, validateViewerTrace } from './replay-player.js';

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

export function createLatestRequestGuard() {
  let generation = 0;
  let activeController = null;

  return {
    async run(request) {
      const requestGeneration = ++generation;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      try {
        const value = await request(controller.signal);
        if (requestGeneration !== generation) return { accepted: false };
        return { accepted: true, value };
      } catch (error) {
        if (requestGeneration !== generation || isAbortError(error)) return { accepted: false };
        throw error;
      } finally {
        if (activeController === controller) activeController = null;
      }
    },
    cancel() {
      generation += 1;
      activeController?.abort();
      activeController = null;
    },
  };
}

export function createSessionListLoader({ requestSessions, applySessions }) {
  const requests = createLatestRequestGuard();
  return {
    async load() {
      const result = await requests.run(requestSessions);
      if (!result.accepted) return false;
      applySessions(result.value);
      return true;
    },
    cancel() {
      requests.cancel();
    },
  };
}

function formatDate(value, fallback) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
}

export function formatSessionLabel(session) {
  const source = String(session.source || 'unknown');
  const difficulty = String(session.difficulty || 'unknown');
  const id = String(session.id || 'unknown session');
  const userId = String(session.user_id || 'unknown user');
  return `${formatDate(session.started_at, 'unknown date')} · ${source} · ${difficulty}`
    + ` · session ${id} · user ${userId}`;
}

export function formatAnswerLabel(answer) {
  const question = Number(answer.question_index) + 1;
  const item = String(answer.item_id || 'unknown item');
  const pick = answer.picked_none ? 'none' : `sample ${answer.picked_sample ?? 'unknown'}`;
  const result = answer.picked_correct ? 'correct' : 'wrong';
  return `Question ${question} · ${item} · ${pick} · ${result}`
    + ` · answered ${formatDate(answer.answered_at, 'unknown time')}`;
}

export function replaceSelectOptions(select, rows, labelFor, documentImpl = document) {
  select.replaceChildren();
  const placeholder = documentImpl.createElement('option');
  placeholder.value = '';
  placeholder.textContent = rows.length ? 'Select…' : 'None available';
  select.appendChild(placeholder);
  for (const row of rows) {
    const option = documentImpl.createElement('option');
    option.value = String(row.id);
    option.textContent = labelFor(row);
    select.appendChild(option);
  }
  select.disabled = rows.length === 0;
}

export function createReplayController({
  plugin,
  playTrace = playViewerTrace,
  validateTrace = validateViewerTrace,
}) {
  let generation = 0;
  let active = null;

  return {
    async play(trace) {
      validateTrace(trace);
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

export function createReplayPlaybackUi({
  replayController,
  setControls,
  setStatus,
  hasSelectedAnswer,
  validateTrace = validateViewerTrace,
}) {
  let generation = 0;

  return {
    async play(trace) {
      try {
        validateTrace(trace);
      } catch (error) {
        setStatus(error.message, true);
        return false;
      }

      const playGeneration = ++generation;
      setControls({ playDisabled: true, stopDisabled: false });
      setStatus('Playing answer trace…');
      try {
        const completed = await replayController.play(trace);
        if (playGeneration !== generation) return false;
        if (completed) setStatus('Playback complete.');
        return completed;
      } catch (error) {
        if (playGeneration === generation) setStatus(error.message, true);
        return false;
      } finally {
        if (playGeneration === generation) {
          setControls({ playDisabled: false, stopDisabled: true });
        }
      }
    },

    async selectionChanged(hasAnswer) {
      const selectionGeneration = ++generation;
      const stopping = replayController.stop();
      setControls({ playDisabled: true, stopDisabled: true });
      try {
        await stopping;
      } catch (error) {
        if (selectionGeneration === generation) setStatus(error.message, true);
      } finally {
        if (selectionGeneration === generation) {
          setControls({ playDisabled: !hasAnswer, stopDisabled: true });
        }
      }
    },

    async stop({ announce = true } = {}) {
      const stopGeneration = ++generation;
      const stopping = replayController.stop();
      setControls({ playDisabled: true, stopDisabled: true });
      try {
        await stopping;
        if (stopGeneration === generation && announce) setStatus('Playback stopped.');
      } catch (error) {
        if (stopGeneration === generation) setStatus(error.message, true);
      } finally {
        if (stopGeneration === generation) {
          setControls({
            playDisabled: !hasSelectedAnswer(),
            stopDisabled: true,
          });
        }
      }
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
  const answerRequests = createLatestRequestGuard();

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.dataset.error = isError ? 'true' : 'false';
  }

  async function requestReplay(payload, signal) {
    const response = await fetch('/api/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: replayPassword, ...payload }),
      signal,
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

  let viewer;
  try {
    viewer = await molstar.Viewer.create('viewer', VIEWER_OPTIONS);
  } catch {
    setStatus('Could not initialize the molecular viewer.', true);
    return;
  }
  const replayController = createReplayController({ plugin: viewer.plugin });
  const playbackUi = createReplayPlaybackUi({
    replayController,
    setStatus,
    hasSelectedAnswer: () => answersById.has(answerSelect.value),
    setControls({ playDisabled, stopDisabled }) {
      playButton.disabled = playDisabled;
      stopButton.disabled = stopDisabled;
    },
  });
  const sessionLoader = createSessionListLoader({
    requestSessions: signal => requestReplay({ action: 'sessions' }, signal),
    applySessions(sessions) {
      replaceSelectOptions(sessionSelect, sessions, formatSessionLabel);
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
      setStatus(sessions.length ? 'Select a session.' : 'No replay sessions are available.');
    },
  });

  async function loadAnswers() {
    const sessionId = sessionSelect.value;
    answersById.clear();
    playButton.disabled = true;
    stopButton.disabled = true;
    replaceSelectOptions(answerSelect, [], formatAnswerLabel);
    setStatus(sessionId ? 'Loading traced answers…' : 'Select a session.');
    try {
      const result = await answerRequests.run(async signal => {
        await playbackUi.stop({ announce: false });
        if (!sessionId) return [];
        return requestReplay({
          action: 'answers',
          session_id: sessionId,
        }, signal);
      });
      if (!result.accepted) return;
      const answers = result.value;
      for (const answer of answers) answersById.set(String(answer.id), answer);
      replaceSelectOptions(answerSelect, answers, formatAnswerLabel);
      setStatus(sessionId
        ? (answers.length ? 'Select an answer to replay.' : 'This session has no traced answers.')
        : 'Select a session.');
    } catch (error) {
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
      setStatus(error.message, true);
    }
  }

  async function connect() {
    sessionLoader.cancel();
    answerRequests.cancel();
    replayPassword = passwordInput.value;
    passwordInput.value = '';
    if (!replayPassword) {
      setStatus('Enter the replay password.', true);
      connectButton.disabled = false;
      return;
    }

    connectButton.disabled = true;
    setStatus('Loading recent sessions…');
    let completedCurrentRequest = false;
    try {
      const applied = await sessionLoader.load();
      if (!applied) return;
      completedCurrentRequest = true;
    } catch (error) {
      completedCurrentRequest = true;
      replaceSelectOptions(sessionSelect, [], formatSessionLabel);
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
      setStatus(error.message, true);
    } finally {
      if (completedCurrentRequest) connectButton.disabled = false;
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
    void playbackUi.selectionChanged(answersById.has(answerSelect.value));
  });
  playButton.addEventListener('click', () => {
    const answer = answersById.get(answerSelect.value);
    if (!answer) return;
    void playbackUi.play(answer.viewer_trace);
  });
  stopButton.addEventListener('click', () => {
    void playbackUi.stop();
  });

  setStatus('Enter the replay password to connect.');
}

if (typeof document !== 'undefined') {
  void initReplayPage();
}
