# Foldarium handoff: Vercel + Supabase

## Which app to use

Use the frontend at the **repository root**:

- `index.html` + `app.js` — quiz and Mol* viewer;
- `leaderboard.html` — leaderboard UI;
- `quiz_items*.json` — the preset collection used by the current GitHub Pages demo.

The `app/` directory is a legacy full-data/SQLite prototype. It remains useful for its larger manifest
examples, old endpoint semantics, and data-preparation history, but it is not the target Vercel app and
its Python server is not required.

## What "live" means

The Foldarium browser never runs OpenFold or another inference model; it renders existing predictions.
A database-backed live product means that its pose collection and shared scores can grow without
rebuilding a small static-demo manifest.

Foldarium also needs its own independent, method-neutral prediction pipeline. An external organization
may later supply predictions through an import adapter, but the Foldarium deployment must not require or
include that provider's implementation.

The current static demo is not database-backed yet. On startup, `app.js` fetches four committed JSON
manifests, then lazily fetches the protein, pocket, crystal ligand, and displayed pose files named in
those records.

## Target architecture

- **Vercel:** serve the root HTML, JavaScript, and other small static assets.
- **Supabase Postgres:** campaigns, targets, prediction-run state and provenance, published systems and
  poses, sessions, and answers.
- **Supabase Storage or another Foldarium-controlled object store:** immutable, versioned target inputs,
  raw prediction outputs, and viewer-sized PDB/mmCIF assets.
- **Foldarium scheduler and workers:** collect weekly targets, enqueue configured methods, run containerized
  co-folding jobs on suitable GPU infrastructure, evaluate released references, and publish approved
  batches. These workers are independent of Vercel.
- **External-provider adapters:** optionally import predictions such as future OpenFold Portal OF3 output
  into the same normalized run contract without importing provider source code.
- **Optional Edge Function or Vercel function:** assemble a quiz session and score/reveal answers when
  correctness must not be exposed to the browser.

A continuously running Python web process is not part of this architecture. Python remains useful
offline for pose preparation, alignment, validation, clustering, and database ingestion.

## Weekly catalog growth

The intended lifecycle is:

1. On Saturday, a Foldarium intake job reads public wwPDB Phase-I prerelease sequence and non-polymer
   manifests. Coordinates are not available yet.
2. It applies Foldarium's versioned target-selection policy, stores every decision and skip reason, and
   creates one normalized target package per selected entry.
3. The scheduler creates deterministic prediction runs for every enabled method/configuration. Local
   adapters submit containerized jobs to Foldarium-controlled GPU infrastructure; provider adapters may
   import externally generated predictions later.
4. Each adapter writes the same normalized result: stable run identity, method/version/configuration,
   seed or rank, input and image/checkpoint provenance, status, confidence metadata, checksums, and raw
   structure-object URLs.
5. After Wednesday's PDB coordinate release, Foldarium aligns predictions to each released reference,
   scores the benchmark ligand, clusters poses across methods, builds viewer-sized assets, and writes one
   versioned publication manifest.
6. A privileged, idempotent publisher validates that manifest and its objects, upserts Supabase rows,
   and atomically marks the batch `published`.
7. The root frontend queries only published systems, so a new week appears without a Vercel redeploy.

The number of methods, seeds, and ranked samples is a campaign configuration and cost decision, not a
frontend assumption. Raw outputs belong in object storage; Postgres should contain metadata, state,
provenance, checksums, and URLs rather than molecular-file blobs.

This method-neutral weekly runner and Supabase publisher are not implemented on `main` yet. Existing
`prep/cameo/` scripts are useful scientific references for released-coordinate alignment, RMSD scoring,
clustering, and bucketing, but they consume an existing CAMEO result layout and are not the independent
Foldarium runner.

## Ownership and integration order

1. The Supabase/backend owner defines the control-plane schema, object layout, row-level-security policy,
   worker/publisher credentials, idempotent publication operation, and bounded quiz-session query.
2. The Foldarium compute owner implements target intake, the method-adapter contract, job execution,
   retries, provenance, and external-result importers without copying provider implementations.
3. The scientific pipeline owner implements Wednesday evaluation, clustering, QC, and the versioned
   publication manifest.
4. Preserve the normalized item shape already consumed by `app.js`, or provide a small adapter to it.
5. Foldarium's manifest loader is replaced with that query/API; the Mol* viewer, clustering, method
   pages, and grid can remain unchanged.
6. Replace the browser-only leaderboard with Supabase session/answer writes and aggregation.

This division keeps the database design with the person implementing it and avoids creating a second,
conflicting schema in the frontend repository.

## Viewer data contract

Each returned item needs stable identity and display metadata, structure URLs, and a list of choices.
The fields currently used include:

```text
item:
  id, source, ligand, n_heavy
  protein_file, pocket_file, xtal_lig_file
  choices[]

choice:
  af3_sample                         stable pose/sample ID despite the historical name
  pose_file                          absolute or relative PDB/mmCIF URL
  cluster, is_rep                    clustering and representative metadata
  _method                            co-folding method; hidden until reveal where appropriate
  afprotein_file                     optional predicted-protein structure
  rmsd, correct, plddt               answer/scoring metadata
```

`protein_file`, `pose_file`, and the other structure fields may be public or short-lived signed
Supabase Storage URLs. Keep stable system, pose, dataset, and manifest-version identifiers so database
growth or reclustering does not change the meaning of previously recorded answers.

The current client-side manifests contain `rmsd` and `correct`. That is acceptable for a casual static
demo, but anyone can inspect them in browser developer tools. If answer secrecy matters, withhold those
fields from the initial session response and return them only from a score/reveal function.

## Security boundary

- Never put a Supabase service-role key in browser code or a committed file.
- Use the public anon key only with appropriate row-level-security policies.
- Validate score/session writes server-side or in a restricted database function if leaderboard
  integrity matters.
- Prefer versioned immutable structure objects so cached molecular files cannot silently change.

## Local static demo

The preset demo needs only a static file server:

```bash
python3 -m http.server 8000
# Open http://127.0.0.1:8000
```

`?dev=1` enables browse mode without submitting votes. The Python command here is only a convenient
local static-file server; it is not an application backend.
