import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  discoverPdbFiles,
  ensurePublicBucket,
  uploadStructures,
} from '../scripts/upload-structures.mjs';

function response(status, body = '') {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `status ${status}`,
    text: async () => body,
  };
}

async function withTempDirectory(fn) {
  const rootDir = await mkdtemp(path.join(tmpdir(), 'foldarium-structures-'));
  try {
    await fn(rootDir);
  } finally {
    await rm(rootDir, { recursive: true, force: true });
  }
}

test('discovers only PDB files and preserves repository-relative keys', async () => {
  await withTempDirectory(async (rootDir) => {
    await mkdir(path.join(rootDir, 'data/A'), { recursive: true });
    await mkdir(path.join(rootDir, 'data_rnp/B'), { recursive: true });
    await writeFile(path.join(rootDir, 'data/A/pose-1.pdb'), 'ATOM\n');
    await writeFile(path.join(rootDir, 'data/A/readme.txt'), 'not a structure\n');
    await writeFile(path.join(rootDir, 'data_rnp/B/protein.pdb'), 'ATOM\n');

    const files = await discoverPdbFiles(rootDir);

    assert.deepEqual(files.map((file) => file.objectKey), [
      'data/A/pose-1.pdb',
      'data_rnp/B/protein.pdb',
    ]);
    assert.deepEqual(files.map((file) => file.absolutePath), [
      path.join(rootDir, 'data/A/pose-1.pdb'),
      path.join(rootDir, 'data_rnp/B/protein.pdb'),
    ]);
  });
});

test('creates a missing public structures bucket', async () => {
  const requests = [];
  const fetchImpl = async (url, options = {}) => {
    requests.push({ url, ...options });
    return options.method === 'GET' ? response(404) : response(200);
  };

  await ensurePublicBucket({
    fetchImpl,
    url: 'https://project.test',
    key: 'secret',
  });

  const [lookupRequest, createRequest] = requests;
  assert.equal(lookupRequest.url, 'https://project.test/storage/v1/bucket/structures');
  assert.equal(lookupRequest.headers.apikey, 'secret');
  assert.equal(lookupRequest.headers.Authorization, 'Bearer secret');
  assert.equal(createRequest.url, 'https://project.test/storage/v1/bucket');
  assert.equal(createRequest.method, 'POST');
  assert.deepEqual(JSON.parse(createRequest.body), {
    id: 'structures',
    name: 'structures',
    public: true,
  });
});

test('counts successful and existing uploads without failing', async () => {
  await withTempDirectory(async (rootDir) => {
    const uploadedPath = path.join(rootDir, 'pose-1.pdb');
    const existingPath = path.join(rootDir, 'pose-2.pdb');
    await writeFile(uploadedPath, 'ATOM 1\n');
    await writeFile(existingPath, 'ATOM 2\n');
    const requests = [];
    const fetchImpl = async (url, options) => {
      requests.push({ url, ...options });
      return url.endsWith('pose-1.pdb')
        ? response(200)
        : response(409, 'The resource already exists');
    };

    const summary = await uploadStructures({
      files: [
        { absolutePath: uploadedPath, objectKey: 'data/A/pose-1.pdb' },
        { absolutePath: existingPath, objectKey: 'data/A/pose-2.pdb' },
      ],
      fetchImpl,
      url: 'https://project.test',
      key: 'secret',
      overwrite: false,
      concurrency: 2,
    });

    assert.deepEqual(summary, { uploaded: 1, skipped: 1, failed: [] });
    assert.equal(requests[0].headers['Content-Type'], 'chemical/x-pdb');
    assert.equal(requests[0].headers['cache-control'], '31536000');
    assert.equal(requests[0].headers['x-upsert'], 'false');
  });
});

test('reports failed object keys without stopping remaining uploads', async () => {
  await withTempDirectory(async (rootDir) => {
    const goodPath = path.join(rootDir, 'good.pdb');
    const brokenPath = path.join(rootDir, 'broken.pdb');
    await writeFile(goodPath, 'ATOM 1\n');
    await writeFile(brokenPath, 'ATOM 2\n');
    const fetchImpl = async (url) => (
      url.endsWith('broken.pdb') ? response(500, 'storage unavailable') : response(200)
    );

    const summary = await uploadStructures({
      files: [
        { absolutePath: goodPath, objectKey: 'data/A/good.pdb' },
        { absolutePath: brokenPath, objectKey: 'data/A/broken.pdb' },
      ],
      fetchImpl,
      url: 'https://project.test',
      key: 'secret',
      overwrite: false,
      concurrency: 2,
    });

    assert.deepEqual(summary, {
      uploaded: 1,
      skipped: 0,
      failed: ['data/A/broken.pdb'],
    });
  });
});
