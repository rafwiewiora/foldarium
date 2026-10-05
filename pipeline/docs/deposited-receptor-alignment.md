# Sparse deposited receptor alignment

Prepared scientific evaluator change; existing stored evaluations are not rewritten.
No prediction, ligand or receptor coordinates are repaired, generated or discarded.

The ordinary released-pose evaluator still uses its existing observed-sequence
similarity threshold and alignment, with
`foldarium-receptor-aligned-symmetry-rmsd/v4` provenance. Its normal result bytes
remain unchanged. The global threshold is not lowered.

A separate conditional path handles a deposited full-length protein with only a
small resolved region. For example, 192 observed residues from an explicitly
declared 893-residue entity can fail a whole-sequence length-normalized similarity
filter despite exact residue correspondence. The fallback requires all of:

- The complete predicted receptor sequence equals the deposited full entity
  sequence, without unknown residue identities; predicted `label_seq_id` values
  are the exact complete consecutive sequence.
- The released label chain maps to that entity in `_struct_asym`, and every
  `_entity_poly_seq` position and residue identity agrees with the parsed entity.
- Every observed reference residue has a unique in-range label position and the
  expected identity at that position.
- The deposited unobserved/zero-occupancy residue ledger accounts for every
  missing position for the same label chain, author chain and selected first model
  (which must be deposited model number one), with matching residue identities
  and no overlap with observed residues.
- At least five unambiguous observed C-alpha pairs have finite coordinates and
  geometry sufficient to determine alignment. Missing coordinates are never
  imputed, and ligand coordinates are never fitted separately.

Only that conditional result uses
`foldarium-receptor-aligned-symmetry-rmsd/v5` and
`deposited-full-entity-label-seq/v1`. It records the model/entity/chain identities,
full-sequence and paired-position digests, and expected, observed, explicitly
missing and aligned-C-alpha counts. The original observed-sequence similarity
remains reported. Ligand graph mapping, RMSD correctness thresholds, alternative
reference copies, population membership and choice identities stay unchanged.
Bad poses still receive their measured score.

The audit is retained in the private evaluation and released result, with explicit
version/policy/count validation. Blind manifest metadata and all selector-kit boundaries reject these fields,
including nested target metadata and the bundled standalone client. Newly built
kits include the hardened client; existing frozen kit bytes are not rewritten.
Historical public research retains each choice's evaluator version; detailed proof
stays bound to the private evaluation artifact. Existing artifact format validators
support the resulting sorted mixed evaluator-version list through their existing
content/lineage checks.

Malformed or incomplete deposition metadata, conflicting labels/identity,
ambiguous C-alpha alternatives, insufficient observations or non-finite alignment
still fail closed. A sequence fragment alone is insufficient proof. This is a
narrow released-reference compatibility policy, not a generic fragment scorer.
