# Weekly LLM voting runbook

## Purpose

This runbook produces reproducible, blind, complete dual-mode ballots for the
**next Weekly target**. It covers LLM participants and the deterministic Smina
participant from kit freeze through post-reveal scoring.

Do not use this procedure on an in-flight round that was not prepared for the
v2 contract. Do not deploy, register a live kit, issue production tokens, or
submit production ballots as part of a dry run. Contract semantics are defined
in [Weekly selection semantics](weekly-selection-semantics.md), and endpoint
shapes are defined in [Weekly Selector API](weekly-selector-api.md).

## Roles and required records

Use separate named roles where practical:

- **Round operator:** prepares and freezes the blind manifest and kit.
- **Method operator:** freezes model, prompt, tools, and inference configuration.
- **Submitter:** validates and submits the complete batch.
- **Reviewer:** independently checks digests, receipt, blindness, and historical
  scope resolutions.
- **Reveal operator:** verifies reveal artifacts and runs post-reveal scoring.

Create an append-only run record before work starts. It must contain:

- run ID and UTC timestamps;
- deployment environment and round ID;
- source revision/deployment identifiers;
- blind-manifest, kit, and optional ZIP-container SHA-256 values;
- item and choice counts;
- provider, model, and exact version;
- prompt, tool, and canonical config SHA-256 values;
- the full structured blindness attestation, canonical attestation SHA-256, and
  the reviewed network allowlist artifact/digest;
- token ID (never the raw token), expiry, and revocation state;
- submission UUID, revision, payload digest, and receipt;
- operator/reviewer identities and approvals;
- all aborts, retries, supersessions, and incident references; and
- after reveal, reveal-manifest digest and scorer/policy versions.

Store raw tokens and service credentials in an approved secret store, not in the
run directory, shell history, chat, logs, or screenshots.

## 1. Preflight the next target

1. Confirm the intended target is the next Weekly round prepared for Selector
   v2, not the current legacy round or a previously revealed target.
2. Resolve and record the trusted deployment environment. Check that API,
   database, storage, and intended token issuance all resolve to that same
   environment.
3. Record `round_id`, `opens_at`, `closes_at`, item count, and round status from
   the authoritative service.
4. Confirm the round has not opened if kit publication is still being prepared.
   Once published/open, candidate, clustering, target, or policy changes require
   a new round/version.
5. Confirm system clocks are UTC-synchronized and leave enough time to validate,
   submit, and verify a receipt before `closes_at`.
6. Confirm the v2 API/schema, Python client, JavaScript contract, and Rust/WASM
   mapper pass the same canonical fixtures.
7. Verify the operator cannot access production service credentials during a
   Preview rehearsal unless explicitly required and approved.

Abort on any ambiguous target, environment mismatch, legacy schema, changed
round content, or insufficient receipt-verification window.

## 2. Freeze and verify the blind kit

1. Build the kit from the immutable promoted blind manifest and normalized model
   inputs before the voting window.
2. Verify that each item has unique immutable item IDs, raw choice IDs, and
   advertised cluster IDs. Record representative flags only as display metadata.
3. Ensure the kit contains only approved blind material: normalized target
   inputs, candidate pose/protein/pocket assets, cluster mappings, public
   descriptors, schemas, clients, and per-file checksums.
4. Scan recursively for forbidden content: reference or released coordinates,
   reveal manifests, correctness, RMSDs, vote totals, private indexes, private
   run/sample IDs, answer labels, and unapproved external links.
5. Generate the deterministic kit and run the reference verifier. Repeat the
   build in a clean workspace and require the same canonical `kit_sha256`.
6. Register the descriptor once. Fetch it through the public API and require
   exact agreement on environment, round, blind-manifest digest, kit digest,
   item count, byte size, and storage identity.
7. Download the registered object into a clean inference workspace and verify
   every file digest with the bundled/reference v2 client.
8. Record the ZIP-container SHA-256 separately if required. Do not compare it to
   `kit_sha256` unless the kit schema explicitly defines them as the same digest.

Example workflow after the v2 client is available:

```bash
python client/foldarium_selector_client.py verify weekly-selector-kit.zip
python client/foldarium_selector_client.py template weekly-selector-kit.zip \
  > items.template.json
```

Never hand-edit `manifest.json`, candidate files, or checksums after the freeze.
A changed file invalidates the run and requires rebuilding and re-registering a
new round/version before open.

## 3. Freeze method provenance

Start from the exact registered prompt profile in the verified kit. Do not
rewrite, summarize, prepend to, or otherwise customize its system prompt, item
template, or response schema. Create a read-only method directory containing:

1. the exact provider name, model name, provider model/version identifier, API
   version, and any pinned region/runtime;
2. `prompt_profile_id`, the exact bundled prompt-profile bytes, and each
   rendered item request;
3. canonical tool declarations and every local helper executable or source file
   available during inference;
4. canonical JSON configuration containing temperature, top-p, seed when
   supported, maximum tokens, stop sequences, reasoning/tool settings,
   concurrency, timeout, retry policy, and provider-specific options; and
5. runtime/container/lockfile digests needed for reproduction.

Compute and review:

```text
prompt_sha256 = registered prompt profile digest from the verified kit
tools_sha256  = SHA256(canonical tool manifest or frozen tool bundle)
config_sha256 = SHA256(canonical JSON inference configuration)
```

Record a SHA-256 for every rendered item request and a canonical manifest root
over those request digests. The registered profile digest remains stable across
rounds; the rendered request digests bind the profile to the exact item evidence.

Do not use a marketing alias such as “latest” as `model_version`. If the
provider cannot report or pin a meaningful version, record the returned model
identifier and response metadata exactly, mark the limitation, and require
explicit reviewer approval before Preview. An unannounced provider version
change creates a new method identity and run.

## 4. Establish the blindness boundary

Use a new workspace containing only the verified kit and frozen method
artifacts. Clear or disable shared retrieval caches. Prefer no network access
for inference; if the provider API requires network access, allowlist only the
provider endpoint and required authentication service.

Before inference, construct
`foldarium.selector-blindness-attestation/v1` with
`workspace_policy: "verified-kit-only"` and either:

- `network_policy: "none"` with
  `network_allowlist_sha256: "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"`
  (the SHA-256 of canonical JSON `[]`); or
- `network_policy: "provider-api-only"` with the SHA-256 of the canonical
  non-empty, sorted, duplicate-free JSON array used by the reviewed
  provider/auth network control.

Set `browser_enabled`, `web_search_enabled`, `external_retrieval_enabled`, and
`shared_cache_enabled` to the JSON boolean `false`. Preserve the exact
attestation and allowlist artifact read-only. If any capability is needed or
the enforced network boundary cannot be matched to the attestation, abort; do
not weaken, omit, or encode a capability as a string.

The model and its tools must not:

- fetch released/reference structures or reveal data;
- search public identifiers, target names, ligands, or sequence fragments for
  an answer;
- query previous votes, results, leaderboards, or operator notes;
- read private pipeline indexes, run IDs, sample IDs, or scoring outputs not in
  the kit; or
- receive hints derived from any of those sources.

Preserve request/response metadata needed for audit under the approved privacy
policy. Reasoning traces are not required by this contract and must not be
collected or published unless policy explicitly permits it.

## 5. Issue a round-bound token

After method artifacts are frozen:

1. Authenticate as the intended account with a Supabase access token.
2. Request a Selector token using the exact environment, round ID, display name,
   provider/model/version, prompt/tool/config digests, and complete structured
   blindness attestation.
3. Verify the response repeats the intended round/environment and that
   `expires_at` is after the planned submission but no later than permitted
   policy.
4. Place the one-time plaintext token in the approved secret store.
5. Record only token ID/metadata in the run record and confirm the backing store
   contains only its hash.

Use a distinct token for every round/method-provenance identity. Revoke and
replace a token after suspected disclosure; never reuse a token across
Preview/production or across rounds.

## 6. Run inference and construct both modes

Process the complete kit under the frozen method. For every item, solicit and
record two independent decisions:

- clustered: one advertised `cluster_id`, or explicit `none`;
- unclustered: one advertised raw `choice_id`, or explicit `none`.

The prompt must explain that:

- cluster and exact modes are independent;
- a representative pose ID is not an exact vote unless selected explicitly in
  unclustered mode;
- no selection may be inferred from the other mode;
- `none` is an explicit abstention, not a missing value; and
- only IDs in the frozen kit are valid.

Map provider output into the strict tagged-union schema mechanically. Do not
repair an invalid ID by fuzzy matching, infer a cluster from an exact choice,
infer an exact choice from a cluster representative, or replace missing output
with `none`. Retry only under the frozen retry policy. If complete valid output
cannot be obtained without changing prompt/tools/config, abort that method run
and create new provenance before retrying.

Use one newly generated submission UUID for the normalized complete snapshot.
Keep raw provider output separate from submission JSON; explanations,
confidence, and reasoning are not ballot fields.

## 7. Generate the deterministic Smina ballot

Smina uses no prompt or answer material. For every item:

1. verify every advertised raw choice has exactly one finite affinity produced
   by the pinned score-only protocol;
2. verify scoring-function, binary version/digest, configuration, protein and
   ligand input digests, and kit choice identity;
3. sort by numeric affinity ascending, then immutable `choice_id` ascending;
4. choose the first raw choice as the unclustered exact decision; and
5. use that choice's advertised `cluster_id` as the clustered decision.

Lower/more-negative affinity is better (`-8.0` precedes `-7.0`). Numeric ties
are resolved only by immutable choice ID. Never use input order, cluster
representative status, method name, or mutable display label as a tie-break.

If any item has a missing, duplicate, non-finite, stale, or mismatched score,
abort the entire Smina batch. Do not emit `none` and do not score a partial
Smina participant.

## 8. Validate the complete batch offline

Run at least two independent v2 validators (for example Python and Rust/WASM)
against the downloaded kit. Require byte-for-byte agreement on normalized
canonical JSON and `payload_digest`.

Validation must establish:

- exact schema version and strict key sets;
- submission UUID;
- environment, round, blind-manifest digest, and kit digest;
- exactly one row for every manifest item, with no extras or duplicates;
- one valid tagged decision in each mode for every item;
- all cluster and raw choice IDs occur in that same item;
- no forbidden/reveal fields or embedded coordinate arrays;
- finite, canonical JSON and payload size; and
- identical digest across implementations.

Review a machine-generated summary of decision counts, but do not add names or
display labels to the ballot. Save the normalized payload read-only.

Example workflow:

```bash
python client/foldarium_selector_client.py build weekly-selector-kit.zip \
  --submission-id "$SUBMISSION_ID" \
  --items-json decisions.json > submission.json
python client/foldarium_selector_client.py validate \
  weekly-selector-kit.zip submission.json
python client/foldarium_selector_client.py digest \
  weekly-selector-kit.zip submission.json
```

## 9. Submit and verify the receipt

1. Recheck round status and time immediately before submission.
2. POST the frozen normalized JSON with the round-bound Selector bearer token.
3. Require `201` for a new revision or `200` only for a verified idempotent
   retry.
4. Compare receipt submission ID, environment, round, blind-manifest digest,
   kit digest, and payload digest to the local record.
5. Fetch the receipt independently with the same token and require exact
   agreement.
6. Record the server revision and acceptance timestamp. Confirm the timestamp is
   before close.

For a network timeout or lost response, fetch the receipt by submission UUID.
If found and matching, record success. If not found, retry the exact same bytes
and UUID. Never alter content under the same UUID.

To revise before close, produce another complete validated snapshot with a new
submission UUID. Submit it, require the next revision number, and verify that
the latest projection points to it. Never delete or overwrite the earlier
revision. No revisions are accepted at or after `closes_at`.

### Catch-up runs after close

Never replay a missed model run through the pre-close token/submission path.
While the round is closed and still unrevealed, wrap the same complete v2
payload in `foldarium.selector-post-close-benchmark/v1` and submit it to the
server-only `/benchmarks` endpoint. Set `run_class` exactly to
`post_close_benchmark`, bind the canonical prompt and all execution digests,
record exactly one observed model identifier, and set
`reasoning_trace_retained` to `false`.

Use the audited runner to verify the kit, render deterministic evidence and
images, score one item at a time, validate every provider response, and emit
both a private execution bundle and canonical benchmark JSON. By default the
runner is artifact-only and performs no network submission.

```bash
python pipeline/scripts/weekly_llm_score.py preflight-claude
python pipeline/scripts/weekly_llm_score.py preflight-cursor
python pipeline/scripts/weekly_llm_score.py list-cursor-models

python pipeline/scripts/weekly_llm_score.py run weekly-selector-kit.zip \
  --output-dir /secure/run/out \
  --provider fake \
  --fake-fixture pipeline/tests/fixtures/weekly_llm_fake_provider.json \
  --execution-id "$EXECUTION_ID"

python pipeline/scripts/weekly_llm_score.py run weekly-selector-kit.zip \
  --output-dir /secure/run/out \
  --provider claude \
  --network-allowlist /secure/reviewed/provider-allowlist.json \
  --assert-provider-egress-enforced

python pipeline/scripts/weekly_llm_score.py run weekly-selector-kit.zip \
  --output-dir /secure/run/out \
  --provider cursor \
  --network-allowlist /secure/reviewed/provider-allowlist.json \
  --assert-provider-egress-enforced
```

Optional submission uses `FOLDARIUM_SELECTOR_BENCHMARK_URL` and
`FOLDARIUM_SELECTOR_BENCHMARK_TOKEN` environment variables only. Retries must
reuse the same execution UUID and exact benchmark bytes. Deliberate reruns use a
new UUID and set `--supersedes-execution-id` to the prior execution.

For Claude's first pass, request `default` effort by omitting the CLI effort
flag. If the CLI does not report applied effort, record
`effort_reporting: "not_exposed"` and `applied_effort: null`; never infer it.
For Sol, request high effort and record the provider-reported applied value when
available. Abort either run if more than one observed model identifier appears.

The benchmark execution UUID must equal the enclosed payload submission UUID.
An exact retry reuses that UUID and bytes. A deliberate rerun uses a new UUID
and names the prior execution in `supersedes_execution_id`. These records remain
separate from official pre-close votes in storage, scoring, and presentation.

## 10. Close, reveal, and score

After close:

1. Freeze the accepted revision inventory and verify there is at most one latest
   revision per identity/round.
2. Do not expose ballots, names, or aggregate results while the round remains
   blind.
3. Publish the reveal only after reference coordinates, evaluation artifacts,
   blind-manifest binding, and reveal-manifest digest pass the normal reveal
   checks.
4. Verify every revealed raw choice maps to the same immutable item and cluster
   advertised in the blind kit.
5. Score the latest accepted pre-close revision separately:
   - exact: selected raw choice against raw exact correctness;
   - cluster: selected cluster against the versioned cluster acceptance policy;
   - none: retained as an abstention in that mode.
6. Include Smina only if its full deterministic run passed.
7. Recompute results independently and compare totals, names, item counts, and
   exact/cluster scores before publication.
8. Publish only sanitized post-reveal results. Keep Selector submissions
   distinct from manual `weekly_quiz_votes`.

If a reveal is delayed, withdrawn, or invalid, keep results unavailable. Do not
substitute another reference or silently remove an item from an immutable round.

## Historical Claude Opus/Codex resolution

The historical evidence consists of the frozen versions of
`local/build_llm_vote_packets.py`, `local/validate_llm_ballots.py`, generated
packet/ballot artifacts, and the source round manifest. Hash each artifact and
store a canonical evidence-manifest digest in the reviewed resolution record.
Do not invent a digest in documentation; the ledger must contain the digest of
the exact evidence actually reviewed.

The reviewed procedure is cluster-scoped:

- the packet builder grouped candidates by `cluster_id`;
- each cluster card exposed a representative `vote_choice_id`;
- instructions requested one cluster card's representative ID or null; and
- the validator accepted only the representative-ID set or `picked_none`.

Therefore resolve each valid historical Claude Opus/Codex row as `cluster` or
`none`, never `exact`. Before applying a resolution:

1. freeze and hash the source vote row and all evidence;
2. verify source and proposed target round, item ID, representative-to-cluster
   mapping, and manifest digest;
3. record source/target round IDs, frozen row fingerprint, evidence digest,
   resolver, reason, and timestamp;
4. require an independent reviewer;
5. write the append-only resolution attestation and projection provenance in
   one transaction; and
6. verify totals and names are unchanged except for explicit scope.

Corrections supersede an earlier ledger row; they never edit it. Keep
`scope unknown` for ambiguous evidence and for every historical procedure not
proven equivalent. Require zero unresolved rows for the intended target round
before removing that warning from its retrospective.

## Abort criteria

Abort the affected complete run immediately if any of the following occurs.

### Round or kit

- round/environment is ambiguous or differs anywhere in the chain;
- blind-manifest, kit, file, or storage-object digest mismatch;
- item/choice/cluster membership changes after freeze;
- forbidden answer/reveal/private content appears in the kit;
- the target has already revealed, or was not prepared for v2; or
- the kit cannot be rebuilt/verified deterministically.

### Provenance or blindness

- provider/model/version is missing, mutable, or changes mid-run;
- prompt/tool/config bytes differ from their registered digest;
- an unregistered tool, retrieval source, cache, or operator hint is used;
- the stored blindness attestation or its canonical digest differs from the
  reviewed inference workspace/network control;
- reference/reveal data, external answer lookup, or prior vote/result leakage is
  possible;
- a raw token or credential appears in logs or an unapproved location; or
- historical source/target scope or evidence digest cannot be reviewed.

### Decisions or Smina

- any item or mode is missing, duplicated, extra, inferred, or invalid;
- a representative ID is treated as exact without a separate exact decision;
- validators disagree on normalization or payload digest;
- Smina has any missing/non-finite/mismatched score or unpinned runtime; or
- a partial batch would be required.

### Submission, receipt, or reveal

- token is expired, revoked, or bound to another round/environment;
- close is reached before a matching receipt is persisted;
- receipt fields/digest differ from local canonical payload;
- revision ordering or latest projection is ambiguous;
- reveal digest, mapping, or scorer policy does not match the frozen round; or
- post-reveal independent recomputation disagrees.

## Recovery after abort

Preserve the failed run, hashes, logs, and incident reason read-only. Revoke
affected tokens. Do not patch accepted revisions, manifests, kits, or resolution
ledger rows.

If only transport failed and a matching receipt exists, document recovery
without resubmitting changed content. If method artifacts changed, create a new
method identity/token and rerun every item. If blind round content changed
before open, create a new round/version and kit. If leakage may have occurred,
discard all affected decisions and rerun from a clean workspace after review.

Preview rehearsal and reviewer sign-off are required again after any
contract-affecting incident.

## Presentation and unresolved policy

Visual parity review is separate from this runbook. It verifies that manual
Weekly voting remains unchanged and that post-reveal cluster, pose, `None`, and
correct-answer labels/styles are accurate. It cannot repair or redefine stored
selection provenance.

Before production, record decisions for the open policies listed in
[Weekly selection semantics](weekly-selection-semantics.md), especially
leaderboard/abstention reporting, token lifetime, retention, participant limits,
and post-reveal provenance disclosure. A policy decision receives a version and
effective round; it is never applied retroactively without an explicit audited
migration.
