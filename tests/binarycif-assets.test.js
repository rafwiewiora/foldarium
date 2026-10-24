import test from 'node:test';
import assert from 'node:assert/strict';
import {
  structureLoadSpec,
  validateDualFormatStructureAsset,
} from '../binarycif-assets.js';

const pdbSha = 'a'.repeat(64);
const bcifSha = 'b'.repeat(64);
const asset = {
  schema_version: 'foldarium.structure-asset/v1',
  pdb: {
    object_uri: `supabase://foldarium-weekly-quiz/sha256/aa/${pdbSha}`,
    sha256: pdbSha,
    size_bytes: 483_484,
    media_type: 'chemical/x-pdb',
  },
  bcif: {
    object_uri: `supabase://foldarium-weekly-quiz/sha256/bb/${bcifSha}`,
    sha256: bcifSha,
    size_bytes: 63_558,
    media_type: 'application/octet-stream',
    encoding: 'BinaryCIF',
    source_pdb_sha256: pdbSha,
  },
};

test('selects BinaryCIF with the Molstar binary mmCIF load contract', () => {
  assert.deepEqual(structureLoadSpec(asset), {
    objectUri: asset.bcif.object_uri,
    format: 'mmcif',
    isBinary: true,
    sha256: bcifSha,
    sizeBytes: 63_558,
  });
});

test('keeps PDB fallback for text-dependent H-bond assembly', () => {
  assert.deepEqual(structureLoadSpec(asset, { requiresPdbText: true }), {
    objectUri: asset.pdb.object_uri,
    format: 'pdb',
    isBinary: false,
    sha256: pdbSha,
    sizeBytes: 483_484,
  });
});

test('accepts future assets that have only the mandatory PDB variant', () => {
  const pdbOnly = { schema_version: asset.schema_version, pdb: asset.pdb };
  assert.equal(structureLoadSpec(pdbOnly).objectUri, asset.pdb.object_uri);
});

test('rejects BinaryCIF without digest-bound PDB provenance', () => {
  assert.throws(
    () => validateDualFormatStructureAsset({
      ...asset,
      bcif: { ...asset.bcif, source_pdb_sha256: 'c'.repeat(64) },
    }),
    /provenance/,
  );
  assert.throws(
    () => validateDualFormatStructureAsset({
      ...asset,
      bcif: { ...asset.bcif, object_uri: 'supabase://bucket/not-content-addressed' },
    }),
    /digest-bound/,
  );
});
