export function createViewerRebuildCoordinator({
  rebuild,
  setBusy = () => {},
}) {
  const queue = [];
  let running = false;
  let idleWaiters = [];

  function enqueue(mutate = () => {}) {
    const result = new Promise((resolve, reject) => {
      queue.push({ mutate, resolve, reject });
    });
    if (!running) {
      running = true;
      setBusy(true);
      void drain();
    }
    return result;
  }

  async function drain() {
    while (queue.length) {
      const job = queue.shift();
      try {
        await job.mutate();
        await rebuild();
        job.resolve();
      } catch (error) {
        job.reject(error);
      }
    }

    running = false;
    setBusy(false);
    if (!running && queue.length === 0) {
      const waiters = idleWaiters;
      idleWaiters = [];
      for (const resolve of waiters) resolve();
    }
  }

  function waitForIdle() {
    if (!running && queue.length === 0) return Promise.resolve();
    return new Promise(resolve => { idleWaiters.push(resolve); });
  }

  return {
    enqueue,
    waitForIdle,
    get pending() {
      return running || queue.length > 0;
    },
  };
}

export function createRevealAfterIdle({ coordinator, reveal }) {
  let activeReveal = null;

  return function revealAfterIdle() {
    if (!activeReveal) {
      const run = (async () => {
        await coordinator.waitForIdle();
        return reveal();
      })();
      activeReveal = run.finally(() => {
        activeReveal = null;
      });
    }
    return activeReveal;
  };
}
