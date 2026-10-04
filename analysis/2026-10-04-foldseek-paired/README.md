# Foldseek on predicted complexes versus crystals: paired retrospective analysis

**Keep the five-question uniform draw.** The available data support a descriptive
paired comparison, but do not establish that blind Foldseek improves pose
selection over ligand pLDDT or supplies a validated question-interest score.
Crystal-based overlap is more informative retrospectively in the main matched
cohort; it is unavailable for a genuinely blind prospective selector.

This report uses only existing results and three published, revealed rounds.
No Foldseek search, inference, paid API call, database download, pose modification,
new public deployment or sampling-policy change was performed.

## What was actually compared

The original [audit](../../docs/weekly-training-similarity-audit.md) reports
nearest-training-system AUROC **0.7539 on 80 targets**. Its positive label is
**crystal-defined familiarity**, not correct pose availability or pose-selection
success. We reproduced that number before changing the endpoint.

The crystal score is maximum carried-training-ligand overlap with the released
crystal ligand, using crystal-based structural retrieval. The blind scores use
one archived predicted receptor/pocket per target and overlap with the candidate
predicted poses. `nearest` restricts the search to the nearest ligand-bearing
training hit; `pocket` considers the retained top 25. Their target score is the
maximum candidate-pose overlap, and the stored winning choice ID specifies the
pose they select. This changes both the receptor query and the ligand geometry;
it does **not** isolate the effect of replacing only the Foldseek receptor query.

We joined by exact week and target, then exact public choice ID. All 1,000 raw
poses across 100 distinct PDB targets were retained. Every recorded blind winning
choice maps to the published pose; every non-null historical correct-pose label
agrees with the current immutable reveal. The old report omitted outcomes for
three failed crystal audits; their now-read public reveal labels are recorded,
but those targets are not silently added to crystal-score comparisons.

Endpoints are deliberately separate:

- **Oracle availability:** at least one of the ten poses has published raw
  `correct=true`. All 1,000 flags agree with RMSD ≤1.5Å in these rounds.
- **Confidence-selected success:** the pose with highest ligand pLDDT is raw
  correct; ties break by ascending choice ID. This is a frozen simple baseline,
  not a trained confidence calibrator.
- **Actual blind retrieval success:** the stored Foldseek winning pose is raw
  correct. Cluster-accepted correctness is not substituted for the chosen pose.
- **Continuous geometry endpoint:** negative best-pose RMSD, so larger values
  mean better available predictions. This is not the training-alignment RMSD.

## Main comparison: identical 47 targets

The 47 targets have known, finite crystal overlap and both blind overlap scores;
30 contain a correct pose and 23 have a correct confidence-selected pose. All
intervals below are paired, week-stratified target bootstrap 95% intervals
(5,000 resamples, seed 20261004). The same target draw is used for every metric
and each difference; no pose is treated as an independent sample.

| Evidence score | AUROC: correct pose available | AUROC: confidence-selected pose correct | Spearman with negative best RMSD |
| --- | --- | --- | --- |
| Crystal overlap | 0.776 [0.641,0.895] | 0.822 [0.710,0.922] | 0.582 [0.361,0.753] |
| Blind nearest | 0.549 [0.384,0.712] | 0.582 [0.415,0.745] | 0.230 [-0.072,0.493] |
| Blind top 25 pocket | 0.644 [0.488,0.793] | 0.695 [0.539,0.845] | 0.376 [0.103,0.622] |

For correct-pose availability, the paired **blind-minus-crystal** AUROC difference
is −0.227 [−0.371,−0.095] for nearest and −0.132 [−0.265,−0.008] for top 25 pocket.
These are descriptive conditional intervals, not causal effects or evidence of
prospective generalization. The small top 25 difference interval is particularly
sensitive to the cohort and bootstrap assumptions; no multiplicity adjustment
was made for these exploratory comparisons.

The picture varies by week: crystal/nearest/pocket availability AUROCs are
0.729/0.646/0.771 for August 8 (n=14), 0.545/0.390/0.435 for August 15 (n=18), and
0.977/0.636/0.773 for August 22 (n=15). Three weeks do not support a reliable estimate
of between-week generalization. Bootstrap intervals condition on these weeks
and assume targets are independent within week; unmeasured homology dependence
could make them optimistic.

## Choosing a pose is a separate test

On the same 47 targets, nearest retrieves 21 correct poses (44.7%), pocket 22
(46.8%), confidence 23 (48.9%), and Smina 22 (46.8%). Nearest minus confidence is
−4.3 percentage points [−12.8,+4.3]; pocket minus confidence is −2.1 [−8.5,+4.3].
Neither improves on confidence with a clearly positive paired interval.

Using **all 81 targets with both blind overlap scores**, including one whose
crystal audit failed, nearest retrieves 35/81 (43.2%), pocket 37/81 (45.7%), and
confidence 35/81 (43.2%). Pocket minus confidence is +2.5 points [−3.7,+8.6].
The expected success of a uniformly selected raw pose is 41.6% on this cohort,
so much of the aggregate success is explained by which targets have many good
poses. These data do not demonstrate a reliable blind selector gain.

There is still a potentially useful **target-difficulty signal**: on those 81
targets, blind top 25 pocket AUROC for correct-pose availability is 0.707
[0.595,0.815], versus nearest 0.590 [0.472,0.700]. That association is distinct
from identifying the correct pose, estimating human interestingness, or proving
a benefit from weighting a five-question draw.

A terminology correction matters: the old report's “Pose/None accuracy” measures
only whether the threshold predicts existence of a correct pose; it does not
require a selected pose to be correct. On its original 80-target cohort, actual
pose-or-None success (choose None below 0.25, otherwise the stored winning pose)
is 47.5% nearest and 51.3% pocket, rather than the old existence-only 55.0%/61.3%.
No old report data were overwritten.

## RnP-style comparison on the common 34 targets

Both RnP-style crystal/blind scores and both overlap families exist for the same
34 targets; 21 have a correct pose. RnP-style availability AUROC is 0.696
[0.514,0.873] from crystals and 0.652 [0.462,0.829] from predictions. The paired
blind-minus-crystal difference is −0.044 [−0.227,+0.122]: inconclusive.

On these exact 34, RnP-style retrieves 18/34 correct poses (52.9%), confidence 16/34
(47.1%), nearest 14/34 (41.2%), and pocket 16/34 (47.1%). RnP minus confidence is
+5.9 points [−5.9,+17.6], also inconclusive. This controlled approximation is not
the original RnP paper's full PLINDER search. Do not compare its 34-target result
to another method's 80-target result as evidence of a ranking.

## Missingness is part of the result

| Quantity over the complete 100-target source population | Available |
| --- | ---: |
| Released raw pose outcomes | 100 |
| Crystal familiar/novel classification | 97 |
| Numeric crystal overlap | 52 |
| Numeric blind nearest and pocket scores | 81 |
| Numeric RnP-style crystal score | 47 |
| Numeric RnP-style blind score | 54 |
| Main continuous crystal/blind intersection | 47 |
| All metric families' continuous intersection | 34 |

The old report's 51 exact-overlap count and 80 blind count are conditioned on its
complete-audit pair/classification subsets; they are not the global numeric
availability counts above.

The 47-target continuous cohort is 37 familiar/10 novel, with 30/47 (63.8%) having
a correct pose. The 53 excluded targets are 2 familiar/48 novel/3 unknown, with
26/53 (49.1%) having a correct pose. Thus finite-score analysis disproportionately
includes familiar targets. Of 48 absent crystal overlap scores, 44 mean no usable
pre-cutoff ligand-pocket analog, 1 means a confirmed empty search, and 3 are audit
failures. The 19 targets without blind nearest/pocket scores contain 6 correct-pose
targets (31.6%). Missing/empty/failed cases are not interchangeable, and no null
was imputed as overlap zero. The original compact report does not retain every
blind failure reason; recover raw audit records before modeling missingness.

## What is eligible before closing, and what is still missing

Predicted receptor/pocket/poses, their confidence, and a frozen pre-cutoff training
reference library are structurally eligible inputs before closing. Released
crystal queries, crystal ligands, RMSD/correctness, revealed votes, and this paired
analysis are retrospective only. No crystal-derived label should enter a weekly
draw or blind scoring prompt.

The existing blind computations are **post hoc replays of eligible input types**,
not proven preclose executions. The database was downloaded August 27, after all
three voting windows closed, with a 2021-09-30 reference release cutoff. That cutoff
is an approximation to training availability, not proof of any model's actual
training corpus or proof that the complete pipeline was frozen before voting.
There are no new held-out weeks in this analysis and no fitted model.

A pure receptor-query comparison or independent audit of every per-pose ranking
requires the raw artifacts that the compact report does not embed:

1. Full exact audit SHA `911a638269c9569dc94a709b87c0283e3be8118b3b439b8d5f9e2b446c7c021f`
   and blind audit SHA `6a19f126bab7869178f85b6a4d4710956427f974fd88a8b37177460725d5dcaf`,
   including per-choice scores, query/pocket/pose hashes and failure reasons.
2. The exact and predicted query manifests, pinned database digest/release,
   retained raw top 25 hits and query-to-hit correspondences.
3. A factorial replay holding pose candidates and pocket/overlap rules constant
   while changing only crystal versus predicted receptor query; the existing
   aggregates change multiple components and cannot answer that causal contrast.
4. For prospective claims, a manifest/timestamp receipt proving blind scores and
   chosen pose IDs were frozen before the original closing time.

Raw structures and search caches are intentionally outside Git. Their absence
from the checked-out compact report does not imply they were deleted elsewhere;
this task did not scan or download a large search database to recover them.

## Bounded next experiment, with no change to the default draw

Use four consecutive new weeks (up to 40 targets/week) as a fixed descriptive
holdout, retaining all targets and all ten poses. Reuse an already available,
digest-pinned Foldseek database; if it is unavailable, leave the experiment
blocked rather than download a new large database. Run one predicted-receptor
query per target in bounded CPU batches (maximum 2 concurrent jobs, no GPU),
retain at most 25 training hits, and preserve each failed attempt as unknown.
Freeze the scored blind artifact and all selected choice IDs before closing.
Do not tune the 0.25 threshold on these weeks.

After release, evaluate identical targets with crystal queries and published
RMSDs. Predeclare paired target-level endpoints: raw top 1 success versus ligand
pLDDT, correct-pose-availability AUROC, RMSD rank correlation, missingness, and
cost. Keep crystal-query diagnostic results separate from prospective eligible
results. Analyze by week and target; group repeated PDBs or known homologous
families when available. Four weeks is a bounded pilot, not a power guarantee.

Uniform five remains the human default. Any later weighting experiment should
use only frozen blind evidence, explicitly retain sampling probabilities and
compare human completion/engagement separately from scientific pose accuracy.
Existing prediction-derived cluster diversity/disagreement can remain descriptive
optional metadata; this analysis does not validate it or Foldseek as an
interestingness score.

## Reproduction and provenance

Run locally, without network or optional scientific packages:

```sh
python3 analysis/2026-10-04-foldseek-paired/analyze.py
```

[`outcomes.json`](outcomes.json) is a safe scientific projection of the three
published API responses, with response SHA256, exact round/window identity,
all pose IDs/content digests, metrics and labels. It excludes participant names,
votes and structure bytes. [`results.json`](results.json) records source hashes,
all cohort membership, confidence intervals and exact selector-to-pose mappings.
The original scientific report is read without modification. Script assertions
check unique 100-target joins, every selected pose identity, original known labels,
and exact reproduction of the historical 0.7539 proxy-label AUROC.
