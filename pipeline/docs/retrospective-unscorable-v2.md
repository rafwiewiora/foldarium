# Retrospective scoring for explicit unscorable references

The private evaluator validates the full reference disposition, including its
reference digest. Retrospective aggregation calls the same validator before
excluding an item. Missing or malformed dispositions cannot shrink denominators.

Fully scored rounds keep byte-identical public/admin v1 artifacts. A round with at
least one valid unscorable item uses public/admin v2. Source snapshots and the
publication catalog descriptor remain v1; their complete immutable input and
artifact digests retain the original population.

The v2 public/admin `round` keeps full `item_count`/`choice_count` and adds
`scorable_item_count`/`excluded_item_count`. All original questions, choices in the
scientific source, and submitted responses remain present. Questions carry
`evaluation_status`; unscorable questions include the validated
`reference_disposition` with only `reference_sha256` removed from this sanitized
projection. Human, model and Smina responses have `correct: null` for that item,
including a selection of `none`. Its human aggregate retains full
`answered_count`, sets `scorable_answered_count: 0` and `correct_count: null`.

Participant `answered`, `total`, `correct`, `accuracy` and `coverage` use only the
scorable questions. `full_answered`, `full_total`, `excluded_answered` and
`excluded_item_count` retain the full submitted population. With zero scored
answers, accuracy is null. With zero scorable questions, coverage is null and
completion is false. `human_aggregate.unscored_only_count` identifies participants
whose preserved submissions have no scored question. No vote or model execution
is rewritten and no unscorable answer receives a reward or penalty.
