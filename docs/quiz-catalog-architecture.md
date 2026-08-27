# Quiz catalog and weekly lifecycle

Foldarium currently has two useful but separate products:

- an active weekly blind round, whose answers must remain private until the
  Wednesday reveal; and
- a small, already-revealed example set that people can play for fun.

The production database should make those two lifecycle states explicit rather
than copying a weekly manifest into an unrelated static dataset. A weekly item
becomes eligible for the historical pool only through a recorded, idempotent
promotion after its reference coordinates and evaluation have been published.

## Durable catalog

Add a service-owned `quiz_systems` catalog. One row represents one scientific
system, not one appearance in a quiz session. Suggested fields are:

- `system_id uuid` as the internal stable key;
- `source_kind` (`legacy`, `wwpdb_weekly`, or `cameo`), source target ID, and
  optional upstream accession;
- `source_week date` and the exact source campaign and round IDs;
- ligand/task identity and the existing drug-like/size/quality filter policy;
- immutable input, prediction, reference, and evaluation digests;
- `answer_status` (`blind`, `revealed`, `withdrawn`, or `invalid`);
- `pool_status` (`ineligible`, `eligible`, or `retired`), with eligibility and
  retirement timestamps/reasons;
- display-safe metadata and service-only provenance kept in separate columns
  or related private tables.

`source_week` is the Saturday quiz date. Store actual `opens_at`, `closes_at`,
coordinate release, reveal, and promotion timestamps separately; do not infer
them later from a name or local timezone.

The catalog references immutable media/artifact rows. It must not duplicate
large PDB objects or make private answer artifacts public merely by changing a
status column.

## Weekly rounds remain immutable

`weekly_quiz_rounds` continues to bind an advertised blind manifest to its
digest. `weekly_quiz_round_items` should map each advertised item to a
`quiz_systems.system_id`, its round-local blind item ID, ordering policy, and
private evaluation index.

Opening a round is atomic and append-only. Neither clustering, confidence,
method labels, scoring, nor media should be edited under an existing open round
ID. A changed manifest receives a new round ID. The production and preview
environment partition remains part of the round key and all current-round RPCs
remain environment-scoped.

Wednesday reveal performs one transaction:

1. verify released coordinates and all artifact digests;
2. evaluate every advertised raw pose;
3. publish the exact reveal manifest;
4. mark the round and its catalog systems `revealed`;
5. write a promotion event for every newly eligible system.

Delayed, withdrawn, or invalid structures are represented explicitly and do not
silently change the membership or digest of an already-open round.

## Historical “for fun” pool

Promotion is an idempotent service-role operation keyed by
`(system_id, reveal_manifest_sha256)`. It records the eligibility policy version
and timestamp. Promotion does not overwrite the original weekly provenance,
votes, or date; a player can always see which week a historical system came
from.

The historical pool contains only `answer_status = 'revealed'` and
`pool_status = 'eligible'` systems. The server returns the answer/evaluation
only through revealed-session RPCs. The browser never receives the full answer
catalog or any active-week answer rows.

## Random draws and resumable sessions

Random selection belongs on the server. Add `quiz_session_items` with a unique
`(session_id, question_index)` and `system_id`. A start-session RPC should:

1. create or idempotently resume the named/anonymous session;
2. select eligible systems under a versioned sampling policy;
3. persist the complete assignment before returning question one; and
4. return only the display-safe manifest for that assignment.

Persisting assignments makes refresh/resume, replay analysis, and scoring
reproducible. A deterministic seed derived with a server secret may be retained
for audit, but the saved assignment is authoritative.

Sampling should support explicit strata (source, week, target class, ligand
size, difficulty, and prior exposure) and per-session caps. Initial production
policy can be uniform over eligible systems while excluding systems already
served to the same participant hash. Later policies can rebalance under-sampled
weeks without changing historical sessions.

## Rollout

1. Finish and validate the first weekly in the `preview` environment.
2. Backfill the existing example subset into `quiz_systems` as `legacy`,
   `revealed`, and `eligible`, preserving its current IDs and media.
3. Link weekly round items to catalog systems without changing the live weekly
   client contract.
4. Exercise Wednesday reveal and promotion in Preview with synthetic sessions.
5. Add server-assigned historical sessions behind a feature flag, then compare
   assignments, scoring, replays, and suggestions against the static flow.
6. Enable production historical draws; keep the static example path available
   as a rollback until the first promoted weekly has been played successfully.

All schema changes are additive until step 6. Production and Preview may share
the current Supabase project only while every new row and RPC is strictly
environment-scoped; a separate project becomes preferable when public beta
traffic, destructive test fixtures, or independent auth/storage policies make
that operational isolation worth the additional migrations and secrets.
