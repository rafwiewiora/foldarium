const SHA256 = /^[0-9a-f]{64}$/;

function validateVariant(variant, { format, mediaType }) {
  if (!variant || typeof variant !== 'object') {
    throw new TypeError(`${format} structure variant is required`);
  }
  if (!SHA256.test(variant.sha256 || '')) {
    throw new TypeError(`${format} structure SHA-256 is invalid`);
  }
  const expectedSuffix = `/sha256/${variant.sha256.slice(0, 2)}/${variant.sha256}`;
  if (
    typeof variant.object_uri !== 'string'
    || !variant.object_uri.startsWith('supabase://')
    || !variant.object_uri.endsWith(expectedSuffix)
  ) {
    throw new TypeError(`${format} structure URI is not digest-bound`);
  }
  if (!Number.isInteger(variant.size_bytes) || variant.size_bytes <= 0) {
    throw new TypeError(`${format} structure size is invalid`);
  }
  if (variant.media_type !== mediaType) {
    throw new TypeError(`${format} structure media type is invalid`);
  }
  return variant;
}

export function validateDualFormatStructureAsset(asset) {
  if (asset?.schema_version !== 'foldarium.structure-asset/v1') {
    throw new TypeError('structure asset schema version is invalid');
  }
  const pdb = validateVariant(asset.pdb, {
    format: 'PDB',
    mediaType: 'chemical/x-pdb',
  });
  let bcif = null;
  if (asset.bcif) {
    bcif = validateVariant(asset.bcif, {
      format: 'BinaryCIF',
      mediaType: 'application/octet-stream',
    });
    if (
      asset.bcif.encoding !== 'BinaryCIF'
      || asset.bcif.source_pdb_sha256 !== pdb.sha256
    ) {
      throw new TypeError('BinaryCIF provenance is not bound to the PDB source');
    }
  }
  return { pdb, bcif };
}

export function structureLoadSpec(
  asset,
  {
    preferBinary = true,
    requiresPdbText = false,
  } = {},
) {
  const { pdb, bcif } = validateDualFormatStructureAsset(asset);
  if (preferBinary && bcif && !requiresPdbText) {
    return {
      objectUri: bcif.object_uri,
      format: 'mmcif',
      isBinary: true,
      sha256: bcif.sha256,
      sizeBytes: bcif.size_bytes,
    };
  }
  return {
    objectUri: pdb.object_uri,
    format: 'pdb',
    isBinary: false,
    sha256: pdb.sha256,
    sizeBytes: pdb.size_bytes,
  };
}
