import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

const BUCKET = 'structures';
const DEFAULT_CONCURRENCY = 6;
const CONTENT_TYPES = {
  '.pdb': 'chemical/x-pdb',
  '.cif': 'chemical/x-cif',
};

function authHeaders(key) {
  const headers = { apikey: key };
  if (!key.startsWith('sb_secret_')) headers.Authorization = `Bearer ${key}`;
  return headers;
}

function storageUrl(url, pathname) {
  return `${url.replace(/\/$/, '')}${pathname}`;
}

async function walkStructureFiles(rootDir, directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(entries.map(async (entry) => {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return walkStructureFiles(rootDir, absolutePath);
    }
    const contentType = CONTENT_TYPES[path.extname(entry.name).toLowerCase()];
    if (entry.isFile() && contentType) {
      return [{
        absolutePath,
        objectKey: path.relative(rootDir, absolutePath).split(path.sep).join('/'),
        contentType,
      }];
    }
    return [];
  }));

  return files.flat();
}

export async function discoverStructureFiles(rootDir, benchmarkDir) {
  const groups = await Promise.all(
    ['data', 'data_rnp'].map(async (name) => {
      try {
        return await walkStructureFiles(rootDir, path.join(rootDir, name));
      } catch (error) {
        if (error.code === 'ENOENT') return [];
        throw error;
      }
    }),
  );

  if (benchmarkDir) {
    const benchmarkGroups = await Promise.all(
      ['systems', 'systems_rnp'].map(async (name) => {
        try {
          const files = await walkStructureFiles(benchmarkDir, path.join(benchmarkDir, name));
          return files.map((file) => ({
            ...file,
            objectKey: `benchmark/demo/${file.objectKey}`,
          }));
        } catch (error) {
          if (error.code === 'ENOENT') return [];
          throw error;
        }
      }),
    );
    groups.push(...benchmarkGroups);
  }

  return groups.flat().sort((left, right) => (
    left.objectKey < right.objectKey ? -1 : left.objectKey > right.objectKey ? 1 : 0
  ));
}

function redactCredential(message, key) {
  return String(message).split(key).join('[redacted]');
}

function responseErrorMessage(response, message, key) {
  return `Storage request failed (${response.status})${message ? `: ${redactCredential(message, key)}` : ''}`;
}

async function responseError(response, key) {
  const message = (await response.text()).trim();
  return responseErrorMessage(response, message, key);
}

export async function ensurePublicBucket({ fetchImpl, url, key }) {
  const headers = authHeaders(key);
  const bucketUrl = storageUrl(url, `/storage/v1/bucket/${BUCKET}`);
  const lookup = await fetchImpl(bucketUrl, { method: 'GET', headers });
  if (lookup.ok) return;
  if (lookup.status !== 404) {
    const message = (await lookup.text()).trim();
    const error = parseStorageError(message);
    const missing = lookup.status === 400
      && (error?.code === 'NoSuchBucket' || String(error?.statusCode) === '404');
    if (!missing) throw new Error(responseErrorMessage(lookup, message, key));
  }

  const created = await fetchImpl(storageUrl(url, '/storage/v1/bucket'), {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: BUCKET, name: BUCKET, public: true }),
  });
  if (!created.ok) throw new Error(await responseError(created, key));
}

function encodedObjectPath(objectKey) {
  return objectKey.split('/').map(encodeURIComponent).join('/');
}

function parseStorageError(message) {
  try {
    return JSON.parse(message);
  } catch {
    return null;
  }
}

function isExistingObjectResponse(response, error) {
  if (response.status !== 409) return false;
  if (['KeyAlreadyExists', 'ResourceAlreadyExists'].includes(error?.code)) return true;
  return error?.statusCode === '409'
    && error.error === 'Duplicate'
    && typeof error.message === 'string'
    && /resource already exists/i.test(error.message);
}

async function uploadOne({ file, fetchImpl, url, key, overwrite }) {
  const body = await readFile(file.absolutePath);
  const response = await fetchImpl(
    storageUrl(url, `/storage/v1/object/${BUCKET}/${encodedObjectPath(file.objectKey)}`),
    {
      method: 'POST',
      headers: {
        ...authHeaders(key),
        'Content-Type': file.contentType || 'chemical/x-pdb',
        'cache-control': 'max-age=31536000',
        'x-upsert': String(overwrite),
      },
      body,
    },
  );

  if (response.ok) return { outcome: 'uploaded' };
  const message = await response.text();
  const error = parseStorageError(message);
  if (!overwrite && isExistingObjectResponse(response, error)) {
    return { outcome: 'skipped' };
  }
  return {
    outcome: 'failed',
    detail: {
      status: response.status,
      message: redactCredential(error?.message || message, key),
    },
  };
}

export async function uploadStructures({
  files,
  fetchImpl,
  url,
  key,
  overwrite = false,
  concurrency = DEFAULT_CONCURRENCY,
  onFailure = () => {},
}) {
  const summary = { uploaded: 0, skipped: 0, failed: [] };
  const limit = Math.max(1, Math.min(DEFAULT_CONCURRENCY, Number(concurrency) || DEFAULT_CONCURRENCY));
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < files.length) {
      const file = files[nextIndex++];
      try {
        const result = await uploadOne({ file, fetchImpl, url, key, overwrite });
        if (result.outcome === 'uploaded') summary.uploaded += 1;
        else if (result.outcome === 'skipped') summary.skipped += 1;
        else {
          summary.failed.push(file.objectKey);
          onFailure({ objectKey: file.objectKey, ...result.detail });
        }
      } catch (error) {
        summary.failed.push(file.objectKey);
        onFailure({
          objectKey: file.objectKey,
          status: null,
          message: redactCredential(error.message, key),
        });
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, files.length) }, worker));
  return summary;
}

export async function runCli({
  env = process.env,
  args = process.argv.slice(2),
  fetchImpl = fetch,
  rootDir = process.cwd(),
  log = console.log,
  error = console.error,
} = {}) {
  const { SUPABASE_URL: url, SUPABASE_SERVICE_ROLE_KEY: key } = env;
  if (!url || !key) {
    error('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.');
    return 1;
  }

  const files = await discoverStructureFiles(rootDir, env.BENCHMARK_DEMO_DIR);
  await ensurePublicBucket({ fetchImpl, url, key });
  const failures = [];
  const summary = await uploadStructures({
    files,
    fetchImpl,
    url,
    key,
    overwrite: args.includes('--overwrite'),
    onFailure: (failure) => failures.push(failure),
  });
  log(`Local: ${files.length}; uploaded: ${summary.uploaded}; skipped: ${summary.skipped}; failed: ${summary.failed.length}`);
  if (summary.failed.length) {
    for (const failure of failures) {
      error(`Failed: ${failure.objectKey} (${failure.status ?? 'request'}): ${failure.message}`);
    }
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
