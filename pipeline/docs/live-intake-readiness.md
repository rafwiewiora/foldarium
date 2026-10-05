# New intake from mutable wwPDB prerelease URLs

The automatic planner and default `deployment_weekly_hook` accept new live intake
only from Saturday 03:00 UTC (inclusive) until Wednesday 00:00 UTC (exclusive).
These boundaries restore the original reviewed Saturday scheduling start and
stop new blind intake when experimental structures are due for release.
[wwPDB's release policy](https://www.wwpdb.org/documentation/journals) describes
prerelease availability by Saturday 03:00 UTC and full release by Wednesday
00:00 UTC. Neither that timetable nor an HTTP modification timestamp proves
that particular downloaded bytes belong to the requested week.

Before fetching, the hook requires the validated snapshot for the immediately
preceding Saturday. Its campaign identity must be canonical, descriptor hashes
must match metadata hashes, and row counts must be valid. No baseline returns
`prior-prerelease-baseline-unavailable`; an older baseline returns
`immediate-prior-prerelease-baseline-unavailable`. Both are explicit dependency
waits. A malformed baseline is an operational error, without fallback to an
older snapshot.

Both current input hashes must differ from that baseline. Either unchanged file
returns `prior-prerelease-source-unchanged`, preventing an unchanged prior pair
or a partially updated pair from being registered. A complete parsed intake
with no eligible bounded targets also waits. These normal dependency waits do
not consume the durable outbox's five-error retry allowance.

This is evidence of changed bytes relative to a known immediate predecessor,
not an upstream release-date attestation. It cannot prove freshness after a
format-only rewrite or an upstream labeling error. The exact acquired files,
hashes, target inputs and registered plan remain immutable provenance.

The hook rechecks the window after acquisition and planning, before new
registration. If another invocation has already registered the campaign, that
existing registration is acknowledged instead. Both apply and live dry-run
paths check an existing campaign before any window, baseline or source fetch,
so lost acknowledgements and historical registered campaigns remain recoverable.
Existing prediction runs and scientific artifacts are never recreated by this
guard. The reconciler passes the exact Saturday date to the hook, allowing
Sunday through Tuesday catch-up inside the same window.

First use, a missed preceding week, or an expired new-intake window requires an
explicit operator-reviewed bootstrap/recovery using provenance-bound saved
source bytes. `build_weekly_plan` and `register_weekly_plan` remain available
for that purpose; no automatic baseline or date is invented. The lower-level
`collect_wwpdb_inputs`/`build_public_weekly_plan` helpers still support explicit
fetchers for replay and do not themselves attest the supplied release date.
They must not be treated as the guarded automatic registration entrypoint.
A wholly missed or unregistered zero-eligible week therefore requires verified
saved-source bootstrap before the following week's automatic intake can resume.

Tests cover exact clock boundaries, direct stale-intent execution, acquisition
crossing Wednesday, existing registration outside the window, missing/older or
malformed baselines, both partial-rollover directions, unchanged pairs, valid
changed inputs, live dry-run behavior, and repeated zero-target dependency waits.
