function disposeViewer(viewer) {
  try { viewer?.dispose?.(); } catch (_) {}
}

export function createGridViewerPool({
  enabled = false,
  maxSize = 9,
} = {}) {
  if (!Number.isInteger(maxSize) || maxSize < 1) {
    throw new TypeError('Grid viewer pool maxSize must be a positive integer');
  }

  const slots = [];

  function add(slot, { source = 'prewarmed' } = {}) {
    const reusable = enabled
      && slot?.viewer
      && slot.plugin
      && slot.host
      && slots.length < maxSize;
    if (!reusable) {
      disposeViewer(slot?.viewer);
      return false;
    }
    slots.push({
      viewer: slot.viewer,
      plugin: slot.plugin,
      host: slot.host,
      source,
      clearing: Promise.resolve({ ok: true }),
    });
    return true;
  }

  function release(cell, { clear = () => cell?.plugin?.clear?.() } = {}) {
    const reusable = enabled
      && cell?.reusable === true
      && cell.viewer
      && cell.plugin
      && cell.host
      && slots.length < maxSize;
    if (!reusable) {
      disposeViewer(cell?.viewer);
      return false;
    }

    let clearing;
    try {
      clearing = Promise.resolve(clear())
        .then(() => ({ ok: true }), error => ({ ok: false, error }));
    } catch (error) {
      clearing = Promise.resolve({ ok: false, error });
    }
    slots.push({
      viewer: cell.viewer,
      plugin: cell.plugin,
      host: cell.host,
      source: 'recycled',
      clearing,
    });
    return true;
  }

  async function acquire() {
    while (slots.length) {
      const slot = slots.shift();
      const result = await slot.clearing;
      if (result.ok) return slot;
      disposeViewer(slot.viewer);
    }
    return null;
  }

  function drain() {
    while (slots.length) disposeViewer(slots.shift().viewer);
  }

  return {
    enabled,
    add,
    release,
    acquire,
    drain,
    size: () => slots.length,
  };
}
