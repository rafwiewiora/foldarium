import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const forbiddenPaths = new Set([
  '.vercelignore',
  'vercel.json',
  'pipeline/SMOKE_TEST_HANDOFF.md',
]);

const forbiddenContent = [
  ['live Supabase project reference', ['wwentnog', 'bknrbmxhfgbg'].join('')],
  [
    'live Supabase publishable key',
    ['sb_publishable_', 'JvyIZVDB2l6t7zIRpBBo7Q_FdHdD36v'].join(''),
  ],
  ['retired browser gate password', ['deploy', 'quickly123'].join('')],
  ['private organization reference', ['Junction', 'Bioscience'].join('')],
  ['hosting-provider environment variable', ['VERCEL', '_ENV'].join('')],
  ['hosting-provider commit variable', ['VERCEL', '_GIT_COMMIT_SHA'].join('')],
];

const files = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' })
  .split('\0')
  .filter(Boolean);
const failures = [];

for (const path of files) {
  if (forbiddenPaths.has(path) || path.startsWith('pipeline/deploy/')) {
    failures.push(`${path}: deployment-specific path`);
    continue;
  }

  let content;
  try {
    content = readFileSync(path, 'utf8');
  } catch {
    continue;
  }
  if (content.includes('\u0000')) continue;

  for (const [label, value] of forbiddenContent) {
    if (content.includes(value)) failures.push(`${path}: ${label}`);
  }
  if (/\/Users\/[A-Za-z0-9._-]+\//.test(content)) {
    failures.push(`${path}: absolute user path`);
  }
  if (/(?:^|\n)\s*(?:import\s+modal\b|from\s+modal\s+import\b)/.test(content)) {
    failures.push(`${path}: deployment-provider SDK import`);
  }
}

if (failures.length) {
  console.error('Public-tree audit failed:');
  for (const failure of failures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log(`Public-tree audit passed (${files.length} tracked files).`);
}
