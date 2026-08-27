import { playViewerTrace, validateViewerTrace } from './replay-player.js';
import { reconstructWeeklyAttempts } from './weekly-trace-replay.js';

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

export function createConnectionGeneration() {
  let generation = 0;
  return {
    advance() {
      generation += 1;
      return generation;
    },
    capture() {
      return generation;
    },
    owns(value) {
      return value === generation;
    },
  };
}

export function createSessionListLoader({ requestSessions, applySessions }) {
  const requests = createLatestRequestGuard();
  return {
    async load(isCurrent = () => true) {
      const result = await requests.run(requestSessions);
      if (!result.accepted || !isCurrent()) return false;
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
  const id = String(session.session_id || session.id || 'unknown session');
  const kind = session.session_kind === 'weekly' ? 'weekly' : 'classic';
  const participant = typeof session.participant_hash === 'string'
    ? session.participant_hash.slice(0, 12) : 'unavailable';
  const detail = kind === 'weekly'
    ? `round ${String(session.round_id || 'unknown')}`
    : `${source} · ${difficulty}`;
  return `${formatDate(session.started_at, 'unknown date')} · ${kind} · ${detail}`
    + ` · session ${id} · participant ${participant}`;
}

export function sessionSelectionKey(session) {
  const id = String(session?.session_id || session?.id || '');
  if (!id) return '';
  return `${session?.session_kind === 'weekly' ? 'weekly' : 'classic'}:${id}`;
}

export function replayActionForSession(session) {
  return session?.session_kind === 'weekly' ? 'weekly-attempts' : 'answers';
}

export function answerSelectionKey(answer) {
  return String(answer?.id || answer?.vote_attempt_id || '');
}

export function formatAnswerLabel(answer) {
  const question = Number(answer.question_index) + 1;
  const item = String(answer.item_id || 'unknown item');
  const weekly = !!answer.vote_attempt_id;
  const pick = answer.picked_none
    ? 'none'
    : (weekly ? `choice ${answer.choice_id ?? 'unknown'}`
      : `sample ${answer.picked_sample ?? 'unknown'}`);
  const result = typeof answer.picked_correct === 'boolean'
    ? (answer.picked_correct ? 'correct' : 'wrong')
    : 'blind vote';
  return `Question ${question} · ${item} · ${pick} · ${result}`
    + ` · answered ${formatDate(answer.answered_at || answer.submitted_at, 'unknown time')}`;
}

export function replaceSelectOptions(
  select,
  rows,
  labelFor,
  documentImpl = document,
  valueFor = row => row.id,
) {
  select.replaceChildren();
  const placeholder = documentImpl.createElement('option');
  placeholder.value = '';
  placeholder.textContent = rows.length ? 'Select…' : 'None available';
  select.appendChild(placeholder);
  for (const row of rows) {
    const option = documentImpl.createElement('option');
    option.value = String(valueFor(row) || '');
    option.textContent = labelFor(row);
    select.appendChild(option);
  }
  select.disabled = rows.length === 0;
}

export function formatCompactAppState(state) {
  if (!state || typeof state !== 'object' || Array.isArray(state)) return 'No app state recorded';
  const entries = Object.entries(state);
  if (!entries.length) return 'No app state recorded';
  return entries.map(([key, value]) => {
    let rendered;
    try {
      rendered = typeof value === 'string' ? value : JSON.stringify(value);
    } catch {
      rendered = '[unavailable]';
    }
    return `${key}: ${rendered}`;
  }).join('\n');
}

export function createReplayController({
  plugin,
  playTrace = playViewerTrace,
  validateTrace = validateViewerTrace,
  onAppEvent,
  onAppStateChange,
  onActivePaneChange,
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
        onAppEvent,
        onAppStateChange,
        onActivePaneChange,
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

export function createSessionAnswerLoader({
  answerRequests,
  connectionGeneration,
  playbackUi,
  requestAnswers,
  clearAnswers,
  applyAnswers,
  setStatus,
}) {
  return async function loadAnswers(
    sessionId,
    sessionGeneration = connectionGeneration.capture(),
  ) {
    clearAnswers();
    setStatus(sessionId ? 'Loading traced answers…' : 'Select a session.');
    try {
      const result = await answerRequests.run(async signal => {
        await playbackUi.stop({ announce: false });
        if (!sessionId) return [];
        return requestAnswers(sessionId, signal);
      });
      if (!result.accepted || !connectionGeneration.owns(sessionGeneration)) return false;
      applyAnswers(result.value, sessionId);
      return true;
    } catch (error) {
      if (!connectionGeneration.owns(sessionGeneration)) return false;
      clearAnswers();
      setStatus(error.message, true);
      return false;
    }
  };
}

export function createReplayConnectHandler({
  connectionGeneration = createConnectionGeneration(),
  sessionLoader,
  answerRequests,
  playbackUi,
  readPassword,
  rememberPassword,
  clearPasswordInput,
  clearAnswerState,
  clearSessionState,
  setConnectDisabled,
  setStatus,
}) {
  return async function connect() {
    const connectGeneration = connectionGeneration.advance();
    sessionLoader.cancel();
    answerRequests.cancel();
    const password = readPassword();
    rememberPassword(password);
    clearPasswordInput();
    const stopping = playbackUi.stop({ announce: false });
    clearSessionState();
    clearAnswerState();

    if (!password) {
      setStatus('Enter the replay password.', true);
      setConnectDisabled(false);
      return false;
    }

    setConnectDisabled(true);
    setStatus('Loading recent sessions…');
    await stopping;
    if (connectGeneration !== connectionGeneration.capture()) return false;

    try {
      const applied = await sessionLoader.load(
        () => connectionGeneration.owns(connectGeneration),
      );
      return connectionGeneration.owns(connectGeneration) && applied;
    } catch (error) {
      if (!connectionGeneration.owns(connectGeneration)) return false;
      clearSessionState();
      setStatus(error.message, true);
      return false;
    } finally {
      if (connectionGeneration.owns(connectGeneration)) setConnectDisabled(false);
    }
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
  const activePane = document.getElementById('active-pane');
  const appState = document.getElementById('app-state');
  const answersById = new Map();
  const sessionsById = new Map();
  const answerRequests = createLatestRequestGuard();
  const connectionGeneration = createConnectionGeneration();
  let renderedConnectionGeneration = connectionGeneration.capture();

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
    if (!Array.isArray(data)
      && !(payload.action === 'weekly-attempts'
        && Array.isArray(data?.attempts) && Array.isArray(data?.batches))) {
      throw new Error('Replay request failed');
    }
    return data;
  }

  let viewer;
  try {
    viewer = await molstar.Viewer.create('viewer', VIEWER_OPTIONS);
  } catch {
    setStatus('Could not initialize the molecular viewer.', true);
    return;
  }
  const replayController = createReplayController({
    plugin: viewer.plugin,
    onActivePaneChange(paneId) {
      activePane.textContent = paneId || 'Canonical viewer';
    },
    onAppStateChange(state) {
      appState.textContent = formatCompactAppState(state);
    },
  });
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
      renderedConnectionGeneration = connectionGeneration.capture();
      sessionsById.clear();
      for (const session of sessions) sessionsById.set(sessionSelectionKey(session), session);
      replaceSelectOptions(
        sessionSelect,
        sessions,
        formatSessionLabel,
        document,
        sessionSelectionKey,
      );
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
      setStatus(sessions.length ? 'Select a session.' : 'No replay sessions are available.');
    },
  });
  const connect = createReplayConnectHandler({
    connectionGeneration,
    sessionLoader,
    answerRequests,
    playbackUi,
    readPassword: () => passwordInput.value,
    rememberPassword(value) {
      replayPassword = value;
    },
    clearPasswordInput() {
      passwordInput.value = '';
    },
    clearAnswerState() {
      answersById.clear();
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
    },
    clearSessionState() {
      sessionsById.clear();
      replaceSelectOptions(sessionSelect, [], formatSessionLabel);
    },
    setConnectDisabled(value) {
      connectButton.disabled = value;
    },
    setStatus,
  });
  const loadSessionAnswers = createSessionAnswerLoader({
    answerRequests,
    connectionGeneration,
    playbackUi,
    requestAnswers: async (sessionKey, signal) => {
      const session = sessionsById.get(sessionKey);
      if (!session) throw new Error('Select a replay session.');
      const response = await requestReplay({
        action: replayActionForSession(session),
        session_id: session.session_id || session.id,
      }, signal);
      return session.session_kind === 'weekly'
        ? reconstructWeeklyAttempts(response.attempts, response.batches)
        : response;
    },
    clearAnswers() {
      answersById.clear();
      playButton.disabled = true;
      stopButton.disabled = true;
      replaceSelectOptions(answerSelect, [], formatAnswerLabel);
    },
    applyAnswers(answers, sessionId) {
      for (const answer of answers) answersById.set(answerSelectionKey(answer), answer);
      replaceSelectOptions(
        answerSelect,
        answers,
        formatAnswerLabel,
        document,
        answerSelectionKey,
      );
      setStatus(sessionId
        ? (answers.length ? 'Select an answer to replay.' : 'This session has no traced answers.')
        : 'Select a session.');
    },
    setStatus,
  });

  function loadAnswers() {
    return loadSessionAnswers(sessionSelect.value, renderedConnectionGeneration);
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
    activePane.textContent = 'Not playing';
    appState.textContent = 'No app state recorded';
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
