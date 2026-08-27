# Foldarium

Foldarium is an open-source molecular pose-triage quiz and a portable pipeline
for assembling, scoring, and revealing blind weekly co-folding rounds.

The repository contains:

- the browser quiz and Mol* molecular viewer;
- pseudonymous voting, leaderboards, and retrospective APIs;
- Supabase/Postgres migrations for the quiz control plane;
- a provider-neutral Python pipeline for OpenFold3 and Boltz-2;
- preparation and scientific evaluation tools;
- an audited Selector API and optional LLM scoring clients.

No hosted-service credentials, model weights, private artifacts, or
provider-specific deployment configuration are included.

## Quick start: local-only viewer

Requirements: Node.js 20 or newer and Python 3.11 or newer.

```bash
git clone https://github.com/rafwiewiora/foldarium.git
cd foldarium
npm run dev
```

Open <http://127.0.0.1:4319/weekly>. Without Supabase configuration the
application shell still runs, but production weekly records, molecular assets,
persistence, shared results, and archive APIs remain unavailable. Mol* and
browser dependencies are loaded from public CDNs.

Run the JavaScript tests with:

```bash
npm test
```

## Supabase-backed development

1. Copy `.env.example` to `.env`.
2. Start the local stack and apply every migration:
   `npx supabase start && npx supabase db reset`.
   Alternatively, create a hosted project and apply `supabase/migrations/` in
   timestamp order.
3. Run `npx supabase status -o env` and copy the local publishable/anonymous
   key and service-role key into `.env`.
4. Create the public `structures` Storage bucket. Anonymous sign-in is enabled
   in the checked-in local configuration.
5. Keep `FOLDARIUM_DEVELOPMENT_WRITES_ENABLED=0` until the schema and RLS checks
   pass; then explicitly set it to `1`.
6. Start the app with `npm run dev:env`.

Only the publishable browser key belongs in browser configuration.
`SUPABASE_SERVICE_ROLE_KEY`, replay passwords, HMAC keys, and benchmark ingest
tokens are server-only secrets.

To upload structure assets generated or downloaded into `data/` and
`data_rnp/` to your own public bucket:

```bash
SUPABASE_URL=https://your-project.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key \
npm run upload:structures
```

The uploader is content-aware and skips existing objects unless `--overwrite`
is passed.
Use `npm run upload:benchmark` with `BENCHMARK_DEMO_DIR` set to a materialized
benchmark demo directory to upload its separately generated molecular assets.
Benchmark demo assets must be generated and uploaded before the demo works.

## Preserved legacy data

The former static/SQLite prototype and 122 MiB of previously public molecular
data are preserved in the versioned
[`foldarium-data`](https://github.com/rafwiewiora/foldarium-data) releases
rather than shipped with every application checkout. Download and verify the
legacy public v1 archive with:

```bash
npm run data:legacy -- --destination ./legacy-data
```

The downloader pins the release SHA-256, rejects unsafe archive paths, and
extracts into `legacy-data/foldarium-legacy-public-v1/`. The archive is
historical research/demo material; it is not the production weekly database.

## Local API server

`server.mjs` is a dependency-free Node HTTP server. It serves the static app,
maps `/weekly` and `/weekly/retrospectives`, and adapts the handlers in `api/`
without assuming a hosting provider.
The standalone classic-results view remains available at
<http://127.0.0.1:4319/leaderboard.html>.

Configuration uses `FOLDARIUM_ENV` (`development`, `preview`, or `production`)
and the matching `FOLDARIUM_<ENV>_*` variables documented in `.env.example`.
Privileged retrospective administration is disabled by default. If enabled,
it must sit behind an authenticated reverse proxy and use the explicit
`authenticated-proxy` access attestation.
The public default branch is the canonical production source; see
[production parity](docs/production-parity.md) for post-deployment verification.

## Prediction pipeline

The pipeline can validate and plan work without a GPU or database:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ./pipeline
python -m unittest discover -s pipeline/tests -v

foldarium-pipeline validate-target pipeline/examples/target.json
foldarium-pipeline weekly-plan \
  --release-date 2026-08-29 \
  --max-targets 2 \
  --output /tmp/foldarium-week.json
```

Install `./pipeline[evaluation]` for Gemmi/NumPy/RDKit evaluation. Real
OpenFold3 or Boltz-2 execution requires those independent projects, their
weights, and an execution environment supplied by the operator. The core task,
worker, artifact, and provenance contracts do not depend on a cloud provider.
See [pipeline/README.md](pipeline/README.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `index.html`, `app.js` | Quiz UI and Mol* viewer |
| `api/`, `lib/` | Provider-neutral HTTP handlers and contracts |
| `supabase/migrations/` | Database schema, RLS, and RPCs |
| `pipeline/` | Weekly orchestration and scientific evaluation |
| `prep/` | CAMEO and Runs-n-Poses preparation tools |
| `benchmark/` | Training-similarity viewer and preparation tools |
| `scripts/fetch-legacy-data.mjs` | Verified optional legacy-data downloader |
| `weekly-selector-offline/` | Offline Selector client |
| `tests/` | Unit and browser-level tests |

## Security and privacy

The public UI has no client-side password gate: such a gate does not provide
security. Protect private deployments at the reverse proxy or identity layer.

Human names are self-chosen pseudonyms. Server-side endpoints minimize exposed
identifiers, but operators remain responsible for their Supabase RLS policies,
retention policy, secret rotation, abuse controls, and privacy disclosures.
Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Data and licensing

Foldarium code is available under the [MIT License](LICENSE). Structures,
prediction outputs, fonts, models, weights, and upstream datasets may have
separate licenses and attribution requirements; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Do not redistribute model
weights or generated datasets until you have verified their terms.

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
