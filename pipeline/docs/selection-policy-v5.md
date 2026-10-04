# Opt-in hydrogen-aware ligand selection

`cameo-drug-like/v5` fixes the intake heavy-atom count for explicit hydrogen
atoms in SMILES. The dependency-free lexer reads the bracket element before
isotope, charge, or atom mapping: `[H]`, `[2H]`, and `[H:1]` contribute zero heavy
atoms; `[He]`, `[Hg]`, and `[Hf]` each contribute one. Attached hydrogen annotations
such as `[NH4+]` and `[13CH3]` still describe one heavy atom. The counter remains a
lexer, not a chemical validity validator; downstream RDKit graph validation is
unchanged. Metal, disconnected ligand, artifact-component, and TEP preference
rules remain unchanged.

The library default remains `cameo-drug-like/v4`.
Unstamped historical targets also mean v4. Existing v4 bracket-hydrogen counts,
token estimates, target/task identities, provenance, and scientifically audited
explicit-H compatibility remain reproducible. A v4 record cannot be relabeled v5:
staging, publication, reveal, private evaluation, and historical recovery use the
frozen target or item policy and revalidate counts. A stage containing mixed v4/v5
policies is rejected before prediction artifacts are downloaded. Unknown policy
values fail closed, including token sizing.

For newly registered targets, v5 can reject a ligand previously overcounted across
the 15-heavy-atom threshold, select a different largest eligible ligand, and change
its resource estimate. These are intended selection differences and produce new
immutable target/task provenance. Full scientific populations must retain their
actual selection-policy version when compared across releases.

## Future activation recommendation

Enable v5 for the next **unregistered** weekly intake as a correctness fix. Do not
rewrite historical campaigns, frozen manifests, selector kits, or evaluations, and
do not change the global default alias to retroactively reinterpret old tasks.
The hook already returns `already-registered` for an existing registered campaign.
This source integration does not activate a new policy.

An intake adapter can explicitly pass `selection_policy_version="cameo-drug-like/v5"`
for a new campaign after review. Record the exact policy in immutable target
provenance and preserve it through sizing, staging, publication, and evaluation.
Existing registered campaigns and unstamped replay targets remain v4. Hosting
profiles and activation settings are intentionally outside this public source.

## Verification

- RDKit confirms the actual counts for the explicit/isotopic/mapped hydrogen and
  He/Hg/Hf examples above.
- A deterministic default-v4 fixture produces identical serialized plan bytes to
  the pre-change source revision, including all target and task IDs.
- End-to-end v5 staging, publication, private reveal validation, and frozen legacy
  recovery are tested; malformed policies/counts and mixed-policy staging fail.
- A scoped read-only audit of the previously affected released week validated all
  37 items and 370 choices without modifying its source private index or blind
  manifest. All v5 counts matched RDKit; the one historical overcount changed
  from 36 to 35 and remained eligible. This recomputation is evidence for future
  intake, not a mutation of the historical v4 record.
- Portable selection/intake tests run without optional chemistry dependencies;
  the complete scientific test suite runs with RDKit, gemmi, and NumPy installed.
