# Explicit bounded inference and completed-work recovery

The public library includes a direct Messages API provider and a
credential-separated publication adapter. It has no schedule, deployment
configuration, hosted launcher, cloud-volume CLI, or default model/budget
configuration. Importing these modules does not contact a provider or start work.

`ApiConfig` requires an explicit pinned model, input/output token bounds,
reviewed upper rates, total budget, request timeout, and rate-review expiry.
`config_sha256` binds the complete provider policy, frozen prompt/schema, wire
schema projection, network allowlist, and cost-accounting policy. The caller must
supply the exact immutable benchmark job, verified blind kit, and configuration.
Model aliases, mismatched provenance, unexpected usage, and unknown schemas fail.

`execute_benchmark_job` runs in the service-authorized host. It validates the
registered round, kit, and frozen job; its launch callback receives only the
blind kit, job/configuration, and execution state directory. The callback must
isolate provider credentials from service credentials, enforce API-only egress,
mount only that execution's state, and serialize writers. The public library
asserts that contract but does not implement a hosting platform's isolation.

`run_isolated_inference` is the library entry point inside that isolated runtime.
It requires an existing initialized budget ledger and a durable commit callback.
Before each new paid request it reserves the full configured input/output cost
ceiling, writes the ledger, and commits storage. Failed commits prevent dispatch;
ambiguous paid outcomes retain their reservation and are never automatically
retried. Successful exact-input responses can be replayed without another paid
request. Missing or corrupt ledgers cannot initialize fresh spending authority.

The existing service-only one-time initialization grant must be applied before
using this adapter. It anchors spending independently of local state: a lost
initialization acknowledgment or lost execution volume cannot silently reset the
budget. The caller must preserve the lease and durable execution state throughout
inference. Configured rates bound accounting, not the provider's actual invoice;
review them against the exact provider/model before enabling a new execution.

Rate-review expiry blocks new reservations and paid requests. It does not change
the frozen configuration digest or invalidate exact completed work. After expiry,
a registration retry can validate and publish the same completed artifact, and a
runner can reconstruct it from verified successful item checkpoints. An unfinished
item still stops before reserving or dispatching another paid request. Checkpoint
reconstruction retains the free model-metadata preflight; a complete artifact can
be verified without provider calls.

The transport fixes its provider origin, refuses redirects and proxy environment
settings, makes no billable retries, and suppresses response/error bodies in
exceptions. Native structured output is separately versioned and hashed; the
original strict response validator remains authoritative. No answer data enters
the inference callback.

The no-network test suite supplies synthetic model metadata and responses. It
covers conservative precharge, ambiguous outcomes, lost ledgers, tampered usage,
input/config identity, expired-rate recovery, registration failure, and exact
runner artifacts. No provider credential or paid call is required for tests.
