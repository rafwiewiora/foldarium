export function resolveAssetUrl(path, baseUrl = '') {
  if (!path || /^https?:\/\//i.test(path) || !baseUrl) return path;
  return `${baseUrl.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}

if (typeof window !== 'undefined') {
  window.foldariumAssetUrl = path => resolveAssetUrl(
    path,
    window.FOLDARIUM_SUPABASE?.structureBaseUrl || '',
  );
}
