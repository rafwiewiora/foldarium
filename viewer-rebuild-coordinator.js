export function createViewerRebuildCoordinator({
  rebuild,
  setBusy = () => {},
}) {
  const queue = [];
  let running = false;
  let idleWaiters = [];

  function enqueue(mutate = () => {}, finalize = () => {}) {
    const result = new Promise((resolve, reject) => {
      queue.push({ mutate, finalize, resolve, reject });
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
        await job.finalize();
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

export function waitForCameraSettled({
  cameraChanged,
  requestReset,
  settleMs = 300,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  return new Promise((resolve, reject) => {
    let timer = null;
    let subscription = null;
    let finished = false;

    const cleanup = () => {
      if (timer !== null) clearTimer(timer);
      timer = null;
      subscription?.unsubscribe();
    };
    const finish = () => {
      if (finished) return;
      finished = true;
      cleanup();
      resolve();
    };
    const schedule = () => {
      if (finished) return;
      if (timer !== null) clearTimer(timer);
      timer = setTimer(() => {
        timer = null;
        finish();
      }, settleMs);
    };

    try {
      subscription = cameraChanged?.subscribe(schedule) ?? null;
      requestReset();
      schedule();
    } catch (error) {
      finished = true;
      cleanup();
      reject(error);
    }
  });
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
