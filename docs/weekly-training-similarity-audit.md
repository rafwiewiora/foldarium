# Weekly training-similarity audit

## Result

The post-reveal audit covers 100 published targets. It classified 39 as familiar, 58 as novel, and 3 as unknown.

| Blind estimator | Classified pairs | Classification accuracy | AUROC | Correct pose pick | Pose/None accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| Nearest training system | 80 | 77.5% | 0.7539 | 42.5% | 55.0% |
| Top-25 pocket-aware | 80 | 63.7% | 0.6628 | 45.0% | 61.3% |

Percentile confidence intervals in the JSON report use 2,000 deterministic target-level bootstrap samples. The fixed historical 0.25 overlap threshold was not calibrated on these Weekly targets.

## Scientific contract

- Foldseek `pdb100` / `3diaa`, with release date strictly before 2021-09-30.
- At most the first 25 retained structural neighbors.
- At least four Foldseek-aligned Cα atoms within 8 Å of the query pocket.
- Pocket-local Cα RMSD at most 3 Å.
- Familiar when maximum carried-ligand vdW-volume overlap is at least 0.25; novel below 0.25.
- Search, download, parse, or incomplete-candidate failures are unknown rather than novel.

The exact label is retrospective: it uses the released RCSB crystal and crystal ligand. The blind estimates use only the archived predicted receptor, predicted pocket, and candidate poses. Reveal manifests, crystal structures, answer overlays, and answer RMSDs are not accepted by the blind scorer.

## Weekly counts

| Week | Targets | Familiar | Novel | Unknown |
| --- | ---: | ---: | ---: | ---: |
| 2026-08-08 | 29 | 11 | 18 | 0 |
| 2026-08-15 | 32 | 15 | 16 | 1 |
| 2026-08-22 | 39 | 13 | 24 | 2 |

## Observed Weekly outcomes

| Exact class | Targets | Correct pose available |
| --- | ---: | ---: |
| Familiar | 39 | 30 (76.9%) |
| Novel | 58 | 25 (43.1%) |
| All | 97 | 55 (56.7%) |

| Automated participant | Overall | Familiar | Novel |
| --- | ---: | ---: | ---: |
| Claude Opus | 46.1% | 65.7% | 33.3% |
| Codex GPT-5.6 | 23.8% | 57.1% | 7.1% |
| GPT-5.6 Sol | 50.0% | 64.3% | 40.0% |
| Ligand pLDDT | 39.2% | 56.4% | 27.6% |
| Smina | 36.1% | 53.8% | 24.1% |

## Representative nearest training ligands

| Target | Week | Class | Training PDB | Ligand | Identity | Local RMSD (Å) | Overlap |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| 9SVM | 2026-08-22 | familiar | 6XG4 | TOP | 0.407 | 0.382 | 0.8736 |
| 9XDX | 2026-08-22 | familiar | 2A3E | AMI | 0.201 | 0.601 | 0.8075 |
| 9S2W | 2026-08-08 | familiar | 6BZC | G6P | 0.528 | 0.192 | 0.7797 |
| 9PXD | 2026-08-22 | familiar | 6CYV | DHF | 0.949 | 0.701 | 0.7653 |
| 9ZRD | 2026-08-08 | familiar | 6R0G | APR | 0.535 | 0.474 | 0.7297 |
| 32HC | 2026-08-22 | familiar | 3VFQ | AR6 | 0.309 | 1.5 | 0.6887 |
| 36JE | 2026-08-15 | familiar | 6U6A | Q0J | 1.0 | 0.953 | 0.6812 |
| 30TS | 2026-08-22 | familiar | 3P1S | FSC | 0.991 | 1.091 | 0.6607 |
| 9T0W | 2026-08-22 | familiar | 1GEY | PPE | 0.284 | 0.662 | 0.6395 |
| 9S7U | 2026-08-15 | familiar | 6SGP | LDK | 0.992 | 0.19 | 0.6162 |

## Public API sensitivity check

The public Foldseek queue completed 62 of 100 targets before repeated timeouts. Among 60 targets with known labels from both backends, 56 agreed (93.3%; 95% target-bootstrap CI [0.8667, 0.9833]). The final table uses the single version-pinned local database snapshot for every target.

## Limitations

- The 2021-09-30 cutoff approximates AlphaFold 3 training availability; it does not describe every model's private or later training corpus.
- Foldseek is a global structural retrieval step. Pocket-local fitting reduces, but does not eliminate, global-vs-pocket similarity mismatch.
- Holo/apo state, missing residues, alternate conformers, and oligomer choice can change a pocket comparison.
- The public Foldseek `pdb100` database is mutable, so cache and result digests are part of the provenance.
- Ligand component filtering is heuristic. Excluded cofactors, additives, modified residues, or unusual ligands can affect the nearest analog.
- The blind proxies are evaluated, not production selectors. Their candidate-pose overlap is not a calibrated probability of pose correctness.

## Provenance

- Scorer: `foldseek-pdb100-carried-ligand-overlap/v7`
- Foldseek backend counts: `{"local-foldseek-batch": 97}`
- Foldseek release: `10-941cd33`
- Foldseek database downloaded: `2026-08-27T09:10:23.761167+00:00`
- Exact audit SHA-256: `55de26cb65e79bead0a4a9e7cc2f35175ab340907de5bbc056ec28e7c3e246e1`
- Blind audit SHA-256: `922f7a5f3b912a80a1e6eb86b423d075beb5ccb051ab70bd87db2f0ff50c67c5`
- Raw structures, API responses, and resumable caches are intentionally outside Git.

Per-target results and all confidence intervals are in [`weekly-training-similarity-results.json`](weekly-training-similarity-results.json) and [`weekly-training-similarity-results.csv`](weekly-training-similarity-results.csv).
