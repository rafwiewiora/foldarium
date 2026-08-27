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

test('resolves a public content-addressed Supabase object URI', () => {
  assert.equal(
    resolveAssetUrl(
      'supabase://foldarium-quiz-public/sha256/ab/file name.pdb',
      '',
      'https://project.supabase.co/',
    ),
    'https://project.supabase.co/storage/v1/object/public/foldarium-quiz-public/sha256/ab/file%20name.pdb',
  );
});

test('does not turn an unsafe Supabase object URI into a public URL', () => {
  assert.equal(
    resolveAssetUrl(
      'supabase://foldarium-quiz-public/../secret',
      '',
      'https://project.supabase.co',
    ),
    'supabase://foldarium-quiz-public/../secret',
  );
});
