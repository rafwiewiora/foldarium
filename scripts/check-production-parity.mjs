import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

export const STATIC_PARITY_TARGETS = Object.freeze([
  ['index.html', '/weekly'],
  ['app.js', '/app.js'],
  ['leaderboard.html', '/leaderboard.html'],
  ['weekly-retrospectives.html', '/weekly/retrospectives'],
  ['weekly-retrospectives.js', '/weekly-retrospectives.js'],
  ['weekly-retrospectives.css', '/weekly-retrospectives.css'],
]);

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function parseOrigin(args) {
  const index = args.indexOf('--origin');
  const value = index >= 0 ? args[index + 1] : args[0];
  if (!value || value.startsWith('--')) throw new Error('Pass --origin https://host.example');
  const origin = new URL(value);
  if (origin.protocol !== 'https:' || origin.username || origin.password) {
    throw new Error('Production origin must be an HTTPS URL without credentials');
  }
  return origin.origin;
}

export function validateProductionConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('Production config is not a JSON object');
  }
  if (config.deploymentEnvironment !== 'production') {
    throw new Error('Production config reports a non-production environment');
  }
  for (const key of ['url', 'publishableKey', 'structureBaseUrl']) {
    if (typeof config[key] !== 'string' || !config[key]) {
      throw new Error(`Production config is missing ${key}`);
    }
  }
  if (!config.enabled) throw new Error('Production config is disabled');
}

export async function checkProductionParity({
  origin,
  rootDirectory = process.cwd(),
  fetchImpl = fetch,
  targets = STATIC_PARITY_TARGETS,
  log = console.log,
}) {
  const mismatches = [];
  for (const [localPath, remotePath] of targets) {
    const [local, response] = await Promise.all([
      readFile(new URL(localPath, pathToFileURL(`${rootDirectory}/`))),
      fetchImpl(new URL(remotePath, origin)),
    ]);
    if (!response.ok) {
      mismatches.push(`${remotePath}: HTTP ${response.status}`);
      continue;
    }
    const remote = Buffer.from(await response.arrayBuffer());
    const localDigest = sha256(local);
    const remoteDigest = sha256(remote);
    if (localDigest !== remoteDigest) {
      mismatches.push(`${localPath}: local ${localDigest}, deployed ${remoteDigest}`);
    } else {
      log(`${localPath}: ${localDigest}`);
    }
  }

  const configResponse = await fetchImpl(new URL('/api/config', origin));
  if (!configResponse.ok) {
    mismatches.push(`/api/config: HTTP ${configResponse.status}`);
  } else {
    try {
      validateProductionConfig(await configResponse.json());
    } catch (error) {
      mismatches.push(`/api/config: ${error.message}`);
    }
  }

  if (mismatches.length) {
    throw new Error(`Production parity failed:\n- ${mismatches.join('\n- ')}`);
  }
  return { checked: targets.length };
}

export async function runCli(args = process.argv.slice(2)) {
  const origin = parseOrigin(args);
  await checkProductionParity({ origin });
  console.log(`Production parity passed for ${origin}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runCli().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
