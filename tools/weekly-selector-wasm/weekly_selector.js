import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));

let wasm;
try {
  wasm = await import('./pkg/weekly_selector_wasm.js');
} catch (error) {
  throw new Error(
    'weekly-selector-wasm pkg is not built; run wasm-pack build --target nodejs --out-dir pkg',
    { cause: error },
  );
}

export function loadParityFixture() {
  return JSON.parse(readFileSync(join(root, 'fixtures', 'parity.json'), 'utf8'));
}

export function validateSubmission(manifest, submission) {
  return JSON.parse(wasm.validateSubmission(JSON.stringify(manifest), JSON.stringify(submission)));
}

export function buildSubmission(manifest, submissionId, items) {
  return JSON.parse(
    wasm.buildSubmission(JSON.stringify(manifest), submissionId, JSON.stringify(items)),
  );
}

export function buildTemplate(manifest) {
  return JSON.parse(wasm.buildTemplate(JSON.stringify(manifest)));
}

export function digestSubmission(manifest, submission) {
  return wasm.digestSubmission(JSON.stringify(manifest), JSON.stringify(submission));
}
