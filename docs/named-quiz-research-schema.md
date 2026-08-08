# Named quiz research schema

Migration `20260808010500_add_named_quiz_research_events.sql` adds the database
contract for named quiz sessions, contextual feedback, and replayable weekly votes.
It is intentionally not a production runbook: applying it changes the production
schema and must go through the normal migration review and backup process.

## Browser RPC contract

All calls require an authenticated Supabase session (anonymous Supabase Auth users
count as authenticated). The browser does not calculate or submit identity hashes.

- `start_named_quiz_session(p_session_id, p_source, p_difficulty, p_display_name)`
  starts or idempotently upgrades a classic quiz session.
- `start_named_weekly_quiz_session(p_session_id, p_round_id, p_display_name,
  p_initial_app_state)` starts a weekly session while its round is open.
- `submit_weekly_quiz_vote_attempt(p_vote_attempt_id, p_session_id, p_round_id,
  p_item_id, p_question_index, p_choice_id, p_picked_none, p_viewer_trace,
  p_app_state, p_active_pane_id)` appends an ordered replay event and updates the
  existing latest-vote projection in the same transaction.
- `complete_named_weekly_quiz_session(p_session_id)` marks a weekly session done.
- `submit_user_suggestion(p_suggestion_id, p_suggestion_text, p_context,
  p_quiz_session_id, p_weekly_session_id, p_item_id, p_page_path,
  p_app_state, p_viewer_snapshot, p_viewer_trace_tail)` stores feedback for exactly
  one owned, named session. An item ID must match captured app state and, for a
  weekly session, an advertised item in that round.

Suggestion contexts are lowercase slugs of 1-64 characters. For example,
`pose-quiz` and `weekly-quiz` are both valid.

The UUID parameters are idempotency keys. Reusing a key for different content is
rejected. Existing direct classic-session writes and the original
`submit_weekly_quiz_vote` RPC remain available for older clients.

## Identity and privacy

The migration generates a random 32-byte key with `pgcrypto` and stores it in the
ungranted `private.foldarium_secrets` table. Participant hashes and participant-bound
normalized display-name hashes are HMAC-SHA-256 values calculated only in
security-definer functions. The same display name used by two users therefore does
not produce a shared correlation key. A plaintext display name is retained on its
owned session; replay-safe views exclude both plaintext names and Supabase Auth user
IDs.

The service-side replay API should query only:

- `replay_quiz_sessions_safe`
- `replay_weekly_sessions_safe`
- `replay_quiz_answers_safe`
- `replay_weekly_vote_attempts_safe`
- `replay_user_suggestions_safe`

Suggestions are free text and display names are personal data. Before production,
set and document retention/deletion periods, update the participant notice, and
confirm who can use the replay service credential.

## Limits and live-test prerequisites

JSON payloads have database-enforced serialized size ceilings: viewer traces 512
KiB, app traces 256 KiB, app state 64 KiB, and suggestion snapshots/tails 128 KiB.
The RPCs also cap named sessions, weekly vote events, and suggestions per user in
rolling time windows. New tables have RLS enabled with no browser table policies;
authenticated clients receive RPC execution only.

A real integration test requires a disposable Supabase project with all earlier
migrations applied, `pgcrypto` available, anonymous Auth enabled, and both an
authenticated JWT and a service-role key. Verify there that:

1. direct reads/writes to the new tables fail for anon/authenticated roles;
2. hashes are stable within the project but the private key is unreadable;
3. idempotent retries return one event and key reuse with changed content fails;
4. closed-round, cross-user, oversized, and rate-limit requests fail;
5. replay-safe views return traces and suggestions without `user_id` or plaintext
   `display_name` fields.
