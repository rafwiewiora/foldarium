# Private prediction failure evidence

The public source provides the collector, publisher library, and additive private
catalog migration. A host must explicitly call `preserve_failure_diagnostics`
after a returned prediction failure, catch diagnostic exceptions, and still
publish the original prediction result. The hosted worker hook and deployment
configuration are not included. Applying the migration and integrating workers
are separate steps; importing this library does not retry any prediction.

Returned worker failures retain their original status and result. A best-effort
private archive stores bounded stdout/stderr and native PDB/mmCIF files, including
alternate filenames that collectors do not recognize. Confidence/ranking/score
JSON files are eligible; arbitrary input/config JSON, checkpoints, environment
dumps, and caches are excluded. Native scientific bytes are never rewritten:
sensitive or oversized files are explicitly omitted. Logs redact known secret
values, bearer credentials, JWTs, quote-aware credential assignments and entire HTTP URLs, including JSON-escaped
URLs and known secret representations; bare Basic authentication is also removed.
This is defense in depth; private storage and service-only access remain required.

The limits are 32 native files, 8 MiB each, 64 MiB total, 256 KiB per log, 256 inspected output entries (including irrelevant extensions) and 128 output
directories. Discovery uses a bounded iterator, without materializing directory
listings; native count/byte limits are checked before reading files. Symlinks are
excluded and mark the inventory incomplete, including unrecognized link names
and a linked or invalid output root. An incomplete empty inventory is not proof
that the worker produced no native outputs. Logs retain
bounded head/tail context. Each descriptor states truncation, omissions, upload
failures and inventory limits. Partial preservation is not complete preservation.
Only uploaded files have object URIs. No failed output becomes a successful
prediction artifact or enters any public/blind quiz projection.

Evidence uses existing immutable SHA-addressed Storage writes with private cache
headers. The actual bucket privacy is checked before upload and again by the
catalog RPC. A separate private append-only catalog binds the descriptor to the
run, authoritative claimed attempt, worker and registered task digest. The private
descriptor additionally records effective retry task digest, image/method identity
and worker/adapter/diagnostic source hashes. Exact duplicate registration is
idempotent; conflicting replacement is rejected. Completed retry history remains
discoverable even when the main prediction result later changes.

Collection, reads and redaction check a 30-second collection deadline between
bounded operations. Diagnostic network requests have at most a five-second timeout,
under a 90-second overall deadline; each evidence upload uses the earlier deadline
that reserves 30 seconds for the descriptor/catalog. A retry does not sleep across
its phase deadline. A missing migration, unavailable Storage, expired lease or any
archive error produces only a sanitized unavailable receipt. It cannot replace
the scientific failure or prevent the normal finish call. Orphaned content-addressed
files can remain if the final catalog write fails; they are not advertised as
preserved evidence. The archive is not a replacement for durable worker dispatch.

Process kills and infrastructure termination before the failure branch do not
run this archiver. They require separately tracked terminal-call recovery. Past
failed scratch outputs cannot be reconstructed by deploying this change. Existing
rounds, successful artifacts, ballots, benchmarks and retry authorizations remain
unchanged. Further inference needs an explicit, provenance-separated recovery
plan; unused dollars do not reset a consumed per-run attempt allowance.
