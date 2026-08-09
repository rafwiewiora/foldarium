# Weekly pose-metrics deployment

`modal_scoring_app.py` is a separate, CPU-only Modal app. It must not be folded
into or deployed over `foldarium-predictions`: Brian can use that production app
independently while scoring stays serialized in `foldarium-weekly-scoring`.

The scientific unit is one exact cofolded pair:

- the full protein from that prediction sample, not the Show-all receptor;
- the ligand coordinates from that same prediction sample;
- the task SMILES used to restore ligand bond order without changing coordinates.

The function returns two pose-only metrics. It invokes smina `2020.12.10` with
`--score_only`, the built-in `vina` functional, one CPU, and seed zero. It also
uses ProLIF `2.2.0` with RDKit `2026.3.4` to count unique
`(protein residue, interaction type)` fingerprint bits. The count includes
`VdWContact`, so it is a simple contact-density summary, not an independent
binding-affinity estimate. Neither metric uses the unreleased reference.

The worker never docks, searches, minimizes, or writes to Supabase. The
third-party smina runtime image is immutable by OCI digest; every result records
the executable SHA-256, `smina --version` output, exact input hashes, ProLIF/RDKit
versions, and interaction policy.

After reviewing the image and function, deploy to the Modal `main` environment:

```console
modal deploy -e main pipeline/deploy/modal_scoring_app.py
```

Score one reviewed local pair synchronously:

```console
modal run -e main pipeline/deploy/modal_scoring_app.py::score_local \
  --protein-path /absolute/path/protein.pdb \
  --ligand-path /absolute/path/pose.pdb \
  --ligand-smiles 'CCO' \
  --pose-id weekly-item-choice
```

The deployed function name for the coordinator is `score_pose` in app
`foldarium-weekly-scoring`. It has no secret, GPU, schedule, or database access.
Its hard Modal envelope is one physical CPU, 2 GiB RAM, five minutes, and one
container. The inner smina subprocess has a two-minute ceiling per pose.

The weekly assembler calls this deployed function only when its explicit
`include_pose_metrics` argument is true. A normal assembly remains metric-free.
Scoring happens before the immutable blind-manifest digest is created, and any
missing, mismatched, or malformed result aborts assembly before a round can open.

The reviewed production-runtime canary on 2026-08-08 used only synthetic local
coordinates. It verified the pinned smina binary and ProLIF runtime end to end;
its numerical score is intentionally not a scientific calibration point. Before
scoring a full round, run a reviewed real cofolded receptor/pose pair and inspect
both the score provenance and interaction summary.

At Modal's 2026-08-08 list rates, the requested resources cost approximately
`$0.00001754/second` (`$0.00105/minute`): one CPU at `$0.0000131/second` plus
2 GiB at `$0.00000222/GiB/second`. A 330-pose weekly set taking two to five CPU
seconds per pose is about `$0.012-$0.029`, plus cold-start/image-transfer time.
The deliberately pessimistic two-minute-per-pose ceiling would be about `$0.69`
for 330 poses. Confirm actual duration on a handful of representative pairs
before submitting the whole set.
