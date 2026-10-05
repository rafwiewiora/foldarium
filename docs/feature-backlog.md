# Feature backlog

This is the durable running list of potential Foldarium features. Items remain
here until they are promoted into an implementation plan or explicitly declined.

Statuses: **idea**, **candidate**, **planned**, **shipped**, or **declined**.
Prepared source remains **planned** until its production flow is verified.

## Shipped

### Retrospective Play for fun

- **Status:** shipped
- **Added:** 2026-08-28
- **Shipped:** 2026-08-29
- **Goal:** let a player launch any published Weekly from its retrospective
  detail page and replay the original blind choices without crystal overlays.
- **Leaderboard contract:** post-reveal answers remain physically separate from
  blind-week ballots and all-time rankings. The retrospective and in-quiz
  leaderboards display opted-in player scores in a separately labeled
  **For fun** group.

### Automatic cofolding benchmark refresh

- **Status:** shipped
- **Added:** 2026-10-04
- **Goal:** refresh method rankings and weekly trends from verified, published
  retrospective evaluations whenever a new round is published.
- **Shipped:** a read-only aggregate API and browser integration replace the
  static method-statistics file. Counts cover published quiz questions with
  method poses; oracle and highest-ligand-pLDDT success use raw RMSD below 1.5 Å.
  Missing ligand confidence excludes a target from top-1 only.
- **Verified:** the production endpoint loads three published weeks with explicit
  population and metric definitions. Full unpublished prediction campaigns are
  outside this aggregate's population.

### Five featured questions per week

- **Status:** shipped
- **Added:** 2026-10-04
- **Goal:** offer a small default human quiz with optional exploration of every
  question. Keep the full prediction, selector, and benchmark populations.
- **Shipped:** the browser accepts a manifest-bound featured selection,
  preserves full question ordinals for votes and recordings, restores the same
  selection on refresh, and summarizes five saved votes without claiming a
  complete full-round ballot. Existing sessions resume with all questions.
- **Shipped:** deterministic uniform selection of up to five questions, optional
  bounded interestingness weighting, immutable private audit artifacts, and the
  manifest-bound public database marker. The score uses blind pose-cluster
  diversity and cross-method disagreement; missing novelty data does not block
  selection. Selection never shrinks the full benchmark or Selector kit.
- **Verified:** an immutable five-question marker is registered for the current
  37-question round, and the public boundary exposes only its safe marker. The
  full manifest and benchmark population remain 37 questions.
- **Pending:** recurring selection for future weeks remains part of gated
  reconciliation; all newly introduced automation gates are still disabled.

## Planned

### Scoped automatic benchmark enrollment

- **Status:** planned
- **Added:** 2026-10-05
- **Prepared:** opt-in v2 policy confines new automatic enrollment to canonical
  production weeks with open voting windows, exact manifest/method binding, one
  immutable enrollment per campaign and a combined configured execution cap.
  Existing v1 authority and already-frozen work remain unchanged.
- **Prepared:** executor revalidation and service-only atomic database checks
  reject stale or out-of-scope work; portable tests cover both boundaries.
- **Pending:** public review and CI; no driver, policy or schedule is enabled.

### Reusable offline acceptance bootstrap

- **Status:** planned
- **Added:** 2026-10-05
- **Prepared:** extract the composed lifecycle test's local PostgreSQL bootstrap,
  fixture SHA/HMAC adapters and external-network denial into one portable helper.
  The existing scenario, migrations and seven CI jobs remain unchanged.
- **Pending:** public review and CI acceptance; no runtime behavior changes.

### Featured assignment comparisons

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** a separate featured leaderboard compares humans, authorized
  models, Smina, and ligand-pLDDT on the same immutable pre-close assignment.
  Exact choices score the raw pose; explicit cluster choices accept a correct
  member. Optional full-round answers do not change featured scores.
- **Prepared:** every selected question counts toward completion, while verified
  unscorable references stay outside accuracy. Only completed assignments enter
  featured all-time totals; legacy full-round history remains separately visible.
- **Verified:** accepted and deployed API/browser/schema, focused scoring and
  provenance tests, database permission guards, and shared browser parity.
  Historical post-close draws correctly remain ineligible. Verification of the
  first eligible published cohort remains pending.

### Versioned hydrogen-aware intake

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** opt-in policy v5 excludes explicit and isotopic hydrogen from
  heavy-atom counts, preserving helium and other two-letter elements. Frozen v4
  targets, identities, and replay behavior remain unchanged; mixed or unknown
  policies fail closed. The default remains v4.
- **Verified:** boundary, chemistry, and immutable v4 replay tests. Future intake
  activation is a separate operational decision.

### Paired Foldseek evidence

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** reproducible offline analysis joins 100 targets and 1,000 raw
  poses from three already published rounds, separating crystal-defined
  familiarity, oracle pose availability, and actual selected-pose correctness.
- **Conclusion:** available evidence does not establish a validated blind
  interestingness score. Retain the uniform five-question default; the report
  proposes a bounded prospective experiment without activating it.


### Historical Preview research archive

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** explicitly authorized expired Preview rounds retain their exact
  original identity, manifests, and voting window. Separate immutable evaluation
  and publication catalogs preserve all poses and exclude human ballots.
- **Prepared:** a distinct archive page and API show verified research results,
  exact model decisions, and scorable versus excluded populations. Frozen model
  receipts are required before publication; this does not reopen voting.
- **Verified:** portable API/server dispatch, database guards, and mobile/desktop
  synthetic browser review. Live historical publication remains pending.

### Receipt-bound API model identities

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** new archive model labels require exact frozen execution and
  verified receipt proofs. Human display names cannot grant automated identity.
  Private proofs stay outside responses, and legacy source bytes remain stable.
- **Verified:** all archive API modes, isolated SQL joins, and mutation/forgery
  rejection. Paid provider execution remains a separate operational decision.

### Optional H-bond evidence availability

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** a parsed unknown receptor residue can make optional ProLIF
  evidence explicitly unavailable. Original poses and successful Smina results
  remain; missing evidence is never displayed as zero. Other science failures
  remain fatal, and correctness denominators are unchanged.
- **Verified:** strict blind marker, retained pose identities, database opening
  guards, and both browser evidence labels. Recovery rollout is tracked separately.

### Audited incomplete-reference handling

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** exact chemical identity and deposited missing-atom proof can mark
  a released crystal reference unscorable. All original questions, choices,
  artifact identities, and votes remain intact; correctness and RMSD are null.
- **Prepared:** private evaluation v6 and retrospective v2 expose full, scorable,
  and excluded populations. Leaderboards, selector scoring, cofolding metrics,
  and Play for fun exclude only verified unscorable questions. Fully scored
  historical artifacts retain their existing formats and behavior.
- **Prepared:** molecular archive review displays the original poses without
  fabricating an answer overlay or a winning None choice. Zero-contact display
  warnings survive retrospective projection.
- **Pending:** production recovery publication verification; source validation
  does not itself claim that a delayed round has been revealed.


### Immutable artifact upload and download recovery

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** survive transient Storage service failures while preserving exact
  scientific artifacts and publication integrity.
- **Prepared:** bounded retries of exact immutable downloads and uploads of
  the same bytes at the same content address,
  with SHA verification after ambiguous success. Permanent failures and digest
  conflicts fail closed; RPCs and mutable writes do not retry.
- **Deployed:** the immutable-upload retry fix is running in production.
- **Verified:** the subsequent Preview assembly completed with 39 questions and
  390 choices. Full evaluation and production publication remain separate work.

### Private failure evidence preservation

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** bounded private logs and exact native output bytes can be
  preserved after a returned prediction failure. Sensitive or oversized native
  files are omitted; logs redact known/escaped credentials and entire HTTP URLs,
  including interrupted quoted credential values.
- **Prepared:** service-only append-only registration binds evidence to the exact
  run, claimed attempt, worker, task digest, and private content-addressed bucket.
  Discovery and upload phases are bounded; skipped links mark incomplete evidence.
- **Verified:** accepted hardening, applied private catalog migration, and full
  public CI including clean database replay. The operational worker bundle and
  stored reconciliation schedule are verified deployed. The public library requires an explicit host
  hook and never retries scientific work.

### Bounded inference and completed-work recovery

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** portable provider and recovery libraries require an explicit
  pinned model/configuration, frozen job and blind kit, one-time budget grant,
  durable ledger, serialized execution, and caller-enforced API-only isolation.
  No schedule, hosted launcher, default budget, or automatic invocation is added.
- **Prepared:** rate-review expiry prevents new spending while allowing exact
  completed artifacts and successful cached responses to be recovered. Missing
  ledgers, ambiguous paid outcomes, or changed input/configuration fail closed.
- **Verified:** synthetic no-network tests exercise the real runner and immutable
  artifact registration retry. Production spend remains disabled.

### Acknowledged dispatch observation

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** an SDK-injected observation helper distinguishes polling
  deadlines from terminal worker failures and expired output. Missing or
  ambiguous state preserves the existing dispatch receipt without authorizing
  a replacement prediction.
- **Prepared:** a genuine function timeout rechecks the exact run before the
  existing unclaimed-run replacement path. No retries of claimed scientific
  work, schedules, deployment configuration, or budget changes are introduced.
- **Verified:** portable tests cover nested call identity, uncertainty, terminal
  errors, receipt preservation, and the run-claim race. Deployment is separate.

### Typed vote archival and composed lifecycle verification

- **Status:** planned
- **Added:** 2026-10-05
- **Prepared:** verified typed selection provenance is authoritative even when
  optional UI telemetry is absent or contradictory. Legacy source bytes remain
  unchanged; malformed typed proof cannot fall back to telemetry.
- **Prepared:** a private scoped getter validates immutable attempts/resolutions,
  exact manifest identity and complete audit counts, including SQL NULL edges.
- **Prepared:** offline composed PostgreSQL acceptance exercises full six-item
  model/benchmark population, five persisted human votes, bounded inference
  recovery, ingestion, reveal, publication and strict public projections.
- **Pending:** accepted private release, deployment and public CI. No paid calls,
  hosted launchers, live credentials or runtime settings are part of this bundle.

### Sparse deposited receptor proof and publication protection

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** a conditional evaluator aligns sparse released receptors only
  with complete deposited sequence/label/missing-residue proof. Ordinary v4
  results remain byte-identical; the conditional path records explicit v5 proof.
- **Verified:** the preserved 37-item/370-choice replay retains 300 identical v4
  scores and 60 existing unscorable choices, while ten choices gain the proven
  conditional evaluation. All 36 other items remain unchanged.
- **Prepared:** all ten reference-proof fields are rejected throughout blind
  manifests and selector kits, including the standalone verifier.
- **Prepared:** generic claimed-worker failure cannot authorize a retry while
  completed native science may await publication. SQL and Python preserve the
  exact run/attempt for artifact recovery.
- **Verified:** production worker deployment, migration through `20261005030000`,
  and all seven public CI jobs. The preserved evaluation completed successfully
  with 37 items, 370 choices, 31 scorable items and six reference exclusions.

### Supported Node CI coverage

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** web and API tests plus the public boundary audit run independently
  on Node 22 and 24; selector parity explicitly uses Node 24.
- **Verified:** both supported runtime jobs and all other public CI checks pass. This changes test runners only,
  without altering runtime configuration, hosting integration or access policy.

### Durable prediction handoffs

- **Status:** planned
- **Added:** 2026-10-04
- **Prepared:** single submission grants bind exact run attempts, frozen tasks,
  acknowledgements and worker identity. Uncertain outcomes cannot authorize a
  duplicate prediction; proven loss preserves immutable private evidence.
- **Prepared:** verified complete logs-only failures may use the existing single
  retry. Native, missing or incomplete evidence requires artifact recovery.
- **Verified:** production worker cutover and the additive migration. The
  portable library and injected tests do not enable a hosted scheduler.
- **Pending:** full public CI and unattended recovery observations.

### Durable weekly reconciliation

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** reconcile exact stored campaigns and rounds through preparation,
  featured selection, benchmark ingestion, evaluation, reveal, and publication.
- **Prepared:** provider-independent planning, a bounded service-role execution
  loop, durable action leases and dispatch receipts, immutable benchmark
  expectations, exact artifact verification, and atomic reveal eligibility.
  Gates default off and dry runs perform no mutations. Absent benchmark policy
  blocks release; an intentionally empty policy must be explicitly configured.
- **Prepared:** exact lifecycle scope binds one canonical round and manifest per
  campaign/environment. Ambiguous historical siblings are not automatically
  enrolled, repaired, or promoted by guessing from version labels.
- **Prepared:** a service-only database initialization grant survives complete
  inference-volume loss. The grant is immutable and can initialize a budget only
  once; retries must preserve an existing valid ledger. No provider launcher or
  paid inference is enabled by this schema. The initialization migration has
  been applied, while paid inference remains disabled.
- **Prepared:** late handoffs atomically require an absent activation marker, so
  competing successors cannot overwrite a completed handoff even when the
  finite safety-close timestamp remains unchanged.
- **Deployed:** database migration and operational execution adapter, with all
  new gates disabled.
- **Pending:** live verification and deliberate gate activation. The public tree
  supplies the contract and tests, without a scheduler, deployment configuration,
  credentials, or provider launcher.

### Audited explicit-hydrogen eligibility compatibility

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** evaluate preserved historical ligand metadata when the original intake
  atom counter included explicit hydrogen and assembly recorded its removal.
- **Prepared:** strict compatibility requires the original passed selection
  policy, exact SMILES digest, recomputed hydrogen removal, complete graph and
  per-choice binding audits, and at least 15 actual heavy atoms. Both original
  and normalized counts remain unchanged; arbitrary count differences fail.
- **Verified:** regression tests cover preserved choices and manifests, missing
  or changed provenance, unexplained differences, and the heavy-atom minimum.
- **Deployed:** the compatibility fix is running in production; a private
  evaluation retry is underway.
- **Pending:** completed evaluation and publication recovery verification.

### Predicted-pocket display recovery

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** keep all predicted poses available when a ligand has no nearby
  receptor residues, displaying the full predicted protein with unchanged
  coordinates and a clear display warning.
- **Prepared:** the portable assembly fallback and browser warnings, including
  simultaneous alignment warnings and retrospective review, pass regression tests.
- **Verified:** the shared browser source matches production. This display
  fallback does not change scoring inputs, imply correctness, or remove choices.
- **Verified:** the new Preview assembly contains 39 questions and 390 choices,
  including one explicit full-receptor warning.
- **Pending:** complete scientific evaluation and production publication of the
  new assembly.

## Candidate features

### Apo-pocket structural similarity

- **Status:** candidate
- **Added:** 2026-08-28
- **Goal:** distinguish a genuinely new pocket geometry from a known apo pocket
  that has no prior ligand-bound training analogue.
- **Motivating example:** `37HF` has highly familiar PCSK9 structures and an
  AZD0780-compatible pocket across multiple apo structures, but no eligible
  pre-cutoff ligand occupied that pocket. The current ligand-bound scorer
  therefore reports no usable training analogue.
- **Possible approach:** compare the released query pocket with pre-cutoff
  ligand-free protein structures after structural alignment, calibrate an
  independent threshold, and report it alongside—not in place of—the canonical
  protein-frame overlap and RnP-style ligand/pocket metrics.
- **Important distinction:** report protein familiarity, apo-pocket familiarity,
  and ligand-bound-system familiarity separately.
