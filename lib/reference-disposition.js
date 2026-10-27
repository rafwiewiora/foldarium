// A missing score is never an incorrect answer. Only this audited, versioned
// released-reference disposition permits null metrics in a revealed item.
export const REFERENCE_DISPOSITION_POLICY = 'foldarium.released-reference-disposition/v1';
export const REFERENCE_BRIDGE_DISPOSITION_POLICY = 'foldarium.released-reference-disposition/v2';
const FIELDS = [
  'policy', 'code', 'component_id', 'expected_heavy_atoms', 'observed_heavy_atoms',
  'explicitly_unobserved_heavy_atoms', 'reference_coverage',
  'minimum_reference_coverage', 'reference_sha256',
].sort();
const BRIDGE_FIELDS = [...FIELDS, 'missing_heavy_atom_ids', 'observed_component_count',
  'missing_component_boundary_counts', 'ccd_heavy_atom_graph_sha256'].sort();
const PRIVATE_DIGESTS = new Set(['reference_sha256', 'ccd_heavy_atom_graph_sha256']);

function validBridgeProof(d, publicProjection) {
  const atoms = d.missing_heavy_atom_ids;
  const boundaries = d.missing_component_boundary_counts;
  return Array.isArray(atoms) && atoms.length === d.explicitly_unobserved_heavy_atoms
    && atoms.every((atom, index) => typeof atom === 'string' && atom.trim() === atom && atom.length > 0
      && (index === 0 || atoms[index - 1] < atom))
    && Number.isSafeInteger(d.observed_component_count) && d.observed_component_count >= 2
    && d.observed_component_count <= d.observed_heavy_atoms
    && Array.isArray(boundaries) && boundaries.length > 0 && boundaries.length <= atoms.length
    && boundaries.every((count, index) => Number.isSafeInteger(count) && count >= 1
      && count <= d.observed_component_count && (index === 0 || boundaries[index - 1] <= count))
    && boundaries.some(count => count >= 2)
    && (publicProjection || (typeof d.ccd_heavy_atom_graph_sha256 === 'string'
      && /^[0-9a-f]{64}$/.test(d.ccd_heavy_atom_graph_sha256)));
}

export function isUnscorableReference(item, { publicProjection = false, requireChoices = true } = {}) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) {
    throw new Error('invalid reveal item');
  }
  if (item.evaluation_status == null || item.evaluation_status === 'scored') {
    if (item.reference_disposition != null) throw new Error('scored item has reference disposition');
    return false;
  }
  if (item.evaluation_status !== 'unscorable') throw new Error('unknown evaluation status');
  const d = item.reference_disposition;
  const bridge = d?.policy === REFERENCE_BRIDGE_DISPOSITION_POLICY && d?.code === 'missing_reference_bridge';
  const fields = bridge ? BRIDGE_FIELDS : FIELDS;
  if (!d || typeof d !== 'object' || Array.isArray(d)
    || JSON.stringify(Object.keys(d).sort()) !== JSON.stringify(publicProjection ? fields.filter(key => !PRIVATE_DIGESTS.has(key)) : fields)
    || (!bridge && (d.policy !== REFERENCE_DISPOSITION_POLICY || d.code !== 'insufficient_reference_coverage'))
    || typeof d.component_id !== 'string' || !d.component_id.trim()
    || !Number.isSafeInteger(d.expected_heavy_atoms) || d.expected_heavy_atoms <= 0
    || !Number.isSafeInteger(d.observed_heavy_atoms) || d.observed_heavy_atoms <= 0
    || !Number.isSafeInteger(d.explicitly_unobserved_heavy_atoms)
    || d.explicitly_unobserved_heavy_atoms <= 0
    || d.observed_heavy_atoms + d.explicitly_unobserved_heavy_atoms !== d.expected_heavy_atoms
    || d.minimum_reference_coverage !== 0.8
    || !Number.isFinite(d.reference_coverage)
    || d.reference_coverage !== d.observed_heavy_atoms / d.expected_heavy_atoms
    || (bridge ? d.reference_coverage < d.minimum_reference_coverage || d.reference_coverage >= 1
      || !validBridgeProof(d, publicProjection) : d.reference_coverage >= d.minimum_reference_coverage)
    || (!publicProjection && !/^[0-9a-f]{64}$/.test(d.reference_sha256))) {
    throw new Error('invalid reference disposition');
  }
  if (!requireChoices) return true;
  if (!Array.isArray(item.choices) || !item.choices.length) throw new Error('unscorable item has no choices');
  for (const choice of item.choices) {
    if (!choice || choice.rmsd !== null || choice.correct !== null || choice.accepted_correct !== null
      || (!publicProjection && choice.reference_sha256 !== d.reference_sha256)) {
      throw new Error('unscorable choice metrics or reference binding are invalid');
    }
  }
  return true;
}

export function projectReferenceDisposition(item) {
  if (!isUnscorableReference(item)) return {};
  const { reference_sha256: _digest, ccd_heavy_atom_graph_sha256: _graphDigest, ...disposition } = item.reference_disposition;
  return { evaluation_status: 'unscorable', reference_disposition: disposition };
}

export function referencePopulation(items) {
  const excluded = items.filter(item => isUnscorableReference(item)).length;
  return { item_count: items.length, scorable_item_count: items.length - excluded, excluded_item_count: excluded };
}
