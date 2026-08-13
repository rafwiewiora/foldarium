const DEFAULT_KEY = 'foldariumWeeklyResumeV1';

function normalizedToken(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || value.version !== 1
      || typeof value.session_id !== 'string' || !value.session_id
      || typeof value.round_id !== 'string' || !value.round_id
      || !Number.isInteger(value.question_index) || value.question_index < 0) return null;
  return {
    version: 1,
    session_id: value.session_id,
    round_id: value.round_id,
    question_index: value.question_index,
  };
}

export function createWeeklySessionResumeStore(storage = globalThis.sessionStorage, key = DEFAULT_KEY) {
  return {
    read() {
      try { return normalizedToken(JSON.parse(storage.getItem(key))); }
      catch { return null; }
    },
    save({ sessionId, roundId, questionIndex }) {
      const token = normalizedToken({
        version: 1,
        session_id: sessionId,
        round_id: roundId,
        question_index: questionIndex,
      });
      if (!token) throw new Error('Weekly resume token is invalid.');
      storage.setItem(key, JSON.stringify(token));
      return token;
    },
    clear() {
      try { storage.removeItem(key); } catch {}
    },
    hasToken() {
      return this.read() !== null;
    },
  };
}
