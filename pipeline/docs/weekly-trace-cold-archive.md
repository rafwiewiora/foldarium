# Weekly thinking-trace cold archives

Foldarium's vote totals, revision history, comments, participant hashes, and
compact replay bindings are operational records and remain hot in Postgres.
Continuous camera/app event batches and legacy full traces embedded in older
vote attempts are research recordings: after the agreed hot-retention window,
they can be copied to verified cold storage without changing quiz behavior.

This branch contains only the foundation for that lifecycle. It does **not**
connect to Supabase, upload an archive, or delete a source row.

## Archive boundary

There is one archive per `weekly_quiz_sessions.session_id`. The source snapshot
must come from a privileged, server-safe export and contain only:

- session ID, round ID, participant/display-name HMACs, and timestamps;
- every append-only vote attempt, including its choice revision, dedicated
  `vote_comment`, compact `app_state`, active pane, and nullable legacy
  `viewer_trace`;
- every continuous trace batch, with its visit and sequence envelope.

Plaintext names, auth user IDs, emails, tokens, and unknown fields fail closed.
New vote attempts are expected to leave `viewer_trace` null and bind to the
continuous stream through `app_state.trace_visit_id` and
`app_state.trace_through_sequence`. Older embedded traces are retained exactly.

## Deterministic local export

The exporter uses canonical UTF-8 JSONL (sorted object keys, compact separators,
one record per line) and deterministic gzip (`mtime=0`, no filename). Gzip is the
dependency-free fallback available in Python's standard library; the manifest
records the compression algorithm so a later format version can standardize on
Zstandard when its runtime dependency is pinned.

Input is a local JSON file. This command performs no network operation:

```bash
cd pipeline
PYTHONPATH=src python scripts/export_weekly_trace_archive.py export \
  --source /secure/local/session-snapshot.json \
  --output-dir /secure/local/verified-archives
```

It writes `<session_id>.jsonl.gz` and `<session_id>.manifest.json` using mode
`0600`, then immediately reads and fully verifies them. Running the same export
again reuses the byte-identical pair. If either existing file differs, export
fails instead of overwriting it.

Verification can also be repeated independently. Supplying the source snapshot
checks drift in addition to the self-contained integrity checks:

```bash
PYTHONPATH=src python scripts/export_weekly_trace_archive.py verify \
  --archive /secure/local/verified-archives/<session>.jsonl.gz \
  --manifest /secure/local/verified-archives/<session>.manifest.json \
  --source /secure/local/session-snapshot.json
```

Verification covers:

- compressed and uncompressed byte counts and SHA-256 checksums;
- canonical JSON and stable record ordering;
- exact vote-attempt and trace-batch primary-key membership;
- a SHA-256 and byte count for every source record;
- session/round membership and duplicate-ID rejection;
- visit/question consistency, endpoint binding, and gap/overlap-free sequences;
- explicit integrity-accounted `omitted`/`dead_letter` ranges (reason and byte
  count required), while every unexplained range still fails closed;
- manifest counts, compact visit index, and content-derived archive identity;
- exact equality with the supplied source snapshot.

## Catalog and future lifecycle

`20260812160000_add_weekly_trace_archive_catalog.sql` adds private, server-only
tables for jobs, manifests, exact row membership, and compact visit indices. It
defines no RPC and no deletion mechanism. Browser roles receive no privileges.

A later reviewed worker may implement upload and retention only with the
following separate phases:

1. select sessions older than the configured hot-retention cutoff;
2. take one transactionally consistent server-safe snapshot per session;
3. export and independently verify it locally;
4. upload to versioned, encrypted S3-compatible storage with object lock or an
   equivalent retention policy;
5. download the exact object version and rerun full verification;
6. record verified archive/manifests/members/visits in the private catalog;
7. require a separate explicit purge approval and re-check source membership,
   archive availability, checksum, object version, and age before deleting only
   bulky trace payloads.

Vote attempts, choices, comments, participant hashes, app-state bindings, and
the compact archive indices must never be part of that purge. Any count,
membership, checksum, sequence, version, or availability mismatch fails closed.
Stable record, visit, visit-batch, and per-question vote-revision ordinals are
included in the manifest/catalog so equal or delayed submission timestamps do
not make replay order ambiguous.
