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
