export function resolveAssetUrl(path, baseUrl = '', supabaseUrl = '') {
  if (!path || /^https?:\/\//i.test(path)) return path;
  if (path.startsWith('supabase://')) {
    const match = /^supabase:\/\/([A-Za-z0-9._-]+)\/(.+)$/.exec(path);
    if (!match || !supabaseUrl) return path;
    const segments = match[2].split('/');
    if (segments.some(segment => !segment || segment === '.' || segment === '..')) return path;
    const objectPath = segments.map(segment => encodeURIComponent(segment)).join('/');
    return `${supabaseUrl.replace(/\/+$/, '')}/storage/v1/object/public/`
      + `${encodeURIComponent(match[1])}/${objectPath}`;
  }
  if (!baseUrl) return path;
  return `${baseUrl.replace(/\/+$/, '')}/${path.replace(/^\/+/, '')}`;
}

if (typeof window !== 'undefined') {
  window.foldariumAssetUrl = path => resolveAssetUrl(
    path,
    window.FOLDARIUM_SUPABASE?.structureBaseUrl || '',
    window.FOLDARIUM_SUPABASE?.url || '',
  );
}
