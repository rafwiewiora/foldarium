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

Nothing in Foldarium currently runs OpenFold or another inference model. The browser renders existing
predictions. A database-backed live product means that its pose collection and shared scores can grow
without rebuilding a small static-demo manifest.

The current static demo is not database-backed yet. On startup, `app.js` fetches four committed JSON
manifests, then lazily fetches the protein, pocket, crystal ligand, and displayed pose files named in
those records.

## Target architecture

- **Vercel:** serve the root HTML, JavaScript, and other small static assets.
- **Supabase Postgres:** systems/items, poses, clustering and method provenance, sessions, and answers.
- **Supabase Storage:** immutable, versioned PDB/mmCIF structure assets.
- **Optional Edge Function or Vercel function:** assemble a quiz session and score/reveal answers when
  correctness must not be exposed to the browser.

A continuously running Python web process is not part of this architecture. Python remains useful
offline for pose preparation, alignment, validation, clustering, and database ingestion.

## Ownership and integration order

1. Brian defines the Supabase schema, Storage layout, row-level-security policy, and a query that returns
   a bounded quiz session rather than the whole database.
2. Preserve the normalized item shape already consumed by `app.js`, or provide a small adapter to it.
3. Foldarium's manifest loader is replaced with that Supabase query/API; the Mol* viewer, clustering,
   method pages, and grid can remain unchanged.
4. Replace the browser-only leaderboard with Supabase session/answer writes and aggregation.

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
