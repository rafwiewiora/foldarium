import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, rename, rm, stat } from 'node:fs/promises';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const LEGACY_RELEASE = Object.freeze({
  archiveName: 'foldarium-legacy-public-v1.tar.gz',
  archiveRoot: 'foldarium-legacy-public-v1',
  sha256: 'b58bc1c0fd297c1a46fe85b7c28eedc19f1b93a8ec51ab189763d10e681f5484',
  url: 'https://github.com/rafwiewiora/foldarium-data/releases/download/'
    + 'legacy-public-v1/foldarium-legacy-public-v1.tar.gz',
});

export function parseArgs(args) {
  const options = {
    destination: path.resolve('legacy-data'),
    extract: true,
    force: false,
  };
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === '--destination') {
      const value = args[index + 1];
      if (!value || value.startsWith('--')) throw new Error('--destination requires a path');
      options.destination = path.resolve(value);
      index += 1;
    } else if (argument === '--no-extract') {
      options.extract = false;
    } else if (argument === '--force') {
      options.force = true;
    } else if (argument === '--help' || argument === '-h') {
      options.help = true;
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }
  return options;
}

export async function sha256File(filePath) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(filePath)) digest.update(chunk);
  return digest.digest('hex');
}

export async function verifyArchive(filePath, expected = LEGACY_RELEASE.sha256) {
  const actual = await sha256File(filePath);
  if (actual !== expected) {
    throw new Error(`Archive checksum mismatch: expected ${expected}, received ${actual}`);
  }
  return actual;
}

async function exists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

async function run(command, args, { capture = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: capture ? ['ignore', 'pipe', 'inherit'] : 'inherit',
    });
    const chunks = [];
    if (capture) child.stdout.on('data', chunk => chunks.push(chunk));
    child.once('error', reject);
    child.once('close', code => {
      if (code !== 0) {
        reject(new Error(`${command} exited with status ${code}`));
        return;
      }
      resolve(capture ? Buffer.concat(chunks).toString('utf8') : '');
    });
  });
}

export function validateArchiveListing(listing, expectedRoot = LEGACY_RELEASE.archiveRoot) {
  const prefix = `${expectedRoot}/`;
  const entries = listing.split('\n').filter(Boolean);
  if (!entries.length) throw new Error('Archive is empty');
  for (const entry of entries) {
    if (
      entry.startsWith('/')
      || entry.includes('\\')
      || entry.split('/').includes('..')
      || (entry !== expectedRoot && !entry.startsWith(prefix))
    ) {
      throw new Error(`Unsafe archive entry: ${entry}`);
    }
  }
  return entries;
}

export async function fetchLegacyData({
  destination,
  extract = true,
  force = false,
  fetchImpl = fetch,
  release = LEGACY_RELEASE,
  log = console.log,
}) {
  await mkdir(destination, { recursive: true });
  const archivePath = path.join(destination, release.archiveName);
  let validExistingArchive = false;
  if (await exists(archivePath)) {
    try {
      await verifyArchive(archivePath, release.sha256);
      validExistingArchive = true;
      log(`Verified existing ${release.archiveName}`);
    } catch (error) {
      if (!force) throw error;
    }
  }

  if (!validExistingArchive) {
    const temporaryPath = `${archivePath}.part-${process.pid}`;
    await rm(temporaryPath, { force: true });
    try {
      const response = await fetchImpl(release.url);
      if (!response.ok || !response.body) {
        throw new Error(`Download failed with HTTP ${response.status}`);
      }
      await pipeline(Readable.fromWeb(response.body), createWriteStream(temporaryPath, { flags: 'wx' }));
      await verifyArchive(temporaryPath, release.sha256);
      await rename(temporaryPath, archivePath);
      log(`Downloaded and verified ${release.archiveName}`);
    } finally {
      await rm(temporaryPath, { force: true });
    }
  }

  if (extract) {
    const extractedRoot = path.join(destination, release.archiveRoot);
    if (await exists(extractedRoot)) {
      if (!force) throw new Error(`Extraction target already exists: ${extractedRoot}`);
      await rm(extractedRoot, { recursive: true, force: true });
    }
    const listing = await run('tar', ['-tzf', archivePath], { capture: true });
    validateArchiveListing(listing, release.archiveRoot);
    await run('tar', ['-xzf', archivePath, '-C', destination]);
    log(`Extracted ${release.archiveRoot}`);
  }

  return { archivePath, extractedRoot: extract ? path.join(destination, release.archiveRoot) : '' };
}

function usage() {
  return [
    'Usage: npm run data:legacy -- [--destination PATH] [--no-extract] [--force]',
    '',
    'Downloads, verifies, and optionally extracts Foldarium legacy public data v1.',
  ].join('\n');
}

export async function runCli(args = process.argv.slice(2)) {
  const options = parseArgs(args);
  if (options.help) {
    console.log(usage());
    return;
  }
  await fetchLegacyData(options);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runCli().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
