import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  fetchLegacyData,
  parseArgs,
  validateArchiveListing,
  verifyArchive,
} from '../scripts/fetch-legacy-data.mjs';

async function withTempDirectory(t) {
  const directory = await mkdtemp(path.join(tmpdir(), 'foldarium-legacy-data-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

test('parses downloader options without hidden defaults', () => {
  const options = parseArgs(['--destination', './fixtures', '--no-extract', '--force']);
  assert.equal(options.destination, path.resolve('./fixtures'));
  assert.equal(options.extract, false);
  assert.equal(options.force, true);
  assert.throws(() => parseArgs(['--destination']), /requires a path/);
  assert.throws(() => parseArgs(['--unknown']), /Unknown argument/);
});

test('rejects archive listings that can escape the destination', () => {
  assert.deepEqual(
    validateArchiveListing('foldarium-legacy-public-v1/\nfoldarium-legacy-public-v1/app/data.json\n'),
    ['foldarium-legacy-public-v1/', 'foldarium-legacy-public-v1/app/data.json'],
  );
  for (const listing of [
    '../outside',
    '/absolute/path',
    'foldarium-legacy-public-v1/../../outside',
    'another-root/file',
  ]) {
    assert.throws(() => validateArchiveListing(listing), /Unsafe archive entry/);
  }
});

test('downloads and verifies the immutable archive before retaining it', async t => {
  const destination = await withTempDirectory(t);
  const payload = Buffer.from('synthetic archive bytes');
  const sha256 = createHash('sha256').update(payload).digest('hex');
  const release = {
    archiveName: 'fixture.tar.gz',
    archiveRoot: 'fixture',
    sha256,
    url: 'https://example.test/fixture.tar.gz',
  };
  let requests = 0;
  const result = await fetchLegacyData({
    destination,
    extract: false,
    release,
    log() {},
    fetchImpl: async url => {
      requests += 1;
      assert.equal(url, release.url);
      return new Response(payload);
    },
  });
  assert.equal(requests, 1);
  assert.deepEqual(await readFile(result.archivePath), payload);
  assert.equal(await verifyArchive(result.archivePath, sha256), sha256);

  await fetchLegacyData({
    destination,
    extract: false,
    release,
    log() {},
    fetchImpl: async () => {
      throw new Error('should not download a verified archive twice');
    },
  });
});

test('fails closed on an existing archive with the wrong checksum', async t => {
  const destination = await withTempDirectory(t);
  await writeFile(path.join(destination, 'fixture.tar.gz'), 'wrong bytes');
  await assert.rejects(
    fetchLegacyData({
      destination,
      extract: false,
      release: {
        archiveName: 'fixture.tar.gz',
        archiveRoot: 'fixture',
        sha256: '0'.repeat(64),
        url: 'https://example.test/fixture.tar.gz',
      },
      log() {},
      fetchImpl: async () => new Response('replacement'),
    }),
    /checksum mismatch/,
  );
});
