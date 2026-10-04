// A missing score is never an incorrect answer. Only this audited, versioned
// released-reference disposition permits null metrics in a revealed item.
export const REFERENCE_DISPOSITION_POLICY = 'foldarium.released-reference-disposition/v1';
const FIELDS = [
  'policy', 'code', 'component_id', 'expected_heavy_atoms', 'observed_heavy_atoms',
  'explicitly_unobserved_heavy_atoms', 'reference_coverage',
  'minimum_reference_coverage', 'reference_sha256',
].sort();

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
  if (!d || typeof d !== 'object' || Array.isArray(d)
    || JSON.stringify(Object.keys(d).sort()) !== JSON.stringify(publicProjection ? FIELDS.filter(key => key !== 'reference_sha256') : FIELDS)
    || d.policy !== REFERENCE_DISPOSITION_POLICY
    || d.code !== 'insufficient_reference_coverage'
    || typeof d.component_id !== 'string' || !d.component_id.trim()
    || !Number.isSafeInteger(d.expected_heavy_atoms) || d.expected_heavy_atoms <= 0
    || !Number.isSafeInteger(d.observed_heavy_atoms) || d.observed_heavy_atoms <= 0
    || !Number.isSafeInteger(d.explicitly_unobserved_heavy_atoms)
    || d.explicitly_unobserved_heavy_atoms <= 0
    || d.observed_heavy_atoms + d.explicitly_unobserved_heavy_atoms !== d.expected_heavy_atoms
    || d.minimum_reference_coverage !== 0.8
    || !Number.isFinite(d.reference_coverage)
    || d.reference_coverage !== d.observed_heavy_atoms / d.expected_heavy_atoms
    || d.reference_coverage >= d.minimum_reference_coverage
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
  const { reference_sha256: _digest, ...disposition } = item.reference_disposition;
  return { evaluation_status: 'unscorable', reference_disposition: disposition };
}

export function referencePopulation(items) {
  const excluded = items.filter(item => isUnscorableReference(item)).length;
  return { item_count: items.length, scorable_item_count: items.length - excluded, excluded_item_count: excluded };
}
