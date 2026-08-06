import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveAssetUrl } from '../structure-assets.js';

test('prefixes relative structure paths with the Storage base URL', () => {
  assert.equal(
    resolveAssetUrl(
      'data/13NM/pose-1.pdb',
      'https://project.supabase.co/storage/v1/object/public/structures/',
    ),
    'https://project.supabase.co/storage/v1/object/public/structures/data/13NM/pose-1.pdb',
  );
});

test('leaves absolute URLs unchanged', () => {
  assert.equal(
    resolveAssetUrl('https://example.test/pose.pdb', 'https://storage.test'),
    'https://example.test/pose.pdb',
  );
});

test('uses local paths when Storage is unconfigured', () => {
  assert.equal(resolveAssetUrl('data/13NM/pose-1.pdb', ''), 'data/13NM/pose-1.pdb');
});
