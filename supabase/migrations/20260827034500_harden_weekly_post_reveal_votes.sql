-- Harden post-reveal idempotency, replay privacy, and append-only privileges.

begin;

-- Keep the original validated implementations private. Public wrappers acquire
-- an identity-specific transaction lock before the implementation's first
-- idempotency lookup, so concurrent retries observe the committed row.
alter function public.start_named_weekly_post_reveal_session(
  uuid, text, text, jsonb
) rename to start_named_weekly_post_reveal_session_unlocked_v1;
alter function public.start_named_weekly_post_reveal_session_unlocked_v1(
  uuid, text, text, jsonb
) set schema private;

alter function public.submit_weekly_post_reveal_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) rename to submit_weekly_post_reveal_vote_attempt_unlocked_v1;
alter function public.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) set schema private;

create or replace function public.start_named_weekly_post_reveal_session(
  p_session_id uuid,
  p_round_id text,
  p_display_name text,
  p_initial_app_state jsonb default null
)
returns public.weekly_quiz_post_reveal_sessions
language plpgsql
security definer
set search_path = pg_catalog
as $$
begin
  if p_session_id is null then
    raise exception 'invalid post-reveal session identity' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'weekly-post-reveal-session-id:' || p_session_id::text, 0
  ));
  return private.start_named_weekly_post_reveal_session_unlocked_v1(
    p_session_id, p_round_id, p_display_name, p_initial_app_state
  );
end;
$$;

create or replace function public.submit_weekly_post_reveal_vote_attempt(
  p_vote_attempt_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_choice_id text,
  p_picked_none boolean,
  p_vote_comment text,
  p_viewer_trace jsonb default null,
  p_app_state jsonb default null,
  p_active_pane_id text default null
)
returns public.weekly_quiz_post_reveal_vote_attempts
language plpgsql
security definer
set search_path = pg_catalog
as $$
begin
  if p_vote_attempt_id is null then
    raise exception 'invalid post-reveal vote identity' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    'weekly-post-reveal-vote-id:' || p_vote_attempt_id::text, 0
  ));
  return private.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
    p_vote_attempt_id, p_session_id, p_round_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, p_vote_comment, p_viewer_trace, p_app_state,
    p_active_pane_id
  );
end;
$$;

-- The safe service replay surface follows the existing research-data boundary:
-- stable identity hashes are exposed, never the participant's plaintext name.
drop view public.replay_weekly_post_reveal_vote_attempts_safe;
create view public.replay_weekly_post_reveal_vote_attempts_safe
with (security_barrier = true, security_invoker = true)
as
select
  attempt.vote_attempt_id, attempt.session_id, attempt.round_id,
  session.participant_hash, session.display_name_hash,
  attempt.item_id, attempt.question_index, attempt.choice_id, attempt.picked_none,
  attempt.selection_kind, attempt.selection_id, attempt.submission_phase,
  attempt.viewer_trace, attempt.app_state, attempt.active_pane_id,
  attempt.vote_comment, attempt.submitted_at
from public.weekly_quiz_post_reveal_vote_attempts attempt
join public.weekly_quiz_post_reveal_sessions session
  using (session_id, round_id, user_id);

revoke all on function private.start_named_weekly_post_reveal_session_unlocked_v1(
  uuid, text, text, jsonb
) from public;
revoke all on function private.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) from public;
revoke all on function public.start_named_weekly_post_reveal_session(
  uuid, text, text, jsonb
) from public;
revoke all on function public.submit_weekly_post_reveal_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) from public;
revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function private.start_named_weekly_post_reveal_session_unlocked_v1(
      uuid, text, text, jsonb
    ) from anon;
    revoke all on function private.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) from anon;
    revoke all on function public.start_named_weekly_post_reveal_session(
      uuid, text, text, jsonb
    ) from anon;
    revoke all on function public.submit_weekly_post_reveal_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) from anon;
    revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe from anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function private.start_named_weekly_post_reveal_session_unlocked_v1(
      uuid, text, text, jsonb
    ) from authenticated;
    revoke all on function private.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) from authenticated;
    revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe
      from authenticated;
    grant execute on function public.start_named_weekly_post_reveal_session(
      uuid, text, text, jsonb
    ) to authenticated;
    grant execute on function public.submit_weekly_post_reveal_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) to authenticated;
  end if;

  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke all on function private.start_named_weekly_post_reveal_session_unlocked_v1(
      uuid, text, text, jsonb
    ) from service_role;
    revoke all on function private.submit_weekly_post_reveal_vote_attempt_unlocked_v1(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) from service_role;
    revoke execute on function public.start_named_weekly_post_reveal_session(
      uuid, text, text, jsonb
    ) from service_role;
    revoke execute on function public.submit_weekly_post_reveal_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) from service_role;
    revoke insert, update, delete, truncate
      on table public.weekly_quiz_post_reveal_sessions from service_role;
    revoke insert, update, delete, truncate
      on table public.weekly_quiz_post_reveal_vote_attempts from service_role;
    grant select on table public.weekly_quiz_post_reveal_sessions to service_role;
    grant select on table public.weekly_quiz_post_reveal_vote_attempts to service_role;
    grant select on table public.replay_weekly_post_reveal_vote_attempts_safe
      to service_role;
  end if;
end;
$$;

comment on function public.start_named_weekly_post_reveal_session(
  uuid, text, text, jsonb
) is 'Starts one answer-informed session with concurrency-safe idempotency before any identity lookup.';
comment on function public.submit_weekly_post_reveal_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) is 'Submits one append-only answer-informed vote with concurrency-safe idempotency and explicit selection provenance.';
comment on view public.replay_weekly_post_reveal_vote_attempts_safe is
  'Server-only answer-informed vote replay without auth user IDs or plaintext display names.';

notify pgrst, 'reload schema';

commit;
