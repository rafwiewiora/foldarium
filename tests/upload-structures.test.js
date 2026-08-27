import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  discoverStructureFiles,
  ensurePublicBucket,
  runCli,
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

test('discovers only supported structure files and preserves repository-relative keys', async () => {
  await withTempDirectory(async (rootDir) => {
    await mkdir(path.join(rootDir, 'data/A'), { recursive: true });
    await mkdir(path.join(rootDir, 'data_rnp/B'), { recursive: true });
    await writeFile(path.join(rootDir, 'data/A/pose-1.pdb'), 'ATOM\n');
    await writeFile(path.join(rootDir, 'data/A/readme.txt'), 'not a structure\n');
    await writeFile(path.join(rootDir, 'data_rnp/B/protein.pdb'), 'ATOM\n');

    const files = await discoverStructureFiles(rootDir);

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

test('discovers benchmark PDB and CIF files with benchmark object keys and content types', async () => {
  await withTempDirectory(async (rootDir) => {
    const benchmarkDir = path.join(rootDir, 'external-benchmark', 'demo');
    await mkdir(path.join(benchmarkDir, 'systems/1ABC'), { recursive: true });
    await mkdir(path.join(benchmarkDir, 'systems_rnp/2DEF'), { recursive: true });
    await writeFile(path.join(benchmarkDir, 'systems/1ABC/pose.pdb'), 'ATOM\n');
    await writeFile(path.join(benchmarkDir, 'systems_rnp/2DEF/xtal.cif'), 'data_test\n');
    await writeFile(path.join(benchmarkDir, 'systems/1ABC/readme.txt'), 'ignored\n');

    const files = await discoverStructureFiles(rootDir, benchmarkDir);

    assert.deepEqual(
      files.map(({ objectKey }) => objectKey),
      [
        'benchmark/demo/systems/1ABC/pose.pdb',
        'benchmark/demo/systems_rnp/2DEF/xtal.cif',
      ],
    );

    const requests = [];
    const summary = await uploadStructures({
      files,
      fetchImpl: async (url, options) => {
        requests.push({ url, ...options });
        return response(200);
      },
      url: 'https://project.test',
      key: 'secret',
    });

    assert.deepEqual(summary, { uploaded: 2, skipped: 0, failed: [] });
    assert.deepEqual(
      requests.map((request) => request.headers['Content-Type']).sort(),
      ['chemical/x-cif', 'chemical/x-pdb'],
    );
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
    key: 'sb_secret_test',
  });

  const [lookupRequest, createRequest] = requests;
  assert.equal(lookupRequest.url, 'https://project.test/storage/v1/bucket/structures');
  assert.equal(lookupRequest.headers.apikey, 'sb_secret_test');
  assert.equal(lookupRequest.headers.Authorization, undefined);
  assert.equal(createRequest.url, 'https://project.test/storage/v1/bucket');
  assert.equal(createRequest.method, 'POST');
  assert.deepEqual(JSON.parse(createRequest.body), {
    id: 'structures',
    name: 'structures',
    public: true,
  });
});

test('creates the bucket for Supabase NoSuchBucket responses', async () => {
  const requests = [];
  await ensurePublicBucket({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, ...options });
      if (options.method === 'GET') {
        return response(400, JSON.stringify({
          statusCode: '404',
          error: 'Bucket not found',
          message: 'Bucket not found',
          code: 'NoSuchBucket',
        }));
      }
      return response(200);
    },
    url: 'https://project.test',
    key: 'sb_secret_test',
  });

  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, 'https://project.test/storage/v1/bucket');
  assert.equal(requests[1].method, 'POST');
});

test('uses a bearer header for legacy JWT service-role keys', async () => {
  let request;
  await ensurePublicBucket({
    fetchImpl: async (url, options = {}) => {
      request = { url, ...options };
      return response(200);
    },
    url: 'https://project.test',
    key: 'eyJlegacy-service-role',
  });

  assert.equal(request.headers.apikey, 'eyJlegacy-service-role');
  assert.equal(request.headers.Authorization, 'Bearer eyJlegacy-service-role');
});

test('redacts credentials from bucket setup errors', async () => {
  const key = 'bucket-secret';

  await assert.rejects(
    ensurePublicBucket({
      fetchImpl: async () => response(500, `lookup failed: ${key}`),
      url: 'https://project.test',
      key,
    }),
    (error) => {
      assert.match(error.message, /\[redacted\]/);
      assert.doesNotMatch(error.message, /bucket-secret/);
      return true;
    },
  );

  await assert.rejects(
    ensurePublicBucket({
      fetchImpl: async (url, options = {}) => (
        options.method === 'GET'
          ? response(404)
          : response(500, `creation failed: ${key}`)
      ),
      url: 'https://project.test',
      key,
    }),
    (error) => {
      assert.match(error.message, /\[redacted\]/);
      assert.doesNotMatch(error.message, /bucket-secret/);
      return true;
    },
  );
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
        : response(409, JSON.stringify({
          code: 'KeyAlreadyExists',
          message: 'Object already exists',
        }));
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

    assert.deepEqual(summary, {
      uploaded: 1,
      skipped: 1,
      failed: [],
    });
    assert.equal(requests[0].headers['Content-Type'], 'chemical/x-pdb');
    assert.equal(requests[0].headers['cache-control'], 'max-age=31536000');
    assert.equal(requests[0].headers['x-upsert'], 'false');
  });
});

test('retains legacy Supabase duplicate response compatibility', async () => {
  await withTempDirectory(async (rootDir) => {
    const existingPath = path.join(rootDir, 'pose-2.pdb');
    await writeFile(existingPath, 'ATOM 2\n');

    const summary = await uploadStructures({
      files: [{ absolutePath: existingPath, objectKey: 'data/A/pose-2.pdb' }],
      fetchImpl: async () => response(409, JSON.stringify({
        statusCode: '409',
        error: 'Duplicate',
        message: 'The resource already exists',
      })),
      url: 'https://project.test',
      key: 'secret',
    });

    assert.deepEqual(summary, { uploaded: 0, skipped: 1, failed: [] });
  });
});

test('skips a current Supabase ResourceAlreadyExists response', async () => {
  await withTempDirectory(async (rootDir) => {
    const existingPath = path.join(rootDir, 'pose-3.pdb');
    await writeFile(existingPath, 'ATOM 3\n');

    const summary = await uploadStructures({
      files: [{ absolutePath: existingPath, objectKey: 'data/A/pose-3.pdb' }],
      fetchImpl: async () => response(409, JSON.stringify({
        code: 'ResourceAlreadyExists',
        message: 'Object already exists',
      })),
      url: 'https://project.test',
      key: 'secret',
    });

    assert.deepEqual(summary, { uploaded: 0, skipped: 1, failed: [] });
  });
});

test('does not classify a misleading server error as an existing object', async () => {
  await withTempDirectory(async (rootDir) => {
    const brokenPath = path.join(rootDir, 'broken.pdb');
    await writeFile(brokenPath, 'ATOM 1\n');

    const diagnostics = [];
    const summary = await uploadStructures({
      files: [{ absolutePath: brokenPath, objectKey: 'data/A/broken.pdb' }],
      fetchImpl: async () => response(500, 'The resource already exists'),
      url: 'https://project.test',
      key: 'secret',
      onFailure: (detail) => diagnostics.push(detail),
    });

    assert.deepEqual(summary.failed, ['data/A/broken.pdb']);
    assert.deepEqual(diagnostics, [{
      objectKey: 'data/A/broken.pdb',
      status: 500,
      message: 'The resource already exists',
    }]);
    assert.equal(summary.skipped, 0);
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

    const diagnostics = [];
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
      onFailure: (detail) => diagnostics.push(detail),
    });

    assert.deepEqual(summary, {
      uploaded: 1,
      skipped: 0,
      failed: ['data/A/broken.pdb'],
    });
    assert.deepEqual(diagnostics, [{
      objectKey: 'data/A/broken.pdb',
      status: 500,
      message: 'storage unavailable',
    }]);
  });
});

test('encodes object path components and honors overwrite', async () => {
  await withTempDirectory(async (rootDir) => {
    const filePath = path.join(rootDir, 'special.pdb');
    await writeFile(filePath, 'ATOM 1\n');
    const requests = [];

    const summary = await uploadStructures({
      files: [{ absolutePath: filePath, objectKey: 'data/A/pose #1?.pdb' }],
      fetchImpl: async (url, options) => {
        requests.push({ url, ...options });
        return response(200);
      },
      url: 'https://project.test',
      key: 'secret',
      overwrite: true,
    });

    assert.deepEqual(summary, {
      uploaded: 1,
      skipped: 0,
      failed: [],
    });
    assert.equal(
      requests[0].url,
      'https://project.test/storage/v1/object/structures/data/A/pose%20%231%3F.pdb',
    );
    assert.equal(requests[0].headers['x-upsert'], 'true');
  });
});

test('returns a nonzero CLI outcome with failed upload diagnostics', async () => {
  await withTempDirectory(async (rootDir) => {
    await mkdir(path.join(rootDir, 'data/A'), { recursive: true });
    await writeFile(path.join(rootDir, 'data/A/broken.pdb'), 'ATOM 1\n');
    const messages = [];

    const exitCode = await runCli({
      env: {
        SUPABASE_URL: 'https://project.test',
        SUPABASE_SERVICE_ROLE_KEY: 'credential-that-must-not-log',
      },
      rootDir,
      fetchImpl: async (url, options = {}) => {
        if (options.method === 'GET') return response(200);
        return response(500, 'storage unavailable credential-that-must-not-log');
      },
      log: (message) => messages.push(message),
      error: (message) => messages.push(message),
    });

    assert.equal(exitCode, 1);
    assert.match(messages.join('\n'), /data\/A\/broken\.pdb \(500\): storage unavailable \[redacted\]/);
    assert.doesNotMatch(messages.join('\n'), /credential-that-must-not-log/);
  });
});

test('benchmark mode rejects a missing or empty benchmark demo directory', async () => {
  await withTempDirectory(async (rootDir) => {
    const messages = [];
    const common = {
      args: ['--benchmark'],
      rootDir,
      fetchImpl: async () => { throw new Error('must not contact Storage'); },
      log: (message) => messages.push(message),
      error: (message) => messages.push(message),
    };
    const credentials = {
      SUPABASE_URL: 'https://project.test',
      SUPABASE_SERVICE_ROLE_KEY: 'secret',
    };

    assert.equal(await runCli({ ...common, env: credentials }), 1);
    assert.match(messages.pop(), /BENCHMARK_DEMO_DIR is required/i);

    const benchmarkDir = path.join(rootDir, 'empty-benchmark');
    await mkdir(benchmarkDir);
    assert.equal(await runCli({
      ...common,
      env: { ...credentials, BENCHMARK_DEMO_DIR: benchmarkDir },
    }), 1);
    assert.match(messages.pop(), /no benchmark PDB\/CIF files found/i);
  });
});

test('benchmark mode uploads only benchmark demo objects', async () => {
  await withTempDirectory(async (rootDir) => {
    await mkdir(path.join(rootDir, 'data/A'), { recursive: true });
    await writeFile(path.join(rootDir, 'data/A/pose-1.pdb'), 'ATOM\n');
    const benchmarkDir = path.join(rootDir, 'external-benchmark', 'demo');
    await mkdir(path.join(benchmarkDir, 'systems/1ABC'), { recursive: true });
    await writeFile(path.join(benchmarkDir, 'systems/1ABC/pose.pdb'), 'ATOM\n');
    await writeFile(path.join(benchmarkDir, 'systems/1ABC/xtal.cif'), 'data_test\n');
    const uploaded = [];
    const messages = [];

    const exitCode = await runCli({
      env: {
        SUPABASE_URL: 'https://project.test',
        SUPABASE_SERVICE_ROLE_KEY: 'secret',
        BENCHMARK_DEMO_DIR: benchmarkDir,
      },
      args: ['--benchmark'],
      rootDir,
      fetchImpl: async (url, options = {}) => {
        if (options.method === 'POST' && url.includes('/object/')) {
          uploaded.push(url.split('/object/structures/')[1]);
        }
        return response(200);
      },
      log: (message) => messages.push(message),
      error: (message) => messages.push(message),
    });

    assert.equal(exitCode, 0);
    assert.deepEqual(uploaded.sort(), [
      'benchmark/demo/systems/1ABC/pose.pdb',
      'benchmark/demo/systems/1ABC/xtal.cif',
    ]);
    assert.match(messages.join('\n'), /Local: 2; uploaded: 2; skipped: 0; failed: 0/);
  });
});

test('the default structures mode still uploads the repository data trees', async () => {
  await withTempDirectory(async (rootDir) => {
    await mkdir(path.join(rootDir, 'data/A'), { recursive: true });
    await mkdir(path.join(rootDir, 'data_rnp/B'), { recursive: true });
    await writeFile(path.join(rootDir, 'data/A/pose-1.pdb'), 'ATOM\n');
    await writeFile(path.join(rootDir, 'data_rnp/B/protein.pdb'), 'ATOM\n');
    const benchmarkDir = path.join(rootDir, 'external-benchmark', 'demo');
    await mkdir(path.join(benchmarkDir, 'systems/1ABC'), { recursive: true });
    await writeFile(path.join(benchmarkDir, 'systems/1ABC/benchmark.pdb'), 'ATOM\n');
    const uploaded = [];

    const exitCode = await runCli({
      env: {
        SUPABASE_URL: 'https://project.test',
        SUPABASE_SERVICE_ROLE_KEY: 'secret',
        BENCHMARK_DEMO_DIR: benchmarkDir,
      },
      args: [],
      rootDir,
      fetchImpl: async (url, options = {}) => {
        if (options.method === 'POST' && url.includes('/object/')) {
          uploaded.push(url.split('/object/structures/')[1]);
        }
        return response(200);
      },
      log: () => {},
      error: () => {},
    });

    assert.equal(exitCode, 0);
    assert.deepEqual(uploaded.sort(), ['data/A/pose-1.pdb', 'data_rnp/B/protein.pdb']);
  });
});

test('upload:benchmark selects explicit benchmark mode', async () => {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.match(pkg.scripts['upload:benchmark'], /--benchmark/);
});

test('limits concurrent uploads to the requested bound', async () => {
  await withTempDirectory(async (rootDir) => {
    const files = await Promise.all(
      Array.from({ length: 5 }, async (_, index) => {
        const absolutePath = path.join(rootDir, `pose-${index}.pdb`);
        await writeFile(absolutePath, 'ATOM\n');
        return { absolutePath, objectKey: `data/A/pose-${index}.pdb` };
      }),
    );
    let active = 0;
    let maximumActive = 0;

    const summary = await uploadStructures({
      files,
      fetchImpl: async () => {
        active += 1;
        maximumActive = Math.max(maximumActive, active);
        await new Promise((resolve) => setTimeout(resolve, 5));
        active -= 1;
        return response(200);
      },
      url: 'https://project.test',
      key: 'secret',
      concurrency: 2,
    });

    assert.deepEqual(summary, { uploaded: 5, skipped: 0, failed: [] });
    assert.equal(maximumActive, 2);
  });
});
