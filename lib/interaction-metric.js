const UNAVAILABLE = Object.freeze({ status: 'unavailable', availability_policy: 'foldarium.prolif-availability/v1', reason: 'unsupported_receptor_residue', unsupported_residues: ['UNK'] });
export function projectInteractionCount(raw) {
  const fail = () => { throw new Error('Invalid interaction count'); };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) || !['prolif_hbond_residue_count', 'prolif_unique_residue_interaction_type'].includes(raw.metric) || typeof raw.policy !== 'string' || !raw.policy.trim()) fail();
  if (raw.value === null) {
    const keys = ['metric', 'value', 'policy', ...Object.keys(UNAVAILABLE)];
    if (Object.keys(raw).length !== keys.length || Object.keys(raw).some(k => !keys.includes(k)) || raw.metric !== 'prolif_hbond_residue_count' || raw.policy !== 'prolif-implicit-hbond-unique-protein-residue/v2' || Object.keys(UNAVAILABLE).some(k => JSON.stringify(raw[k]) !== JSON.stringify(UNAVAILABLE[k]))) fail();
    return { metric: raw.metric, value: null, policy: raw.policy, ...UNAVAILABLE, unsupported_residues: ['UNK'] };
  }
  if (!Number.isSafeInteger(raw.value) || raw.value < 0 || Object.keys(raw).some(k => !['metric', 'value', 'policy'].includes(k))) fail();
  return { metric: raw.metric, policy: raw.policy, value: raw.value };
}
