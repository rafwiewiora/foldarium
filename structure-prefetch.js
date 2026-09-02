const DEFAULT_CONCURRENCY = 4;
const DEFAULT_MAX_BYTES = 64 * 1024 * 1024;

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

export function gridQuestionAssetPaths(item, clusters, {
  page = 0,
  pageSize = 9,
  clustered = true,
  showProteinEnsemble = false,
} = {}) {
  if (!item || !Array.isArray(clusters)) return [];
  const entries = clustered
    ? clusters.map(cluster => ({
        choice: cluster?.rep || cluster?.members?.[0],
        members: cluster?.members || [],
      }))
    : clusters.flatMap(cluster => (cluster?.members || []).map(choice => ({
        choice,
        members: [choice],
      })));
  const start = Math.max(0, page) * Math.max(1, pageSize);
  const visible = entries.slice(start, start + Math.max(1, pageSize));
  const paths = [];
  for (const { choice, members } of visible) {
    if (!choice) continue;
    const protein = choice.afprotein_file || item.protein_file;
    const pocket = choice.afpocket_file || item.pocket_file;
    paths.push(protein, pocket);
    const poseMembers = clustered ? members : [choice];
    paths.push(...poseMembers.map(member => member?.pose_file));
    if (showProteinEnsemble && clustered) {
      paths.push(...members.map(member => member?.afprotein_file)
        .filter(memberProtein => memberProtein && memberProtein !== protein));
    }
  }
  // Grid renders choice-specific proteins, while its hidden canonical scene
  // also prepares the shared display frame for seamless mode switching.
  paths.push(item.protein_file, item.pocket_file);
  return [...new Set(paths.filter(Boolean))];
}

export function createStructurePrefetcher({
  fetchImpl = globalThis.fetch,
  connection = globalThis.navigator?.connection,
  concurrency = DEFAULT_CONCURRENCY,
  maxBytes = DEFAULT_MAX_BYTES,
} = {}) {
  if (typeof fetchImpl !== 'function') {
    throw new TypeError('fetchImpl must be a function');
  }
  if (!Number.isInteger(concurrency) || concurrency < 1) {
    throw new TypeError('concurrency must be a positive integer');
  }
  if (!Number.isInteger(maxBytes) || maxBytes < 1) {
    throw new TypeError('maxBytes must be a positive integer');
  }

  const completed = new Set();
  const bytesByUrl = new Map();
  const queuedByUrl = new Map();
  const inFlightByUrl = new Map();
  const waiters = new Set();
  let cachedBytes = 0;
  let activeCount = 0;
  let queueOrder = 0;
  let cancelledGeneration = 0;

  function cancel() {
    cancelledGeneration += 1;
    queuedByUrl.clear();
    for (const controller of inFlightByUrl.values()) controller.abort();
    inFlightByUrl.clear();
    for (const waiter of waiters) {
      waiter.resolve({ fetched: [...waiter.fetched], skipped: 'cancelled' });
    }
    waiters.clear();
  }

  function cacheBytes(url, bytes, priority) {
    const previous = bytesByUrl.get(url);
    if (previous) {
      cachedBytes -= previous.bytes.byteLength;
      bytesByUrl.delete(url);
    }
    while (bytes.byteLength <= maxBytes
        && cachedBytes + bytes.byteLength > maxBytes
        && bytesByUrl.size) {
      const oldestUrl = bytesByUrl.keys().next().value;
      const oldest = bytesByUrl.get(oldestUrl);
      bytesByUrl.delete(oldestUrl);
      completed.delete(oldestUrl);
      cachedBytes -= oldest.bytes.byteLength;
    }
    if (bytes.byteLength > maxBytes) return false;
    completed.add(url);
    bytesByUrl.set(url, { bytes, priority });
    cachedBytes += bytes.byteLength;
    return true;
  }

  function settleUrl(url, fetched) {
    for (const waiter of [...waiters]) {
      if (!waiter.pending.delete(url)) continue;
      if (fetched) waiter.fetched.add(url);
      if (waiter.pending.size) continue;
      waiters.delete(waiter);
      waiter.resolve({ fetched: [...waiter.fetched], skipped: null });
    }
  }

  function nextQueued() {
    let selected = null;
    for (const entry of queuedByUrl.values()) {
      if (!selected || entry.priority > selected.priority
          || (entry.priority === selected.priority && entry.order < selected.order)) {
        selected = entry;
      }
    }
    if (selected) queuedByUrl.delete(selected.url);
    return selected;
  }

  function pump() {
    while (activeCount < concurrency && queuedByUrl.size) {
      const entry = nextQueued();
      const generation = cancelledGeneration;
      const controller = new AbortController();
      activeCount += 1;
      inFlightByUrl.set(entry.url, controller);
      void (async () => {
        let fetched = false;
        try {
          const response = await fetchImpl(entry.url, {
            cache: 'force-cache',
            signal: controller.signal,
          });
          if (!response.ok) return;
          const bytes = await response.arrayBuffer();
          if (generation !== cancelledGeneration || controller.signal.aborted) return;
          fetched = cacheBytes(entry.url, bytes, entry.priority);
        } catch (error) {
          if (error?.name !== 'AbortError') completed.delete(entry.url);
        } finally {
          if (inFlightByUrl.get(entry.url) === controller) {
            inFlightByUrl.delete(entry.url);
            settleUrl(entry.url, fetched);
          }
          activeCount -= 1;
          pump();
        }
      })();
    }
  }

  function waitForUrls(urls) {
    const pending = new Set(urls.filter(url => !completed.has(url)));
    if (!pending.size) {
      return Promise.resolve({ fetched: [], skipped: null });
    }
    return new Promise(resolve => {
      waiters.add({ pending, fetched: new Set(), resolve });
    });
  }

  async function prefetch(urls, { priority = 0 } = {}) {
    if (!connectionAllowsPrefetch(connection)) return { fetched: [], skipped: 'connection' };
    const requested = [...new Set((urls || []).filter(Boolean))];
    const pendingResult = waitForUrls(requested);
    for (const url of requested) {
      const cached = bytesByUrl.get(url);
      if (cached) {
        cached.priority = Math.max(cached.priority, priority);
        continue;
      }
      const queued = queuedByUrl.get(url);
      if (queued) {
        queued.priority = Math.max(queued.priority, priority);
        continue;
      }
      if (!inFlightByUrl.has(url)) {
        queuedByUrl.set(url, { url, priority, order: queueOrder++ });
      }
    }
    pump();
    return pendingResult;
  }

  async function textWhenReady(url) {
    const immediate = text(url);
    if (immediate !== null) return immediate;
    const queued = queuedByUrl.get(url);
    if (queued) {
      queued.priority = Number.MAX_SAFE_INTEGER;
      pump();
      // Do not make a foreground Grid wait behind the bounded background queue.
      // Claim queued work for Mol* directly while preserving already in-flight reuse.
      if (!inFlightByUrl.has(url)) {
        queuedByUrl.delete(url);
        settleUrl(url, false);
        return null;
      }
    }
    if (!inFlightByUrl.has(url)) return null;
    const result = await waitForUrls([url]);
    if (result.skipped) return null;
    return text(url);
  }

  function text(url) {
    const entry = bytesByUrl.get(url);
    if (entry === undefined) return null;
    // Refresh insertion order so active/revisited structures survive bounded-cache eviction.
    bytesByUrl.delete(url);
    bytesByUrl.set(url, entry);
    return new TextDecoder().decode(entry.bytes);
  }

  return {
    cancel,
    prefetch,
    completed,
    get cachedBytes() {
      return cachedBytes;
    },
    get queuedCount() {
      return queuedByUrl.size;
    },
    get activeCount() {
      return activeCount;
    },
    text,
    textWhenReady,
  };
}

if (typeof window !== 'undefined') {
  window.foldariumStructurePrefetch = {
    createStructurePrefetcher,
    gridQuestionAssetPaths,
    initialQuestionAssetPaths,
  };
}
