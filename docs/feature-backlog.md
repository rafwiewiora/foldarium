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

## Planned

### Five featured questions per week

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** offer a small default human quiz with optional exploration of every
  question. Keep the full prediction, selector, and benchmark populations.
- **Prepared:** the browser accepts a manifest-bound featured selection,
  preserves full question ordinals for votes and recordings, restores the same
  selection on refresh, and summarizes five saved votes without claiming a
  complete full-round ballot. Existing sessions resume with all questions.
- **Prepared:** deterministic uniform selection of up to five questions, optional
  bounded interestingness weighting, immutable private audit artifacts, and the
  manifest-bound public database marker. The score uses blind pose-cluster
  diversity and cross-method disagreement; missing novelty data does not block
  selection. Selection never shrinks the full benchmark or Selector kit.
- **Deployed:** database registration and browser source; selection activation
  remains gated. The default remains uniform; scores explain the candidate pool.
- **Pending:** validate the complete flow before enabling the new selection gate.

### Immutable artifact upload recovery

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** survive transient Storage service failures while preserving exact
  scientific artifacts and publication integrity.
- **Prepared:** bounded retries of the same bytes at the same content address,
  with SHA verification after ambiguous success. Permanent failures and digest
  conflicts fail closed; RPCs and mutable writes do not retry.
- **Pending:** deployment and live recovery verification.

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
- **Deployed:** database migration and operational execution adapter, with all
  new gates disabled.
- **Pending:** live verification and deliberate gate activation. The public tree
  supplies the contract and tests, without a scheduler, deployment configuration,
  credentials, or provider launcher.

### Automatic cofolding benchmark refresh

- **Status:** planned
- **Added:** 2026-10-04
- **Goal:** refresh method rankings and weekly trends from verified, published
  retrospective evaluations whenever a new round is published.
- **Prepared:** a read-only aggregate API and browser integration replace the
  static method-statistics file. Counts cover published quiz questions with
  method poses; oracle and highest-ligand-pLDDT success use raw RMSD below 1.5 Å.
  Missing ligand confidence excludes a target from top-1 only.
- **Pending:** production deployment and live verification. Full unpublished
  prediction campaigns are outside this aggregate's population.

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
- **Pending:** confirm end-to-end runtime assembly behavior in the new round.

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
