export const RCSB_DOWNLOAD_ORIGIN = 'https://files.rcsb.org/download';
export const RCSB_STRUCTURE_ORIGIN = 'https://www.rcsb.org/structure';
const PDB_ID = /^[A-Za-z0-9]{4}$/;

export function rcsbReferenceUrl(pdbId) {
  if (typeof pdbId !== 'string' || !PDB_ID.test(pdbId)) {
    throw new Error('PDB ID is invalid');
  }
  return `${RCSB_DOWNLOAD_ORIGIN}/${pdbId.toUpperCase()}.cif.gz`;
}

export function rcsbUncompressedCifUrl(pdbId) {
  if (typeof pdbId !== 'string' || !PDB_ID.test(pdbId)) {
    throw new Error('PDB ID is invalid');
  }
  return `${RCSB_DOWNLOAD_ORIGIN}/${pdbId.toUpperCase()}.cif`;
}

export function rcsbStructurePageUrl(pdbId) {
  if (typeof pdbId !== 'string' || !PDB_ID.test(pdbId)) {
    throw new Error('PDB ID is invalid');
  }
  return `${RCSB_STRUCTURE_ORIGIN}/${pdbId.toUpperCase()}`;
}

export function parseTrustedRcsbReferenceUri(referenceUri) {
  if (typeof referenceUri !== 'string' || !referenceUri) {
    throw new Error('reference_uri must be non-empty text');
  }
  let parsed;
  try {
    parsed = new URL(referenceUri);
  } catch {
    throw new Error('reference_uri is not a valid URL');
  }
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'files.rcsb.org') {
    throw new Error('reference_uri host is not allow-listed');
  }
  const match = /^\/download\/([A-Za-z0-9]{4})\.cif\.gz$/.exec(parsed.pathname);
  if (!match) throw new Error('reference_uri path is not a canonical RCSB mmCIF download');
  const pdbId = match[1].toUpperCase();
  if (parsed.search || parsed.hash) {
    throw new Error('reference_uri must not include query or fragment parameters');
  }
  return { pdbId, referenceUri: rcsbReferenceUrl(pdbId) };
}

export function blindLigandComponentId(ligand) {
  if (typeof ligand === 'string' && ligand) return ligand;
  if (ligand && typeof ligand === 'object' && !Array.isArray(ligand)) {
    const componentId = ligand.component_id || ligand.name;
    if (typeof componentId === 'string' && componentId) return componentId;
  }
  throw new Error('blind ligand component identity is missing');
}

export function deriveReleasedCrystalForItem({
  itemId,
  ligand,
  revealChoices,
}) {
  if (typeof itemId !== 'string' || !PDB_ID.test(itemId)) {
    throw new Error(`item ${itemId || '(unknown)'} is not a classic PDB target id`);
  }
  const ligandComponentId = blindLigandComponentId(ligand);
  if (!Array.isArray(revealChoices) || !revealChoices.length) {
    throw new Error(`item ${itemId} has no reveal choices`);
  }

  let expectedReference = null;
  for (const choice of revealChoices) {
    const referenceUri = choice?.reference_uri;
    if (typeof referenceUri !== 'string' || !referenceUri) {
      throw new Error(`item ${itemId}/${choice?.id || '(unknown)'} is missing reference_uri`);
    }
    const parsed = parseTrustedRcsbReferenceUri(referenceUri);
    if (parsed.pdbId !== itemId.toUpperCase()) {
      throw new Error(`item ${itemId} reference_uri does not match the blind target id`);
    }
    const canonical = rcsbReferenceUrl(itemId);
    if (referenceUri !== canonical && referenceUri !== parsed.referenceUri) {
      throw new Error(`item ${itemId} reference_uri is not the canonical released RCSB coordinate`);
    }
    if (!expectedReference) expectedReference = parsed.referenceUri;
    else if (referenceUri !== expectedReference) {
      throw new Error(`item ${itemId} reveal choices disagree on reference_uri`);
    }
  }

  const pdbId = itemId.toUpperCase();
  return Object.freeze({
    pdb_id: pdbId,
    cif_url: rcsbUncompressedCifUrl(pdbId),
    cif_gz_url: rcsbReferenceUrl(pdbId),
    structure_page_url: rcsbStructurePageUrl(pdbId),
    ligand_component_id: ligandComponentId,
  });
}

export function enrichPrivateWeeklyPool(pool, bundle) {
  if (!Array.isArray(pool)) throw new Error('Private evaluation pool is incomplete.');
  const blindItems = new Map((bundle?.blind_manifest?.items || []).map(item => [item.id, item]));
  const revealItems = new Map((bundle?.reveal_manifest?.items || []).map(item => [item.id, item]));
  const answerOverlays = new Map((bundle?.answer_overlays || []).map(row => [row.item_id, row]));
  return pool.map(item => {
    const blindItem = blindItems.get(item.id);
    const revealItem = revealItems.get(item.id);
    const answerOverlay = answerOverlays.get(item.id);
    if (!blindItem || !revealItem || !answerOverlay) {
      throw new Error(`Normalized pool item ${item.id} is not in the bundle.`);
    }
    const releasedCrystal = deriveReleasedCrystalForItem({
      itemId: item.id,
      ligand: blindItem.ligand,
      revealChoices: revealItem.choices,
    });
    const posesById = new Map((answerOverlay.poses || []).map(pose => [pose.id, pose]));
    const choices = item.choices.map(choice => {
      const choiceId = choice._weeklyChoiceId || choice.id;
      const pose = posesById.get(choiceId);
      if (!pose) throw new Error(`Private evaluation choice ${item.id}/${choiceId} has no overlay.`);
      return {
        ...choice,
        answer_overlay_pdb: pose.predicted_pose_pdb,
        answer_crystal_pdb: pose.crystal_ligand_pdb,
        answer_crystal_pocket_pdb: pose.crystal_pocket_pdb,
      };
    });
    return {
      ...item,
      choices,
      released_crystal: releasedCrystal,
      answer_overlay: answerOverlay,
    };
  });
}
