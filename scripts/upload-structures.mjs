import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

const BUCKET = 'structures';
const DEFAULT_CONCURRENCY = 6;

function authHeaders(key) {
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
  };
}

function storageUrl(url, pathname) {
  return `${url.replace(/\/$/, '')}${pathname}`;
}

async function walkPdbFiles(rootDir, directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(entries.map(async (entry) => {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return walkPdbFiles(rootDir, absolutePath);
    }
    if (entry.isFile() && path.extname(entry.name).toLowerCase() === '.pdb') {
      return [{
        absolutePath,
        objectKey: path.relative(rootDir, absolutePath).split(path.sep).join('/'),
      }];
    }
    return [];
  }));

  return files.flat();
}

export async function discoverPdbFiles(rootDir) {
  const groups = await Promise.all(
    ['data', 'data_rnp'].map(async (name) => {
      try {
        return await walkPdbFiles(rootDir, path.join(rootDir, name));
      } catch (error) {
        if (error.code === 'ENOENT') return [];
        throw error;
      }
    }),
  );

  return groups.flat().sort((left, right) => (
    left.objectKey < right.objectKey ? -1 : left.objectKey > right.objectKey ? 1 : 0
  ));
}

async function responseError(response) {
  const message = (await response.text()).trim();
  return `Storage request failed (${response.status})${message ? `: ${message}` : ''}`;
}

export async function ensurePublicBucket({ fetchImpl, url, key }) {
  const headers = authHeaders(key);
  const bucketUrl = storageUrl(url, `/storage/v1/bucket/${BUCKET}`);
  const lookup = await fetchImpl(bucketUrl, { method: 'GET', headers });
  if (lookup.ok) return;
  if (lookup.status !== 404) throw new Error(await responseError(lookup));

  const created = await fetchImpl(storageUrl(url, '/storage/v1/bucket'), {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: BUCKET, name: BUCKET, public: true }),
  });
  if (!created.ok) throw new Error(await responseError(created));
}

function encodedObjectPath(objectKey) {
  return objectKey.split('/').map(encodeURIComponent).join('/');
}

async function uploadOne({ file, fetchImpl, url, key, overwrite }) {
  const body = await readFile(file.absolutePath);
  const response = await fetchImpl(
    storageUrl(url, `/storage/v1/object/${BUCKET}/${encodedObjectPath(file.objectKey)}`),
    {
      method: 'POST',
      headers: {
        ...authHeaders(key),
        'Content-Type': 'chemical/x-pdb',
        'cache-control': '31536000',
        'x-upsert': String(overwrite),
      },
      body,
    },
  );

  if (response.ok) return 'uploaded';
  const message = await response.text();
  if (!overwrite && (response.status === 409 || /already exists|duplicate/i.test(message))) {
    return 'skipped';
  }
  return 'failed';
}

export async function uploadStructures({
  files,
  fetchImpl,
  url,
  key,
  overwrite = false,
  concurrency = DEFAULT_CONCURRENCY,
}) {
  const summary = { uploaded: 0, skipped: 0, failed: [] };
  const limit = Math.max(1, Math.min(DEFAULT_CONCURRENCY, Number(concurrency) || DEFAULT_CONCURRENCY));
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < files.length) {
      const file = files[nextIndex++];
      try {
        const result = await uploadOne({ file, fetchImpl, url, key, overwrite });
        if (result === 'uploaded') summary.uploaded += 1;
        else if (result === 'skipped') summary.skipped += 1;
        else summary.failed.push(file.objectKey);
      } catch {
        summary.failed.push(file.objectKey);
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, files.length) }, worker));
  return summary;
}

export async function runCli({
  env = process.env,
  args = process.argv.slice(2),
  log = console.log,
  error = console.error,
} = {}) {
  const { SUPABASE_URL: url, SUPABASE_SERVICE_ROLE_KEY: key } = env;
  if (!url || !key) {
    error('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.');
    return 1;
  }

  const files = await discoverPdbFiles(process.cwd());
  await ensurePublicBucket({ fetchImpl: fetch, url, key });
  const summary = await uploadStructures({
    files,
    fetchImpl: fetch,
    url,
    key,
    overwrite: args.includes('--overwrite'),
  });
  log(`Local: ${files.length}; uploaded: ${summary.uploaded}; skipped: ${summary.skipped}; failed: ${summary.failed.length}`);
  if (summary.failed.length) {
    error(`Failed: ${summary.failed.join(', ')}`);
    return 1;
  }
  return 0;
}

if (process.argv[1]?.endsWith('upload-structures.mjs')) {
  runCli().then((exitCode) => {
    process.exitCode = exitCode;
  }).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
