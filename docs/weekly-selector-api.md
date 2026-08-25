# Weekly Selector API

## Contract status

This is the normative API contract for the **next Weekly target** operated under
the dual-mode design. The recovered v1 implementation uses nullable
`choice_id`/`cluster_id` fields and tokens that are not yet round-bound; that is
not sufficient for this contract. Production must remain disabled until the
implementation, database contract, offline clients, and cross-language fixtures
all implement the strict v2 semantics below.

The conceptual meaning of each decision is defined in
[Weekly selection semantics](weekly-selection-semantics.md). The operational
sequence is in [Weekly LLM voting runbook](weekly-llm-voting-runbook.md).

## General rules

- Requests and responses use UTF-8 JSON unless the kit endpoint returns a
  download location.
- Unknown object keys are rejected. IDs are opaque and case-sensitive.
- SHA-256 values are 64 lowercase hexadecimal characters.
- Timestamps are UTC RFC 3339 values.
- Every response uses `Cache-Control: no-store`.
- The deployment environment is one of `production`, `preview`, or
  `development`; it is never inferred from participant input alone.
- Submission JSON is bounded (65,536 bytes in the initial contract).
- Reference/reveal fields, coordinates embedded in JSON, correctness, RMSDs,
  private run/sample IDs, and other forbidden answer material are rejected
  recursively.

## Resources and endpoints

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/weekly-selector/docs` | none | Machine-readable contract versions |
| `GET` | `/api/weekly-selector/rounds/current` | none | Next/open Weekly round and immutable kit descriptor |
| `GET` | `/api/weekly-selector/kits/{round_id}` | none | Verified kit download metadata |
| `POST` | `/api/weekly-selector/tokens` | Supabase access token | Register provenance and issue a round-bound selector token |
| `POST` | `/api/weekly-selector/submissions` | selector bearer token | Atomically accept one complete batch revision |
| `GET` | `/api/weekly-selector/submissions/{submission_id}` | selector bearer token | Retrieve and verify an owned receipt |
| `GET` | `/api/weekly-selector-results?round_id={round_id}` | none, after reveal | Sanitized exact/cluster results |

The API must resolve its environment from trusted deployment configuration and
pass it to every environment-scoped database operation. A request cannot
override that configuration to cross environments.

## Round and kit

`GET /rounds/current` returns only a round prepared for this selector contract:

```json
{
  "schema_version": "foldarium.weekly-selector-round/v2",
  "environment": "preview",
  "round_id": "weekly-2026-09-05",
  "public_status": "open",
  "opens_at": "2026-09-05T03:00:00Z",
  "closes_at": "2026-09-09T00:00:00Z",
  "item_count": 8,
  "blind_manifest_sha256": "…64 lowercase hex…",
  "kit": {
    "schema_version": "foldarium.weekly-selector-kit/v2",
    "kit_sha256": "…64 lowercase hex…",
    "byte_size": 123456,
    "created_at": "2026-09-04T20:00:00Z"
  }
}
```

The descriptor's round, environment, item count, and blind-manifest digest must
match the authoritative round row. The registered kit digest and storage object
are immutable. Registration is idempotent only when every descriptor field and
the stored object are identical; conflicting registration fails.

`GET /kits/{round_id}` returns the verified descriptor and a time-bounded or
otherwise approved download URL. A downloaded archive is trusted only after the
bundled/reference verifier confirms:

1. schema and round/environment binding;
2. `blind_manifest_sha256`;
3. canonical `kit_sha256`;
4. every listed file path, size, and SHA-256;
5. exact item/choice/cluster uniqueness; and
6. absence of forbidden answer material.

The kit digest is the digest defined by the kit schema over its canonical
manifest content; it must not be confused with an optional SHA-256 of the ZIP
container bytes.

## Provenance registration and token issuance

`POST /tokens` requires an authenticated Supabase user and a strict body:

```json
{
  "schema_version": "foldarium.weekly-selector-token-request/v2",
  "round_id": "weekly-2026-09-05",
  "environment": "preview",
  "display_name": "Claude Opus",
  "provider": "anthropic",
  "model": "claude-opus",
  "model_version": "exact-provider-version",
  "prompt_sha256": "…64 lowercase hex…",
  "tool_sha256": "…64 lowercase hex…",
  "config_sha256": "…64 lowercase hex…"
}
```

Provider, model, version, and all three digests are required. Digests refer to
frozen artifacts:

- `prompt_sha256`: exact prompt bytes, including system/developer text and
  templates supplied by the operator;
- `tool_sha256`: canonical tool declarations plus any executable/helper bundle
  available to the model; and
- `config_sha256`: canonical JSON for inference parameters and runtime settings.

The server verifies that the requested environment equals the trusted
deployment environment and that the round is the prepared next/open round. It
stores the normalized identity/provenance and a SHA-256 hash of a
cryptographically random token. The raw token is returned exactly once:

```json
{
  "schema_version": "foldarium.weekly-selector-token/v2",
  "token": "one-time-plaintext-secret",
  "token_type": "Bearer",
  "environment": "preview",
  "round_id": "weekly-2026-09-05",
  "expires_at": "2026-09-09T00:00:00Z"
}
```

The persisted token record includes round, environment, identity, issue/expiry
times, and revocation state, but never plaintext token material. Token lookup
hashes the presented bearer token and uses constant-time comparison where
comparison occurs in application code. Expired, revoked, wrong-round, or
wrong-environment tokens fail closed.

## Strict v2 submission

The strict top-level shape is:

```json
{
  "schema_version": "foldarium.selector-submission/v2",
  "submission_id": "1d445da0-9b3e-4f26-9021-28c7ea96818a",
  "environment": "preview",
  "round_id": "weekly-2026-09-05",
  "blind_manifest_sha256": "…64 lowercase hex…",
  "kit_sha256": "…64 lowercase hex…",
  "items": [
    {
      "item_id": "item-01",
      "clustered": {
        "selection_kind": "cluster",
        "cluster_id": "cluster-a"
      },
      "unclustered": {
        "selection_kind": "exact",
        "choice_id": "pose-a-2"
      }
    },
    {
      "item_id": "item-02",
      "clustered": {
        "selection_kind": "none"
      },
      "unclustered": {
        "selection_kind": "none"
      }
    }
  ]
}
```

The allowed mode objects are exact tagged unions:

```text
clustered   = {"selection_kind":"cluster","cluster_id":<advertised ID>}
            | {"selection_kind":"none"}

unclustered = {"selection_kind":"exact","choice_id":<advertised ID>}
            | {"selection_kind":"none"}
```

No nullable or omitted shorthand is accepted. `cluster_id` is forbidden on
`none`; `choice_id` is forbidden on `none`; a representative choice ID is not a
cluster ID. The item array must contain exactly one entry for every blind
manifest item and no others. Item order is normalized by immutable `item_id`
before canonical digesting.

The client computes:

```text
payload_digest = SHA256(canonical_json(normalized_submission))
```

Canonical JSON uses sorted object keys, compact separators, UTF-8, finite JSON
values, and the schema's fixed item ordering. The API independently normalizes,
validates, and computes the digest; a caller-provided digest is never trusted
without recomputation.

## Atomic acceptance, revisions, and retries

`POST /submissions` validates, in one transaction:

- bearer token hash, expiry, revocation, environment, round, and identity;
- round status and `opens_at <= accepted_at < closes_at`;
- submission UUID and strict key sets;
- environment, round, blind-manifest digest, and kit digest;
- exact complete item set;
- each advertised cluster/exact ID;
- forbidden/leak-prone fields and payload size; and
- canonical payload digest.

No item row or latest projection is written unless all checks pass. For one
identity and round:

- a first accepted complete payload is revision 1;
- a new submission UUID with a different complete payload appends revision 2,
  and so on;
- the latest accepted pre-close revision is updated in the same transaction;
- all prior revisions remain immutable;
- retrying the same UUID, token identity, and canonical payload returns the
  original receipt with `idempotent: true`; and
- reusing a UUID for a different payload or identity returns `409 Conflict`.

Revision allocation is serialized per identity/round. A failure or timeout with
an unknown outcome is resolved by fetching the receipt; the client must not
invent success or submit a modified payload under the same UUID.

An acceptance response is `201 Created` for a new revision or `200 OK` for an
idempotent retry:

```json
{
  "schema_version": "foldarium.weekly-selector-receipt/v2",
  "submission_id": "1d445da0-9b3e-4f26-9021-28c7ea96818a",
  "revision_number": 1,
  "environment": "preview",
  "round_id": "weekly-2026-09-05",
  "blind_manifest_sha256": "…64 lowercase hex…",
  "kit_sha256": "…64 lowercase hex…",
  "payload_digest": "…64 lowercase hex…",
  "submitted_at": "2026-09-06T12:34:56Z",
  "idempotent": false
}
```

`GET /submissions/{submission_id}` requires a token for the same identity,
round, and environment and returns the persisted receipt. It never returns the
ballot, token hash, user ID, or private identity fields.

## Errors

The API returns generic public errors and logs a correlation-safe internal
reason without secrets or ballot content:

- `400` malformed or contract-invalid input;
- `401` missing, invalid, expired, revoked, or incorrectly bound credential;
- `404` unavailable round/kit or receipt not owned by the token identity;
- `409` immutable ID/content conflict;
- `413` oversized request;
- `415` unsupported media type;
- `422` optional for a syntactically valid but semantically incomplete batch;
- `429` issuance/submission rate limit; and
- `502` unavailable or invalid upstream state.

Validation errors must not reveal which hidden answer field or candidate would
have been correct.

## Post-reveal results

The results endpoint is unavailable until the authoritative round status is
`revealed` and the reveal manifest and its digest have been verified against the
same blind manifest and kit catalog record. It reads only the latest accepted
pre-close revision per identity/round.

The response reports exact and cluster scoring separately, with per-item
`cluster`, `exact`, and `none` counts/names in the corresponding mode. It may
include the deterministic Smina participant only when its complete provenance
and input checks pass. It must not expose user IDs, token data, unrevealed
rounds, private pipeline identifiers, prompt contents, reasoning traces, or
superseded ballots.

Historical ballots with unresolved scope remain labeled `scope unknown`; the
results API must not infer their mode from a representative pose ID.

## Security and data handling

- Raw selector tokens, Supabase credentials, and service-role keys must never be
  logged or included in receipts.
- Direct browser/table access is denied; narrowly scoped security-definer RPCs
  enforce ownership and immutable bindings.
- Identity, tokens, revisions, receipts, and results remain environment-scoped.
- Prompt/tool/config artifacts follow the approved retention policy; their
  digests remain in provenance even if restricted artifacts are later deleted.
- Rate limits apply separately to token issuance, submissions, and receipt
  lookup.
- No endpoint serves reference/reveal material before close, including through
  error text, storage paths, timing-dependent lookup, or shared caches.
