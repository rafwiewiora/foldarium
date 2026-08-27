export function quizEntryMode(pathname = '/') {
  return /^\/weekly(?:\.html)?\/?$/.test(pathname) ? 'weekly' : 'classic';
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const mode = quizEntryMode(window.location.pathname);
  window.FOLDARIUM_QUIZ_MODE = mode;
  document.documentElement.dataset.quizMode = mode;
}
