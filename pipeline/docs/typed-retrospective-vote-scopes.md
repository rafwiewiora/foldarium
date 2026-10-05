# Typed retrospective vote scope

The v2 vote RPC validates selection kind and ID against the immutable blind
manifest. Its optional application state is telemetry and may be absent or
contradict the typed selection. Retrospective archival therefore uses validated
typed provenance first, including None; telemetry cannot override it or block an
otherwise valid vote. Legacy untyped votes retain the existing application-state
inference and the existing exact-only legacy round rule.

Migration `20261005040000_use_verified_typed_retrospective_votes.sql` adds a
service-only scoped getter. It reuses the immutable source-attempt/resolution
audit and additionally requires complete count equality, rejecting incomplete
proof hidden by SQL NULL logic. Source identity, submitted time, manifest digest,
selection membership and any resolution fingerprint must match. Broken typed
proof never falls back to telemetry. The scoped response is private and binds the
round, blind manifest and exact final vote fields; Python rejects mixed snapshots
before producing the unchanged normalized archive schema.

No ballot, source artifact, published digest or classification is rewritten.
Existing untyped source bytes remain identical. New registration still compares
the complete canonical source against the authoritative SQL source and preserves
immutable publication identities. Public projections expose none of the new
private provenance fields. Apply the additive migration before workers using the
new getter; no frontend or paid-inference activation is required.

Validation includes real migrated PostgreSQL v1/v2 vote and resolution RPCs,
null/contradictory telemetry, exact/cluster/None, mismatched attempt, timestamp,
manifest and resolution proof, SQL NULL accounting, private ACLs, and canonical
legacy source parity. Python tests also cover envelope binding and deterministic
source bytes; the coordinator test checks exact selected fields and scoped RPC.
