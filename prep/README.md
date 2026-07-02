# Prep pipelines — how quiz items are built

Two independent pipelines produce the bucketed quiz items. Both end in the same schema
(`quiz_items*.json`): each item is one single-pocket ensemble with a list of anonymised, clustered poses
carrying per-pose RMSD-to-crystal, ligand-pLDDT, and method; plus `n_heavy`, `novel`, and the bucket.

## CAMEO 3D (prospective) — `cameo/`
Source: weekly CAMEO co-folding server (AF3, `server993`), last ~12 months.

**Filter funnel** (`cameo/funnel.py` — run it to reproduce the exact counts):

| stage | count | % |
|---|---|---|
| total CAMEO targets | 8,682 | 100% |
| **drug-like** target ligand (`cameo/drug_like_breakdown.py`, `process_cameo.select_target_het`) | 4,595 | 53% |
| **≥15 heavy atoms** (the quiz `HEAVY_MIN`) | **3,441** | **40%** |
| ≥3 scored AF3 models | 4,557 | 52% |
| oracle-correct (a <2 Å pose exists) | 2,126 | 24% |
| + a clearly-wrong pose ⇒ **game-able** candidates | 486 | 6% |
| crystal + pocket usable · ≥2 clusters · **single-pocket (<8 Å centroid spread)** | 285 | 3% |
| eligible game-able | 254 | 3% |

The two big "drops" aren't waste — `oracle − 4557` = the **all-wrong** bucket (no correct pose, the HARD
game); `oracle − game-able` = the **all-correct / trivial** bucket. Each bucket is built by its own script:
- `build_quiz_items.py` — game-able (a correct + a wrong pose)
- `archive_build_allwrong.py` — all-wrong (no correct pose)
- `archive_build_allcorrect.py` — all-correct (positive control)

Shared steps: `align_to_crystal.py` (superpose poses onto the crystal frame), `add_xtal_perpose.py` /
`add_af3_proteins.py` / `use_af3_protein.py` (crystal ref + AF3-protein toggle). Novelty:
`build_train_sim_quiz.py` / `build_sample_novelty.py` / `build_ligand_novelty.py` (Foldseek → pre-cutoff →
ligand shape overlap). `process_cameo.py` is the core (collect models, pick the drug-like het, per-model RMSD).

## Runs-n-Poses (retrospective) — `rnp/`
Source: the RnP Zenodo release (`prediction_files.tar.gz`, 5 methods) + `all_similarity_scores.parquet`.

Full-archive rebuild pipeline:
1. `rnp_matcher_v2.py` — map each scored ligand-instance-chain to its CIF pose chain by heavy-atom count.
2. `build_index_coords_v2.py` — one stream over the 39 GB tar (all 5 methods incl. boltz2) → index + coords.
3. `build_rnp_quiz_plan_v2.py` — pool **top-3 ranking poses per method**, align to an AF3 reference,
   greedy-cluster ligands (RMSD<2 Å), apply the **single-pocket** filter (centroid spread <8 Å).
4. `stream_ref_proteins_v2.py` + `extract_groundtruth_v2.py` → `build_rnp_quiz_assemble_v2.py` (write items).
5. `clean_tag_rnp_v2.py` — drop unaligned + mixed-compound poses; tag `n_heavy`.
6. `compute_novelty.py` + `fetch_rcsb_dates.py` + `tag_quiz.py` — novelty from the parquet
   (`sucos_shape_pocket_qcov < 25`, max over pre-2021-09-30 training targets; RCSB release dates give the
   cutoff). `rnp_v2_report.py` prints the v1-vs-v2 diff.

Analyses (not part of the build): `pocket_bias_analysis.py` (single- vs multi-pocket success),
`classify_dropped_74.py` (why novel systems drop out), `rule_search.py` / `analyze_plddt.py` /
`consensus.py` (pose-selection rules; ligand-pLDDT beats ranking_score as the pooled selector).

## Downstream (at play time, in `app/app.js`)
- **Strict thresholds**: correct <1.5 Å, wrong >3 Å (buckets re-derived per session).
- **`HEAVY_MIN = 15`**: drop small fragments.
- **`HARD_MIX = {game-able 0.40, all-wrong 0.45, all-correct 0.15}`**: class-balanced Hard draw so a
  constant strategy ("always none") scores ~chance, not the raw ~78% all-wrong base rate.
- **`ALLCORRECT_MAX_FRAC`**: cap the all-correct positive-control so it stays a sprinkle.
