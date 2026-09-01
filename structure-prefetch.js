const DEFAULT_CONCURRENCY = 2;

export function connectionAllowsPrefetch(connection) {
  if (!connection) return true;
  if (connection.saveData) return false;
  return !['slow-2g', '2g'].includes(connection.effectiveType);
}

export function initialQuestionAssetPaths(item, initialChoice = item?.choices?.[0]) {
  if (!item) return [];
  return [...new Set([
    item.protein_file,
    item.pocket_file,
    initialChoice?.pose_file,
    initialChoice?.afprotein_file,
    initialChoice?.afpocket_file,
  ].filter(Boolean))];
}

export function createStructurePrefetcher({
  fetchImpl = globalThis.fetch,
  connection = globalThis.navigator?.connection,
  concurrency = DEFAULT_CONCURRENCY,
} = {}) {
  if (typeof fetchImpl !== 'function') {
    throw new TypeError('fetchImpl must be a function');
  }
  if (!Number.isInteger(concurrency) || concurrency < 1) {
    throw new TypeError('concurrency must be a positive integer');
  }

  const completed = new Set();
  const bytesByUrl = new Map();
  let active = null;
  let revision = 0;

  function cancel() {
    revision += 1;
    active?.abort();
    active = null;
  }

  async function prefetch(urls) {
    cancel();
    if (!connectionAllowsPrefetch(connection)) return { fetched: [], skipped: 'connection' };

    const taskRevision = revision;
    const controller = new AbortController();
    active = controller;
    const queue = [...new Set((urls || []).filter(Boolean))]
      .filter(url => !completed.has(url));
    const fetched = [];
    let cursor = 0;

    async function worker() {
      while (taskRevision === revision && cursor < queue.length) {
        const url = queue[cursor++];
        try {
          const response = await fetchImpl(url, {
            cache: 'force-cache',
            signal: controller.signal,
          });
          if (!response.ok) continue;
          const bytes = await response.arrayBuffer();
          if (taskRevision !== revision) return;
          completed.add(url);
          bytesByUrl.set(url, bytes);
          fetched.push(url);
        } catch (error) {
          if (error?.name !== 'AbortError') {
            // A later navigation may retry transient failures.
            completed.delete(url);
          }
        }
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(concurrency, queue.length) }, worker),
    );
    if (active === controller) active = null;
    return { fetched, skipped: taskRevision === revision ? null : 'stale' };
  }

  return {
    cancel,
    prefetch,
    completed,
    text(url) {
      const bytes = bytesByUrl.get(url);
      return bytes === undefined ? null : new TextDecoder().decode(bytes);
    },
  };
}

if (typeof window !== 'undefined') {
  window.foldariumStructurePrefetch = {
    createStructurePrefetcher,
    initialQuestionAssetPaths,
  };
}
