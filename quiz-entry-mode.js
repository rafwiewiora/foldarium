export function quizEntryMode(pathname = '/') {
  return /^\/weekly(?:\.html)?\/?$/.test(pathname) ? 'weekly' : 'classic';
}

export function programmaticVotingEntry(search = '') {
  return new URLSearchParams(search).get('programmatic') === '1';
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const mode = quizEntryMode(window.location.pathname);
  const programmaticVoting = mode === 'weekly'
    && programmaticVotingEntry(window.location.search);
  window.FOLDARIUM_QUIZ_MODE = mode;
  window.FOLDARIUM_PROGRAMMATIC_VOTING = programmaticVoting;
  document.documentElement.dataset.quizMode = mode;
  document.documentElement.dataset.programmaticVoting = String(programmaticVoting);
}
