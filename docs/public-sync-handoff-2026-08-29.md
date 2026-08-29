# Public mirror sync handoff — 2026-08-29

## Included source

- Final 100-target Weekly training-similarity audit and reproducible reports.
- Canonical protein-frame overlap plus the separately calibrated RnP-style
  SuCOS-pocket comparison.
- Retrospective Default, Novel-first, and Familiar-first sorting with stable
  published question numbers and linked PDB identities.
- Xtal-first, closest-training-second molecular reference navigation; the
  training pose remains absent from Show all.
- Retrospective Play for fun with separate post-reveal scoring and no seeded
  leaderboard entries.
- Provider-neutral intake-conflict handling, one-retry authorization, and
  actionable output-validation subtypes.
- Local-server routing for the Play-for-fun results endpoint.

## Intentional exclusions

The public mirror does not include credentials, hosted runtime values, private
artifacts, access-gate configuration, provider-specific deployment profiles, or
spend-producing schedules. Production HTML shells may therefore differ while
shared browser modules remain parity-checkable.

## Verification

Before merging this sync:

1. Run `npm test`.
2. Run the scientific pipeline tests for training similarity, RnP scoring,
   overlays, reports, worker diagnostics, Supabase coordination, and Weekly
   intake.
3. Run `npm run parity:production -- --origin https://www.foldarium.org`.
4. Confirm `docs/feature-backlog.md` and this handoff match the merged feature
   state.

## Remaining work

- Apo-pocket structural similarity remains a candidate feature and must stay
  separate from protein familiarity and ligand-bound-system familiarity.
