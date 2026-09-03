# Delayed Weekly retrospective release

Weekly rounds keep the existing Wednesday close/reveal behavior unless an exact
open production round is explicitly opted into
`next-weekly-activation`.

This policy changes lifecycle timing only. It does not change cluster, exact, or
none ballot meaning; correctness; vote persistence; result aggregation; or
player-name provenance.

## Opt-in flow

1. Configure the exact open round before its original close. The operator must
   provide the recorded close and a finite safety close no more than seven days
   later. Dry-run is the default.
2. Wednesday's retrospective tick evaluates the round against released
   coordinates, stores the answer artifact privately, and records only its
   digest-bound private descriptor in round metadata. Repeated ticks reuse that
   artifact. The public reveal remains blocked even if the safety close passes.
3. The next production Weekly activation spawns the handoff for the exact
   predecessor and successor IDs.
4. The handoff shortens the predecessor's close to the activation time,
   promotes the prepared artifact into the post-close private catalog, reveals
   the round, snapshots final votes, and publishes its retrospective.
5. If any post-close step fails, the handoff is idempotent and can be rerun with
   the same two round IDs.

The safety close prevents indefinite voting if Saturday activation never
occurs. It is not permission to reveal: delayed rounds without a recorded
successor remain fail-closed.

## Operator entry points

- `configure_delayed_weekly_retrospective`: exact-round dry-run/apply gate.
- `weekly_retrospective_tick`: scheduled or exact-round private preparation.
- `delayed_weekly_retrospective_handoff`: exact predecessor/successor
  dry-run/apply gate and recovery path.
- `weekly_production_promotion_tick`: starts the handoff after opening the next
  production round.

Current rounds without `metadata.retrospective_release` retain the deployed
Wednesday behavior.
