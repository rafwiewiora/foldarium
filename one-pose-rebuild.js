export function createExclusiveViewerRebuild({ rebuild, setBusy = () => {} }) {
  let pending = false;

  return {
    get pending() {
      return pending;
    },

    async run() {
      if (pending) return false;
      pending = true;
      setBusy(true);
      try {
        await rebuild();
        return true;
      } finally {
        pending = false;
        setBusy(false);
      }
    },
  };
}
