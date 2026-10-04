# Historical Preview research archive

Expired Preview rounds are separate research cohorts. They retain the exact original round ID, campaign, Preview environment, blind manifest, private index and opening/closing timestamps. This path never reopens a human voting window, copies a benchmark to production, changes a round row, or reads human sessions/ballots. The full item/choice population remains in evaluation and publication; audited unscorable references retain explicit null metrics and full lineage, with separate scorable/excluded counts.

Nothing is authorized by migration alone. A service operator explicitly calls `authorize_weekly_historical_preview_v1` with the exact stored round ID, blind/private digests and original timestamps. Authorization locks the round and inserts an immutable scope. Superseded siblings do not inherit it. The existing reconciliation evaluation/kit/benchmark/retrospective gates apply; no paid provider or budget is enabled here. An absent benchmark policy still blocks publication. An explicit empty expected set is possible only through the existing reviewed policy RPC.

The reconciler registers a kit, evaluates through the provider-independent historical service, and runs the existing exact benchmark artifact verification/ingestion. Inference remains blind, with no private evaluation access in the restricted inference Sandbox. Publication requires all frozen expected execution receipts with exact provider, model, configuration and verified semantic/payload digests. Existing archived executions remain Preview executions.

Evaluation uses the same scientific evaluator and artifact integrity checks as production, through a separate format `foldarium.historical-preview-evaluation/v1`. Production v5/v6 entry points still reject Preview. The catalog is separate from production evaluations. The public research projection uses `foldarium.historical-preview-research/v1`: all questions and method choices, original source lineage, exact model decisions, explicit `human_cohort.included=false` and null human denominator. No sessions, people, ballot IDs, reasoning, prompts or object URIs are projected. Scientific artifact/config hashes and exact execution UUIDs are deliberate research provenance, so this projection must not be fed to the production human leaderboard sanitizer.

Both evaluation and sanitized research publication bytes stay in private content-addressed Storage. `publish_weekly_historical_preview_v1` locks the original round and rechecks immutable source, evaluation, full population, original closed window, frozen membership, verified receipts and artifact content hash before atomically inserting the publication catalog. Repeated identical writes are idempotent. Changed windows or digests fail closed. The public summary view exposes only published scope/lineage/counts; it hides rows if the original source binding drifts and never exposes a Storage URI.

The read-only `/api/historical-preview-research` lists published summaries and resolves an exact `round_id` through the private catalog. It verifies source/window/catalog identity, bounded artifact size and SHA, exact projection schema, null-score dispositions and benchmark decision correctness before returning research data. Unpublished scopes never trigger private downloads. `/historical-preview-research.html` provides the separate research archive and full-population detail page, explicitly labeled without a human-vote cohort. It does not change the production human leaderboard.

## Verification

`test_historical_preview.py` covers exact source/window authorization, deterministic idempotent evaluation, production/Preview separation, full population, no human cohort, audited null scores, semantic receipt and frozen model mismatch, and durable planning without promotion/reveal.

Generate synthetic fixtures using the evaluation test environment:

```sh
PYTHONPATH=pipeline/src:pipeline/tests python pipeline/tests/test_historical_preview.py --write-pg-fixture /tmp/historical-scored.json
PYTHONPATH=pipeline/src:pipeline/tests python pipeline/tests/test_historical_preview.py --write-pg-fixture /tmp/historical-unscorable.json --unscorable
node pipeline/tests/check_historical_preview_database.mjs /path/to/pglite/dist/index.js /tmp/historical-scored.json --benchmark
node pipeline/tests/check_historical_preview_database.mjs /path/to/pglite/dist/index.js /tmp/historical-unscorable.json --benchmark
```

The disposable PostgreSQL harness applies the real migration and tests authorization privileges, append-only scopes/catalogs, exact window drift, absent policy and missing/unverified receipts, wrong execution/payload, full populations, private-only object references, idempotent publication and safe anonymous summaries. Omit `--benchmark` to exercise an explicit empty expected set. No live credentials, inference, Storage writes or production database changes are used by these tests.
