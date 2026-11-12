# Local cofolding library review handoff — 2026-11-12

Base: public main `b62e8111251d7404d918efbc0a400321ed1ba04c`.
This scoped addition prepares an unscored local reviewer for Molarium-generated
protein–ligand libraries. It does not synchronize an operational deployment or
promote any of the pending Weekly/Selector reliability work in the October
handoff. Existing shared browser modules, databases and leaderboard semantics
are unchanged.

Prepared source: `library-review.html`, its CSS/JS, portable format and IndexedDB
store, protein Cα alignment and explicit ligand-graph rendering. Molarium's
importer records the exact source commit and SHA-256 for each file; only the two
same-version Mol* CDN asset URLs are changed to local assets in that copy.

Validation: alignment/format/aromatic-bond/charge tests; existing public Node
suite and audit; actual WebGPU-generated libraries at 20 and 76 residues viewed
through the Molarium-hosted reviewer, including saved exact preferences,
shortlist export, none-of-these semantics and import under a new local identity.
The screenshot and inference timing receipts live with Molarium's pinned
inference release. These are integration/throughput tests, not experimental pose
accuracy validation. Run `npm test` before merging this source.

Completed: standalone format and browser reviewer, lazy per-compound structure
loading, source integration into Molarium, dependency-light tests and real
end-to-end review checks.
The release follow-up preserves inspected cameras when changing a preference,
note or none-of-these decision; a Molarium browser regression checks that the
same viewer canvas survives a vote. The README now points to this handoff.
The method-comparison follow-up labels each pose's model and execution backend,
so native and quantized browser predictions can share a review library. Unknown
per-pose inference timing is shown as unavailable. It makes no claim that native
predictions are experimental truth or that confidence is calibrated across models.

Pending: separately operated production Foldarium route acceptance/rollout.
The Molarium-hosted route can ship independently. Public source availability
must not be described as deployment to `www.foldarium.org`.

Intentional exclusions remain credentials, private datasets/runtime identifiers,
provider deployment configuration, operational schedules and access controls.
No application-server molecular uploads or new analytics were added.
