# Prediction deployment adapters

This directory is an infrastructure edge around the provider-neutral
`foldarium_pipeline` package. A prediction is always the same versioned
`PredictionTask` JSON, and execution always returns the same `PredictionResult`
dictionary. Modal/GCP settings belong here or in infrastructure configuration;
they must not leak into the scientific task schema.

## Modal bootstrap

The Modal scaffold is safe to prepare before an account is available. It does
not deploy or download models merely by existing in the repository.

1. Install Modal in a deployment-only environment: `python -m pip install modal`.
2. Authenticate with `modal setup`.
3. Create a Modal secret named `foldarium-control-plane` containing
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `FOLDARIUM_STORAGE_BUCKET`.
   Add any other storage credentials required by the core worker to this secret;
   never put values in this repo.
4. Validate with a dry-run task in the core CLI before spending GPU time.
5. Validate the reviewed non-secret production profile from the repository root:

   ```bash
   python3 pipeline/deploy/deploy_profile.py
   ```

   The default is validation-only. Never deploy this app with a bare
   `modal deploy`: its schedules and mutation gates are import-time settings and
   a bare redeploy silently removes them. After reviewing the printed config and
   commit, the explicit production form is:

   ```bash
   python3 pipeline/deploy/deploy_profile.py --apply \
     --confirm molspace-production/main/foldarium-predictions
   ```

   The wrapper requires a clean worktree, scrubs ambient `FOLDARIUM_*` values,
   supplies every gate from `profiles/molspace-main.json`, and verifies the
   deployed configuration digest through the read-only `deployment_config`
   function. Modal-managed secrets are not part of the profile.
6. Bootstrap the OpenFold3 checkpoint cache once with
   `modal run pipeline/deploy/modal_app.py::bootstrap_openfold3_cache`. The
   function writes an explicit `setup_openfold --config` at runtime so both
   `openfold_cache` and `param_directory` point into the mounted Volume.
7. Submit one task with
   `modal run pipeline/deploy/modal_app.py --task-json '<json>'`, or invoke the
   deployed `submit_tasks` function from the control plane.

See Modal's official guides for [GPU functions](https://modal.com/docs/guide/gpu),
[secrets](https://modal.com/docs/guide/secrets), [Volumes](https://modal.com/docs/guide/volumes),
and [scheduled functions](https://modal.com/docs/guide/cron) before production deployment.

OpenFold3 uses the official 0.4-pixi / OpenFold3 0.4.4 OCI index pinned by
immutable digest.
Modal runs under an injected Python 3.12 control interpreter. The upstream OF3 Pixi
environment is activated only around `setup_openfold` and `run_openfold` subprocesses so
its Python and runtime packages cannot shadow Modal's own dependencies.
The initial function requests an A100-40GB, matching upstream's commonly tested
baseline; route genuinely larger targets to a separately costed A100-80GB policy
instead of silently changing the standard campaign runtime.
It also requests eight physical CPU cores and 32 GiB host RAM. Boltz-2 requests
four cores and 16 GiB with its L40S. Both GPU functions have `max_containers=1`
and scale to zero when idle, which is intentionally conservative for the first
credit-limited smoke tests.

Boltz is built with exactly `boltz[cuda]==2.2.1`. Model files persist in
method-specific Modal Volumes to reduce cold starts. Those volumes are caches
only: the deployment wrapper fails closed unless its Supabase publisher is
configured. It atomically claims the deterministic run before GPU execution,
then uploads every durable artifact and writes run state/provenance before
returning success.

Model-command failures are also finalized through the same RPC, with no artifact
rows and a sanitized error summary. Retry policy can then create or reclaim work
deliberately instead of waiting for an invisible failed lease.

Boltz has no separate supported setup command. Its first small, real fixture
prediction is the cache bootstrap/smoke run: use a task with `msa_mode: empty`
and one diffusion sample after the core dry run passes. It downloads into the
mounted Boltz cache and still follows the normal durable publication path. Do
not use an unpublished throwaway run merely to warm the cache.

The weekly schedule is completely absent unless `FOLDARIUM_ENABLE_WEEKLY_CRON=1`
is set when the app is deployed. Once enabled, the default polls every 15 minutes
from Saturday 03:00 through 06:45 UTC. Expected CAMEO publication lag is returned
and logged as `waiting-for-inputs`, not treated as a failed task.
Use `FOLDARIUM_WEEKLY_HOOK=foldarium_pipeline.weekly:modal_weekly_hook`. Planning,
registration, and GPU spend remain independently gated: registration requires
`FOLDARIUM_WEEKLY_REGISTER=1`, and calls are spawned only with
`FOLDARIUM_WEEKLY_SUBMIT=1` after the registration RPC reports success. With the
submit flag absent, the scheduled function returns the target count, accelerator
mix, and maximum GPU-seconds as `planned-not-submitted`. Change
`FOLDARIUM_WEEKLY_CRON` in the environment used by `modal deploy` to move the
schedule without changing core code.

`FOLDARIUM_WEEKLY_GPU_CLASS` is an explicit operator calibration override. The
first production round pins both methods to `l4` and records sampled
`peak_gpu_memory_mib`; a CUDA OOM is stored as `gpu_out_of_memory` and is never
retried automatically. Remove the override only after replacing the provisional
generic sizing ladder with measured, method-specific thresholds.

The deployment adapter embeds only those non-secret weekly switches into the
CPU control image. Supabase credentials are supplied separately by the
`foldarium-control-plane` Secret. Search logs with:

```bash
modal app logs foldarium-predictions --env main --timestamps --search foldarium.weekly
```

Once a registered campaign exists, later ticks exit before crawling CAMEO or
submitting work. Therefore enable registration and GPU submission together only
after approving the displayed bounded budget; a registration-only deployment is
an intentional operator hold point, not an automatic future-submit queue.

### Saturday pose metrics

Weekly fixed-pose metrics run in the separate CPU-only
`foldarium-weekly-scoring` app documented in [SCORING.md](SCORING.md). That app
has no Supabase secret, GPU, schedule, or publication path and cannot replace the
`foldarium-predictions` deployment. It scores the exact pose-specific predicted
protein and ligand together, returning smina score-only affinity plus a simple
ProLIF interaction count with complete provenance.

`assemble_weekly_quiz_round` remains metric-free by default. Pass its explicit
`include_pose_metrics=True` argument only after the scoring app and one real
cofolded-pair canary have been reviewed. The assembler resolves the deployed
function by app/function name, binds successful results into the v3 stage and
manifest digests, and fails closed before opening if any pose cannot be scored.

### Wednesday reveal

#### Exact private pre-close catch-up

`materialize_private_weekly_evaluation` is a manual, unscheduled, CPU-only path
for the one explicitly allow-listed production replacement round
`weekly-2026-08-08-beta-v5-global-tm-29`. It exists so released-coordinate
results can be materialized privately for interface development without ending
the published voting window or exposing answers.

The additive migration
`supabase/migrations/20260815020000_add_private_weekly_evaluations.sql` must be
reviewed and applied before the function can catalog an artifact. The migration
adds an append-only descriptor table with no anon/authenticated grants, policy,
view, or public RPC. Its insert trigger locks the exact round and requires it to
remain production, open, unrevealed, inside its voting window, and bound to the
same blind/private-index digests. The artifact itself is stored by digest in the
configured private prediction-results bucket; the function verifies that the
bucket is not public before downloading or scoring anything.

After a separate code/migration/deployment review, the only supported operator
command is the exact no-publish invocation:

```bash
MODAL_PROFILE=molspace-production modal run --env main \
  pipeline/deploy/modal_app.py::materialize_private_weekly_evaluation \
  --round-id weekly-2026-08-08-beta-v5-global-tm-29 \
  --no-publish
```

Both `--round-id` and `--no-publish` are mandatory. Omitting `--no-publish`,
passing `--publish`, naming another round, selecting a public Storage bucket,
running before open/after close, or observing any existing reveal field aborts
before catalog insertion. The path has no reveal callback and never updates
`weekly_quiz_rounds`, `closes_at`, or reveal fields. Repetition with identical
scientific inputs verifies/reuses the same content-addressed object and exact
catalog row.

The returned report contains only the private object descriptor and integrity
digests, not the reveal manifest. Retrieve the artifact only with a service-role
operator for local/Preview fixture generation. Do not add its table or content
to a browser view/RPC, copy it into the public quiz bucket, or treat successful
materialization as approval for an early public reveal.

#### Automatic post-close retrospective

`weekly_retrospective_tick` is a separate CPU-only job that never calls the
weekly reveal RPC. It is absent from a deployment unless
`FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE=1` is set at deploy time. Its default
`FOLDARIUM_WEEKLY_RETROSPECTIVE_CRON` is `15 0-5 * * 3`, giving six bounded
hourly attempts beginning fifteen minutes after the voting deadline.

The job resolves the newest immutable round for the latest Saturday campaign,
requires the production voting window to be closed, and writes the deterministic
v5 artifact plus its service-role-only catalog descriptor. It accepts a round
immediately before or after the independent atomic reveal transition. An
existing row for the round short-circuits before coordinate downloads or
rescoring, so retries are idempotent. Retrospective failures do not block public
reveal and reveal failures do not block retrospective retries.

Both catalog migrations must be reviewed and applied before enabling the job:
`20260815020000_add_private_weekly_evaluations.sql` creates the inaccessible
append-only catalog, and
`20260825235500_upgrade_private_weekly_evaluations_v5.sql` binds v5 inserts to
the post-close state on either side of reveal. Keep
`FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE` unset until that migration and the
deployment are explicitly approved.

#### Retrospective archive publication

`weekly_retrospective_publication_tick` is distinct from private evaluation
generation. It consumes only an already-revealed production round and its
immutable v5 evaluation, snapshots final votes plus current human pseudonyms,
and classifies LLM credentials through the reviewed service-only automated
identity registry. It stores three
content-addressed objects in the verified private bucket:

- a server-only source snapshot containing stable participant linkage for
  retry verification and future cumulative aggregation;
- a sanitized public artifact containing human aggregates plus the approved
  LLM and Smina entries;
- a pseudonymous detail artifact containing the players' chosen display names
  plus the automated entries.

Neither returned artifact contains user/session IDs, participant hashes,
traces, comments, application state, auth data, credentials, or private object
URIs. The pseudonymous detail artifact never leaves the private bucket; the
server API projects only chosen display names and scored answers from it.
The immutable catalog records separate descriptors for all three objects and
binds them to the exact round, reveal, v5 evaluation artifact, timestamps, and
item/choice counts. Retries rebuild the source and artifacts and fail if an
existing publication differs.

Human counts, answer breakdowns, and chosen pseudonyms are public after reveal,
including for cohorts smaller than three. The known unclustered beta round is
normalized to exact-pose scope; every later non-empty vote must include an
explicit `exact` or `cluster` selection kind or publication fails. Credential
rotation for Claude Opus or Codex must use
`register_weekly_retrospective_automated_identity` with a service-role operator;
the allow-list remains code-reviewed and direct registry writes are not
supported.

The Modal schedule is absent unless
`FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE_PUBLICATION=1` is present at deploy time;
it is intentionally absent from the standing reviewed production profile.
`FOLDARIUM_WEEKLY_RETROSPECTIVE_PUBLICATION_CRON` defaults to
`45 0-5 * * 3`, giving six hourly attempts during the Wednesday window rather
than a single pre-reveal tick. With an exact `round_id`, the function processes only that round.
Without one, it calls the service-only missing-publication scan and backfills
every eligible revealed production round in reveal order; it never derives the
newest campaign.

Review and apply
`supabase/migrations/20260826003000_add_weekly_retrospective_publications.sql`
and the additive retrospective corrections through
`20260826190000_require_retrospective_vote_scope.sql` before any manual
invocation or separate deployment that enables the schedule.
Do not enable the gate in `profiles/molspace-main.json` or apply the migration
without explicit production approval. No migration, deployment, schedule, or
live service is activated by this code.

#### Selector post-close benchmark ingest

Catch-up model runs use `/api/weekly-selector/benchmarks`; they must never be
submitted through the pre-close ballot endpoint. Before enabling this route,
review and apply the Selector migrations through
`20260826210000_add_weekly_selector_post_close_benchmarks.sql`.

The Vercel deployment requires environment-specific server-only credentials:

- `FOLDARIUM_<ENV>_SUPABASE_SERVICE_ROLE_KEY`
- `FOLDARIUM_<ENV>_SELECTOR_BENCHMARK_INGEST_TOKEN`

where `<ENV>` is `PRODUCTION` or `PREVIEW`. Development uses
`SUPABASE_SERVICE_ROLE_KEY` and `FOLDARIUM_SELECTOR_BENCHMARK_INGEST_TOKEN`.
The dedicated ingest token is not a Supabase credential and must be generated
independently. None of these values may appear in browser config, logs, run
manifests, or public artifacts.

Leaving either value unset disables benchmark ingest with `503`. Registration
then remains service-role-only in Postgres, accepts only closed/unrevealed
rounds, and writes only the append-only benchmark table. No environment
variables, migrations, or endpoint are activated by merging this code.

The Wednesday evaluator is a CPU-only Modal function. Its image pins
`gemmi==0.7.5`, `numpy==2.3.2`, and `rdkit==2025.3.6`; it neither reserves a GPU
nor uses either prediction cache Volume. The schedule is absent unless
`FOLDARIUM_ENABLE_WEDNESDAY_REVEAL=1` is set at deploy time. When enabled, the
default `FOLDARIUM_WEDNESDAY_REVEAL_CRON` is `5 0-5 * * 3`: six hourly attempts
from 00:05 through 05:05 UTC on Wednesday, after voting closes at 00:00 UTC.
Each tick has two one-minute Modal infrastructure retries and one active
container, so delayed released coordinates are retried for a bounded window
without creating an unbounded poller.

Publication is a separate mutation gate. With
`FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH` absent or set to `0`, scheduled calls run
the complete evaluation as a dry run and do not call the reveal RPC. Set it to
`1` only in a reviewed deployment that should publish. A manual dry run for an
exact round is:

```bash
modal run --env main pipeline/deploy/modal_app.py::wednesday_reveal_tick \
  --round-id weekly-2026-08-08 --no-publish
```

Use `--publish` only for an explicitly approved manual reveal. If `--round-id`
is omitted, the function derives the most recent UTC Saturday campaign and
resolves its newest immutable public round by `opens_at`. This permits a
digest-preserving replacement round without direct edits to the original. It
then reads that exact private round and digest-bound private index,
downloads each original `predicted_complex` by exact `(run_id, sample_id)`, and
uses the stored classic four-character PDB target IDs for RCSB coordinates. A
missing round/index/artifact, digest mismatch, unavailable coordinate, or one
incomplete evaluation aborts the whole call before publication. Repeated calls
after a successful reveal return `already-revealed` without rescoring.

The standing `molspace-main` profile explicitly pins publication to `0` so its
scheduled Wednesday attempts are dry runs. Do not change that standing profile
to publish; use a separately reviewed explicit call when publication is
authorized.

The reviewed profile preserves the live 40-target Saturday intake cap and the
`*/15 3-12 * * 6` intake window. Preview assembly (`nextweekly_tick`) and
production promotion remain separately scheduled and idempotent. Production
opening and selector-kit registration are gated off by default
(`FOLDARIUM_WEEKLY_PRODUCTION_OPEN=0`, `FOLDARIUM_WEEKLY_REGISTER_SELECTOR_KIT=0`).
If an early wwPDB snapshot conflicts with immutable stored intake content, the
hook returns `waiting-for-registration` without exposing tasks or spawning GPU
work, then rebuilds from public inputs on the next 15-minute tick. HTTP failures
other than the fail-closed 409 conflict remain fatal. A conflict that persists
across ticks requires operator review rather than automatic overwrite.

Each failed prediction is eligible for exactly one retry. Known OOM and MSA
timeouts retain their reviewed resource escalation; every other failure repeats
once with the original L4/30-minute resources. Authorization changes
`max_attempts` from 1 to 2 before spawning and is idempotent, so a run can never
receive a third attempt. Failed output collection now records a bounded
`validation_failure` subtype (`missing_model_files`, `artifact_io_error`, or
`invalid_model_output`) in the private prediction result.

Before enabling retrospective publication, review and apply
`supabase/migrations/20260826190000_require_retrospective_vote_scope.sql`.
The publication schedule defaults to six hourly Wednesday attempts
(`45 0-5 * * 3`) rather than a single pre-reveal tick.

Use the read-only preflight before activation:

```bash
modal run --env main pipeline/deploy/modal_app.py::weekly_lifecycle_preflight
```

Keep the laptop's default Modal profile on `foldariumtest`. The separately
configured `molspace-production` profile is for Brian's final deployment only;
always pass/verify a profile explicitly before any production command.

## Portability rules

- Supabase is the control/catalog source of truth; object storage is the
  artifact source of truth.
- Inputs and outputs travel through object URIs plus checksums, not a provider
  filesystem.
- Modal Volumes, GCP disks, and container filesystems are replaceable caches or
  scratch space.
- Never bake service keys, signed URLs, proprietary data, or model credentials
  into an image.
- Pin production images by digest and record the digest in result provenance.
- Keep one image per prediction method. Dependency conflicts must not alter the
  shared task/result contract.

The Modal-built Boltz bootstrap image is intentionally convenient for the first
deployment, but a Modal image is not an externally pullable OCI artifact. Before
the GCP cutover, materialize that same pinned recipe as a Foldarium-owned image
in Artifact Registry and point both backends at its digest. The GCP mapping is
documented in [`gcp/README.md`](gcp/README.md).
