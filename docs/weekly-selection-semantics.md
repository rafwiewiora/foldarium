# Weekly selection semantics

## Status and scope

This document records the approved conceptual contract for the external Weekly
Selector. It targets the **next Weekly target** that is prepared under this
contract. It must not be retrofitted silently onto an open or already revealed
round. A round prepared with different clustering, candidates, target inputs, or
public metadata is a different immutable round and requires a newly generated
kit.

The Selector is an additional batch participant. It does not replace or reinterpret
the interactive Weekly vote flow. API details are in
[Weekly Selector API](weekly-selector-api.md), and the reproducible operating
procedure is in [Weekly LLM voting runbook](weekly-llm-voting-runbook.md).

## Settled conceptual choices

### One complete batch, two decisions per item

A submission covers exactly the item set in one round's blind manifest. Every
item contains two independent decision modes:

- **Clustered:** select an advertised `cluster_id` with
  `selection_kind = "cluster"`, or explicitly abstain with
  `selection_kind = "none"`.
- **Unclustered:** select one advertised raw `choice_id` with
  `selection_kind = "exact"`, or explicitly abstain with
  `selection_kind = "none"`.

The enclosing mode disambiguates the two `none` decisions. A participant may
choose a cluster but abstain from exact selection, choose an exact pose but
abstain from cluster selection, or abstain in both modes. The exact choice does
not have to belong to the selected cluster; the modes are independent
measurements and must not be repaired or inferred from one another.

A cluster representative is only a display and clustering aid. Selecting its
raw `choice_id` is not evidence of an exact-pose decision unless the participant
separately submitted that ID in unclustered mode. Conversely, an exact choice
must not be converted automatically into a clustered choice.

Submissions are complete snapshots, not streams of per-item votes. Missing,
duplicate, or extra items, unknown identifiers, omitted mode objects, or
implicit defaults invalidate the entire batch. Validation and persistence are
atomic: either the complete batch becomes one revision or none of it does.

### Immutable round and artifact binding

The authoritative candidate universe is the immutable blind manifest. Its
canonical SHA-256 digest binds item IDs, raw choice IDs, cluster IDs, and public
blind metadata to the round.

The deterministic selector kit has its own SHA-256 digest and includes the
blind-manifest digest. The kit may contain normalized target inputs, public
candidate coordinates and descriptors, cluster mappings, checksums, schemas,
and offline clients. It must not contain reference coordinates, reveal
manifests, RMSDs, correctness, vote totals, or private run/sample identifiers.

Every accepted batch is bound to all of:

- deployment environment;
- round ID;
- blind-manifest SHA-256;
- kit SHA-256;
- exact manifest item set;
- participant/method provenance; and
- canonical payload SHA-256.

Published content is never edited in place. Any candidate, cluster, target,
policy, or kit-content change requires a new round or explicitly versioned
replacement before voting starts.

### Method provenance

Each selector identity records a human-readable display label and immutable
method provenance:

- provider, model name, and exact model/version identifier;
- SHA-256 of the exact prompt bytes;
- SHA-256 of the tool bundle or tool declaration;
- SHA-256 of canonical inference configuration; and
- any additional runtime/container artifact digests required to reproduce the
  method.

Changing any of these creates a distinct method identity or version. Provenance
belongs to token issuance and the server-side identity record, not to
participant-editable ballot fields.

### Authentication, revisions, and receipts

Selector bearer tokens are bound to one environment and one round. They expire,
can be revoked, are returned in plaintext only at issuance, and are stored only
as cryptographic hashes. A token for another round or environment cannot read a
receipt or submit a ballot.

Accepted revisions are append-only. A later complete batch from the same
identity and round becomes a new revision and the latest accepted pre-close
revision becomes scoreable; earlier revisions remain auditable. Retrying the
same submission ID with the identical canonical payload is idempotent. Reusing
that ID for different content is a conflict.

The server returns a receipt containing the submission identity, revision,
round/environment binding, canonical payload digest, and acceptance timestamp.
A run is not complete until the caller verifies the receipt against its local
canonical payload.

### Reveal and scoring

No correctness or selector leaderboard is computed or exposed before the round
is revealed. After reveal, the latest accepted pre-close revision is scored in
two separate tracks:

- **Exact score:** an `exact` decision is correct only when that raw choice is
  correct under the round's revealed exact-pose evaluator.
- **Cluster score:** a `cluster` decision is correct when the revealed
  acceptance policy marks that advertised cluster correct (currently, when at
  least one raw member is exact-correct).
- **None:** remains an explicit abstention in its own mode and is never
  reclassified as a pose or cluster.

Results and retrospective displays preserve the distinction among cluster
labels, raw pose labels, and `None`. Names and counts stay attached to the mode
actually submitted.

### Deterministic Smina player

`Smina` is a synthetic, reproducible participant, not a hidden answer oracle.
For each item it:

1. requires one finite advertised Smina affinity for every raw choice;
2. chooses the numerically lowest (most negative) affinity;
3. resolves an exact numeric tie by the immutable raw `choice_id` ordering;
4. submits that raw pose in unclustered mode; and
5. submits the pose's advertised cluster in clustered mode.

Missing, non-finite, mismatched, or incompletely provenance-bound scores abort
the Smina batch. They do not produce `none`. The scoring protocol, Smina binary
version/digest, configuration, and input digests are part of its method
provenance.

## Historical Claude Opus and Codex ballots

The historical procedure was cluster-scoped:

- `local/build_llm_vote_packets.py` grouped raw choices by `cluster_id`, emitted
  one cluster card per group, and instructed the model to return the card's
  representative `vote_choice_id` or null.
- `local/validate_llm_ballots.py` accepted only those representative IDs and a
  `picked_none` null. It did not solicit or validate an independent raw-pose
  choice.

Consequently, reviewed Claude Opus and Codex ballots produced by that procedure
may be resolved only as `cluster` or `none`, never as `exact`. The representative
ID is evidence for cluster identity, not evidence for raw-pose intent.

Before a historical vote is attributed to a round, scope resolution must be
audited against frozen evidence. The append-only resolution record must retain
the source vote fingerprint, source and target round IDs, resolver and reviewer,
evidence digest, decision and reason, timestamp, and any supersession link.
Resolution and projection updates must be transactional. A retrospective must
continue to show `scope unknown` until every intended target-round ballot has a
reviewed resolution; ambiguous rows stay unresolved.

## Blindness and leakage boundary

Selector preparation and inference must not access reference/released
coordinates, reveal data, correctness, RMSDs, private pipeline IDs, prior vote
totals, or external sources that disclose the target answer. Public identifiers
must not be used to look up a reference. Prompts, tool outputs, logs, caches, and
retrieval sources are all inside this boundary.

Only the frozen kit and explicitly approved blind public material may influence
a selection. Discovery of possible leakage requires stopping the affected run,
revoking its token, preserving evidence, and starting a reviewed clean run; a
post hoc claim that leaked material did not affect the answer is insufficient.

## Separate concerns

### Visual parity

Visual parity is a presentation requirement, not a selection semantic. Existing
Weekly interaction behavior and incorrect-card styling remain unchanged. A
retrospective may add clearer cluster, pose, `None`, and correct-answer styling,
but CSS or label changes cannot alter vote provenance, scoring, scope, or the
candidate universe.

### Open policy questions

The following must be decided and versioned before production reporting, but
must not be guessed by implementation or UI work:

- leaderboard ranking and tie-display policy;
- whether abstentions affect a published percentage denominator, while still
  remaining explicit `none` decisions;
- token lifetime, per-identity issuance limits, and revocation administration;
- retention and publication policy for prompts, tool bundles, inference logs,
  and model reasoning;
- participant eligibility, rate limits, and the number of methods allowed per
  authenticated account; and
- how much method provenance and per-question naming is public after reveal.

These choices may change reporting or operations. They do not change the
settled dual-mode ballot meaning, immutable bindings, leakage boundary, or
historical cluster-scope conclusion above.
