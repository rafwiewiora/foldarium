begin;

-- Version-2 continuous batches opt in to lossless sequence validation. Legacy
-- version-1 batches remain readable and insertable during a rolling deployment.
create or replace function public.validate_weekly_trace_stream_v2()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.trace -> 'stream_schema_version' = '2'::jsonb then
    if jsonb_typeof(new.trace -> 'molstar_version') is distinct from 'string'
       or char_length(new.trace ->> 'molstar_version') not between 1 and 40
       or jsonb_typeof(new.trace -> 'visit_started_at') is distinct from 'number'
       or new.trace ->> 'visit_started_at' !~ '^[0-9]+$'
       or jsonb_typeof(new.trace -> 'visit_ordinal') is distinct from 'number'
       or new.trace ->> 'visit_ordinal' !~ '^[0-9]+$'
       or jsonb_array_length(new.trace -> 'entries')
            <> new.last_sequence - new.first_sequence + 1
       or exists (
         select 1
           from jsonb_array_elements(new.trace -> 'entries') with ordinality as entry(value, ordinal_position)
          where entry.value ->> 'seq' !~ '^[0-9]+$'
             or (entry.value ->> 'seq')::integer
                  <> new.first_sequence + entry.ordinal_position - 1
       ) then
      raise exception 'weekly trace v2 continuity or replay metadata is invalid'
        using errcode = '22023';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists validate_weekly_trace_stream_v2
  on public.weekly_quiz_trace_batches;
create trigger validate_weekly_trace_stream_v2
before insert on public.weekly_quiz_trace_batches
for each row execute function public.validate_weekly_trace_stream_v2();

revoke all on function public.validate_weekly_trace_stream_v2()
from public, anon, authenticated;

-- Keep the original ten-argument overload during rolling deployment. Older clients
-- continue to submit legacy snapshots and a NULL vote_comment; new clients select
-- the overload below by including the distinct p_vote_comment named argument.

alter table public.weekly_quiz_vote_attempts
  add column vote_comment text;

alter table public.weekly_quiz_vote_attempts
  add constraint weekly_quiz_vote_attempts_vote_comment_shape
  check (
    vote_comment is null
    or (
      vote_comment = btrim(vote_comment)
      and char_length(vote_comment) between 1 and 4000
      and octet_length(vote_comment) <= 16000
    )
  );

drop view public.replay_weekly_vote_attempts_safe;

create or replace function public.submit_weekly_quiz_vote_attempt(
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
returns public.weekly_quiz_vote_attempts
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session public.weekly_quiz_sessions%rowtype;
  v_round public.weekly_quiz_rounds%rowtype;
  v_attempt public.weekly_quiz_vote_attempts%rowtype;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_vote_attempt_id is null or p_session_id is null
     or nullif(p_round_id, '') is null or nullif(p_item_id, '') is null
     or p_question_index is null or p_question_index < 0
     or p_picked_none is null
     or (p_picked_none and p_choice_id is not null)
     or (not p_picked_none and nullif(p_choice_id, '') is null) then
    raise exception 'invalid weekly vote attempt identity' using errcode = '22023';
  end if;
  if p_viewer_trace is not null and (
       jsonb_typeof(p_viewer_trace) is distinct from 'object'
       or octet_length(p_viewer_trace::text) > 524288
     ) then
    raise exception 'viewer trace is invalid or too large' using errcode = '22023';
  end if;
  if p_app_state is not null and (
       jsonb_typeof(p_app_state) is distinct from 'object'
       or octet_length(p_app_state::text) > 65536
       or exists (
         select 1 from jsonb_object_keys(p_app_state) as app_key(value)
          where regexp_replace(lower(app_key.value), '[^a-z0-9]', '', 'g') = 'votecomment'
       )
     ) then
    raise exception 'app state is invalid or too large' using errcode = '22023';
  end if;
  if p_active_pane_id is not null and (
       char_length(p_active_pane_id) not between 1 and 100
       or p_active_pane_id !~ '^[A-Za-z0-9._:-]+$'
     ) then
    raise exception 'active pane identity is invalid' using errcode = '22023';
  end if;
  if p_vote_comment is not null and (
       p_vote_comment <> btrim(p_vote_comment)
       or char_length(p_vote_comment) not between 1 and 4000
       or octet_length(p_vote_comment) > 16000
     ) then
    raise exception 'vote comment is invalid or too large' using errcode = '22023';
  end if;

  select * into v_attempt
    from public.weekly_quiz_vote_attempts
   where vote_attempt_id = p_vote_attempt_id;
  if found then
    if v_attempt.user_id <> v_user_id
       or v_attempt.session_id <> p_session_id
       or v_attempt.round_id <> p_round_id
       or v_attempt.item_id <> p_item_id
       or v_attempt.question_index <> p_question_index
       or v_attempt.choice_id is distinct from p_choice_id
       or v_attempt.picked_none <> p_picked_none
       or v_attempt.viewer_trace is distinct from p_viewer_trace
       or v_attempt.app_state is distinct from p_app_state
       or v_attempt.active_pane_id is distinct from p_active_pane_id
       or v_attempt.vote_comment is distinct from p_vote_comment then
      raise exception 'vote attempt identity is already bound to different content'
        using errcode = '23505';
    end if;
    return v_attempt;
  end if;

  select * into v_session
    from public.weekly_quiz_sessions
   where session_id = p_session_id
   for share;
  if not found or v_session.user_id <> v_user_id
     or v_session.round_id <> p_round_id or v_session.completed_at is not null then
    raise exception 'weekly quiz session is not accepting votes' using errcode = '23514';
  end if;
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found or v_round.status <> 'open'
     or clock_timestamp() < v_round.opens_at or clock_timestamp() >= v_round.closes_at then
    raise exception 'weekly round is not accepting votes' using errcode = '23514';
  end if;
  if not exists (
    select 1
      from jsonb_array_elements(v_round.blind_manifest -> 'items')
        with ordinality as item(value, ordinal_position)
     where item.value ->> 'id' = p_item_id
       and item.ordinal_position - 1 = p_question_index
       and (
         (p_picked_none and p_choice_id is null)
         or (not p_picked_none and exists (
           select 1 from jsonb_array_elements(item.value -> 'choices') as choice(value)
            where choice.value ->> 'id' = p_choice_id
         ))
       )
  ) then
    raise exception 'vote does not reference a published item/choice' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtextextended('weekly-vote:' || v_user_id::text, 0));
  if (select count(*) from public.weekly_quiz_vote_attempts
       where user_id = v_user_id and submitted_at >= clock_timestamp() - interval '1 minute') >= 60
     or (select count(*) from public.weekly_quiz_vote_attempts
       where user_id = v_user_id and submitted_at >= clock_timestamp() - interval '1 hour') >= 600 then
    raise exception 'too many weekly vote attempts; try again later' using errcode = '42900';
  end if;

  insert into public.weekly_quiz_vote_attempts (
    vote_attempt_id, session_id, round_id, user_id, item_id, question_index,
    choice_id, picked_none, viewer_trace, app_state, active_pane_id, vote_comment
  ) values (
    p_vote_attempt_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, p_viewer_trace, p_app_state, p_active_pane_id, p_vote_comment
  ) returning * into v_attempt;

  insert into public.weekly_quiz_votes (
    vote_id, round_id, user_id, item_id, choice_id, picked_none, submitted_at
  ) values (
    p_vote_attempt_id, p_round_id, v_user_id, p_item_id,
    p_choice_id, p_picked_none, v_attempt.submitted_at
  ) on conflict (round_id, user_id, item_id) do update
       set choice_id = excluded.choice_id,
           picked_none = excluded.picked_none,
           submitted_at = excluded.submitted_at;
  return v_attempt;
end;
$$;

create view public.replay_weekly_vote_attempts_safe
with (security_barrier = true, security_invoker = true)
as
select
  vote.vote_attempt_id, vote.session_id, vote.round_id,
  session.participant_hash, session.display_name_hash,
  vote.item_id, vote.question_index, vote.choice_id, vote.picked_none,
  vote.viewer_trace, vote.app_state, vote.active_pane_id, vote.vote_comment,
  vote.submitted_at
from public.weekly_quiz_vote_attempts as vote
join public.weekly_quiz_sessions as session using (session_id, round_id, user_id);

revoke all on function public.submit_weekly_quiz_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) from public, anon;
grant execute on function public.submit_weekly_quiz_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) to authenticated;
revoke all on table public.replay_weekly_vote_attempts_safe from public, anon, authenticated;
grant select on table public.replay_weekly_vote_attempts_safe to service_role;

comment on column public.weekly_quiz_vote_attempts.vote_comment is
  'Compact append-only note attached to this exact vote revision; retained independently of archived traces.';

comment on function public.submit_weekly_quiz_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) is 'Submits one append-only weekly vote revision with an independently retained compact comment.';

commit;
