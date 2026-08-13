-- Let a player refresh the same browser tab without re-entering a name. The
-- browser retains only the opaque session/round identifiers; this RPC binds
-- them back to the current authenticated user without returning the name.
begin;

create or replace function public.resume_named_weekly_quiz_session(
  p_session_id uuid,
  p_round_id text
)
returns table (
  session_id uuid,
  round_id text,
  next_visit_ordinal bigint,
  last_visit_started_at bigint
)
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session_id uuid;
  v_round_id text;
  v_next_visit_ordinal bigint;
  v_last_visit_started_at bigint;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_session_id is null or nullif(p_round_id, '') is null then
    raise exception 'invalid weekly quiz session identity' using errcode = '22023';
  end if;

  select session.session_id, session.round_id
    into v_session_id, v_round_id
      from public.weekly_quiz_sessions session
      join public.weekly_quiz_rounds round
        on round.round_id = session.round_id
     where session.session_id = p_session_id
       and session.round_id = p_round_id
       and session.user_id = v_user_id
       and session.completed_at is null
       and round.status = 'open'
       and clock_timestamp() >= round.opens_at
       and clock_timestamp() < round.closes_at;

  if not found then
    raise exception 'weekly quiz session cannot be resumed' using errcode = 'P0002';
  end if;

  select
    coalesce(max((batch.trace ->> 'visit_ordinal')::bigint) filter (
      where batch.trace ->> 'visit_ordinal' ~ '^[0-9]+$'
    ), -1) + 1,
    coalesce(max((batch.trace ->> 'visit_started_at')::bigint) filter (
      where batch.trace ->> 'visit_started_at' ~ '^[0-9]+$'
    ), -1)
    into v_next_visit_ordinal, v_last_visit_started_at
    from public.weekly_quiz_trace_batches batch
   where batch.session_id = v_session_id
     and batch.round_id = v_round_id
     and batch.user_id = v_user_id;

  return query select
    v_session_id,
    v_round_id,
    v_next_visit_ordinal,
    v_last_visit_started_at;
end;
$$;

comment on function public.resume_named_weekly_quiz_session(uuid, text) is
  'Validates same-user, same-round tab-refresh resumption and returns no participant name.';

revoke all on function public.resume_named_weekly_quiz_session(uuid, text) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.resume_named_weekly_quiz_session(uuid, text) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant execute on function public.resume_named_weekly_quiz_session(uuid, text) to authenticated;
  end if;
end;
$$;

notify pgrst, 'reload schema';

commit;
