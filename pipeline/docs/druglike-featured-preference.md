# Chemistry preference for featured questions

`foldarium.medchem-presentation-preference/v1` is an explicit presentation heuristic
for medicinal-chemistry-style quiz ligands. It is not an activity, efficacy,
safety, or clinical-drug classifier. It does not change the full prediction or
benchmark population, native coordinates, stored votes, or historical intake.

The old intake policy removes a checked-in set of crystallographic artifacts,
metals and disconnected ligands, then accepts a minimum heavy-atom count. That
is not a complete drug-likeness assessment. A uniform five from that population
can contain lipid probes, highly polar metabolites and perfluoroalkyl acids.

The new helper reads only immutable source ligand SMILES/component identities
from the private index. It computes descriptors with pinned RDKit **2025.03.6**.
It never reads crystal answers, pose correctness, confidence, affinity or votes.
A changed recorded SMILES hash or public/private component mismatch fails closed.
Missing legacy chemistry and invalid SMILES remain explicitly unavailable.

## Preferred pool

A preferred ligand must satisfy all these versioned conditions:

- At least one ring and QED at least 0.5.
- Molecular weight 150–600, topological polar surface area at most 140 Å².
- At most ten rotatable bonds, ten hydrogen-bond acceptors and five donors.
- At most one Rule-of-Five violation (MW >500, logP >5, HBA >10, HBD >5).
- Fewer than four amide bonds and no chain of ten acyclic saturated carbon atoms.
- One connected molecule; not one of the exact checked-in metabolite/artifact
  component identities that are deprioritized for this presentation.

These cutoffs are an operational preference, not published universal boundaries.
QED uses drug-property distributions, but QED alone is insufficient here: a
perfluoroalkyl acid can score above 0.8, and free tryptophan above 0.7. The ring
and exact-identity conditions prevent those examples from displacing typical
medicinal-chemistry scaffolds. Exact component identities avoid a broad
purine/sugar substructure prohibition that would also remove legitimate drugs.
The source for the descriptor's meaning is the
[official RDKit QED documentation](https://rdkit.org/docs/source/rdkit.Chem.QED.html).

## Draw, fallback, and provenance

`assess_druglike_candidates(blind_manifest, private_index)` returns the exact
eligible item IDs plus a private evidence audit: policy, pinned library version,
complete rules, per-item descriptors/reasons, raw-SMILES hashes, source-manifest
hashes and an evidence digest. Its inputs are not modified.

The existing seeded uniform question draw selects up to five from this pool.
It does not rank the whole population by QED. If only one to four preferred items
exist, those form the smaller assignment; unwanted ligands are not added merely
to reach five. With zero preferred items, the audit explicitly records
`legacy-fallback-no-preferred` and returns the original full population so normal
weekly operation can continue. Such a fallback must not be described as a
chemistry-preferred assignment.

The freezer adds the chemistry audit to its existing private selection audit
and recomputes the selection digest before registering it. The public marker's
existing whitelist remains unchanged. Pre-existing frozen selections remain
immutable and must be returned idempotently without recomputing new chemistry.
A separate optional browser view must not overwrite an already frozen cohort.
