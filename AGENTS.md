# Project continuity

- Before changing shared browser, pipeline, or retrospective behavior, read
  `docs/production-parity.md`, `docs/feature-backlog.md`, and the latest
  `docs/public-sync-handoff-*.md`.
- Keep the active task list accurate while working. Before handoff, mark every
  item completed, pending, blocked, or cancelled; do not leave stale
  “in progress” entries.
- Update `docs/feature-backlog.md` in the same change whenever a feature is
  added, promoted, shipped, declined, or superseded.
- Update or supersede the public sync handoff after every mirror sync or major
  release. Record tested source state and intentional exclusions, never secrets
  or private runtime identifiers.
- Preserve the public boundary: no credentials, private artifacts,
  provider-specific deployment code, spend-producing schedules, or access-gate
  configuration.
- Run `npm test` before public merge. When checking a live deployment, also run
  `npm run parity:production -- --origin https://www.foldarium.org`.
