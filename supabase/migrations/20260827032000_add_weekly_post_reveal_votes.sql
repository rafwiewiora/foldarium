begin;

-- Answer-informed play is useful research data, but it must never overwrite or
-- enter the blind-week ballot projection. Keep it in physically separate tables
-- and label every row at the database boundary.

create table public.weekly_quiz_post_reveal_sessions (
  session_id uuid primary key,
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  display_name text not null,
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  participation_phase text not null default 'post_reveal'
    check (participation_phase = 'post_reveal'),
  initial_app_state jsonb,
  started_at timestamptz not null default clock_timestamp(),
  completed_at timestamptz,
  created_at timestamptz not null default clock_timestamp(),
  unique (session_id, round_id, user_id),
  check (
    display_name = regexp_replace(btrim(display_name), '[[:space:]]+', ' ', 'g')
    and char_length(display_name) between 1 and 80
    and octet_length(display_name) <= 320
    and display_name !~ '[[:cntrl:]]'
  ),
  check (
    initial_app_state is null
    or (
      jsonb_typeof(initial_app_state) = 'object'
      and octet_length(initial_app_state::text) <= 65536
    ) is true
  ),
  check (completed_at is null or completed_at >= started_at)
);

create table public.weekly_quiz_post_reveal_vote_attempts (
  vote_attempt_id uuid primary key,
  session_id uuid not null,
  round_id text not null,
  user_id uuid not null,
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  choice_id text,
  picked_none boolean not null,
  selection_kind public.weekly_quiz_selection_kind not null,
  selection_id text,
  submission_phase text not null default 'post_reveal'
    check (submission_phase = 'post_reveal'),
  viewer_trace jsonb,
  app_state jsonb not null,
  active_pane_id text,
  vote_comment text,
  submitted_at timestamptz not null default clock_timestamp(),
  created_at timestamptz not null default clock_timestamp(),
  foreign key (session_id, round_id, user_id)
    references public.weekly_quiz_post_reveal_sessions(session_id, round_id, user_id)
    on delete cascade,
  check (
    (picked_none and choice_id is null and selection_kind = 'none' and selection_id is null)
    or (
      not picked_none
      and nullif(choice_id, '') is not null
      and selection_kind in ('cluster', 'exact')
      and nullif(selection_id, '') is not null
    )
  ),
  check (
    viewer_trace is null
    or (
      jsonb_typeof(viewer_trace) = 'object'
      and octet_length(viewer_trace::text) <= 524288
    ) is true
  ),
  check (
    jsonb_typeof(app_state) = 'object'
    and octet_length(app_state::text) <= 65536
  ),
  check (
    active_pane_id is null
    or (
      char_length(active_pane_id) between 1 and 100
      and active_pane_id ~ '^[A-Za-z0-9._:-]+$'
    )
  ),
  check (
    vote_comment is null
    or (
      vote_comment = btrim(vote_comment)
      and char_length(vote_comment) between 1 and 4000
      and octet_length(vote_comment) <= 16000
    )
  )
);

create index weekly_post_reveal_sessions_user_started_idx
  on public.weekly_quiz_post_reveal_sessions (user_id, started_at desc);
create index weekly_post_reveal_attempts_latest_idx
  on public.weekly_quiz_post_reveal_vote_attempts
    (round_id, user_id, item_id, submitted_at desc, vote_attempt_id desc);
create index weekly_post_reveal_attempts_rate_idx
  on public.weekly_quiz_post_reveal_vote_attempts (user_id, submitted_at desc);

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
declare
  v_user_id uuid;
  v_display_name text;
  v_participant_hash text;
  v_display_name_hash text;
  v_round public.weekly_quiz_rounds%rowtype;
  v_session public.weekly_quiz_post_reveal_sessions%rowtype;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_session_id is null or nullif(p_round_id, '') is null then
    raise exception 'invalid post-reveal session identity' using errcode = '22023';
  end if;
  if p_initial_app_state is not null and (
       jsonb_typeof(p_initial_app_state) is distinct from 'object'
       or octet_length(p_initial_app_state::text) > 65536
     ) then
    raise exception 'initial app state is invalid or too large' using errcode = '22023';
  end if;

  v_display_name := regexp_replace(btrim(p_display_name), '[[:space:]]+', ' ', 'g');
  if nullif(v_display_name, '') is null
     or char_length(v_display_name) > 80
     or octet_length(v_display_name) > 320
     or v_display_name ~ '[[:cntrl:]]' then
    raise exception 'display name must be 1-80 characters without control characters'
      using errcode = '22023';
  end if;

  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found
     or v_round.status <> 'revealed'
     or v_round.reveal_manifest is null
     or v_round.revealed_at is null then
    raise exception 'weekly round is not accepting post-reveal participants'
      using errcode = '23514';
  end if;

  v_participant_hash := private.foldarium_identity_hmac(
    'participant', v_user_id::text
  );
  v_display_name_hash := private.foldarium_identity_hmac(
    'display-name', v_user_id::text || ':' || lower(v_display_name)
  );

  select * into v_session
    from public.weekly_quiz_post_reveal_sessions
   where session_id = p_session_id
   for update;
  if found then
    if v_session.user_id <> v_user_id
       or v_session.round_id <> p_round_id
       or v_session.display_name <> v_display_name
       or v_session.participant_hash <> v_participant_hash
       or v_session.display_name_hash <> v_display_name_hash
       or v_session.initial_app_state is distinct from p_initial_app_state then
      raise exception 'post-reveal session identity is already in use'
        using errcode = '23505';
    end if;
    return v_session;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(
    'weekly-post-reveal-session:' || v_user_id::text, 0
  ));
  if (
    select count(*)
      from public.weekly_quiz_post_reveal_sessions
     where user_id = v_user_id
       and created_at >= clock_timestamp() - interval '1 hour'
  ) >= 30 then
    raise exception 'too many post-reveal sessions; try again later'
      using errcode = '42900';
  end if;

  insert into public.weekly_quiz_post_reveal_sessions (
    session_id, round_id, user_id, display_name,
    participant_hash, display_name_hash, initial_app_state
  ) values (
    p_session_id, p_round_id, v_user_id, v_display_name,
    v_participant_hash, v_display_name_hash, p_initial_app_state
  )
  returning * into v_session;
  return v_session;
end;
$$;

create or replace function public.resume_named_weekly_post_reveal_session(
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
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_session_id is null or nullif(p_round_id, '') is null then
    raise exception 'invalid post-reveal session identity' using errcode = '22023';
  end if;
  if not exists (
    select 1
      from public.weekly_quiz_post_reveal_sessions session
      join public.weekly_quiz_rounds quiz_round using (round_id)
     where session.session_id = p_session_id
       and session.round_id = p_round_id
       and session.user_id = v_user_id
       and session.completed_at is null
       and quiz_round.status = 'revealed'
       and quiz_round.reveal_manifest is not null
       and quiz_round.revealed_at is not null
  ) then
    raise exception 'post-reveal session cannot be resumed' using errcode = 'P0002';
  end if;
  return query select p_session_id, p_round_id, 0::bigint, (-1)::bigint;
end;
$$;

create or replace function public.complete_named_weekly_post_reveal_session(
  p_session_id uuid
)
returns public.weekly_quiz_post_reveal_sessions
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session public.weekly_quiz_post_reveal_sessions%rowtype;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  update public.weekly_quiz_post_reveal_sessions
     set completed_at = coalesce(completed_at, clock_timestamp())
   where session_id = p_session_id
     and user_id = v_user_id
   returning * into v_session;
  if not found then
    raise exception 'unknown post-reveal session' using errcode = 'P0002';
  end if;
  return v_session;
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
declare
  v_user_id uuid;
  v_session public.weekly_quiz_post_reveal_sessions%rowtype;
  v_round public.weekly_quiz_rounds%rowtype;
  v_attempt public.weekly_quiz_post_reveal_vote_attempts%rowtype;
  v_item jsonb;
  v_choice jsonb;
  v_selection_kind public.weekly_quiz_selection_kind;
  v_selection_id text;
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
    raise exception 'invalid post-reveal vote identity' using errcode = '22023';
  end if;
  if p_viewer_trace is not null and (
       jsonb_typeof(p_viewer_trace) is distinct from 'object'
       or octet_length(p_viewer_trace::text) > 524288
     ) then
    raise exception 'viewer trace is invalid or too large' using errcode = '22023';
  end if;
  if p_app_state is null
     or jsonb_typeof(p_app_state) is distinct from 'object'
     or octet_length(p_app_state::text) > 65536
     or p_app_state ->> 'selection_kind' is null
     or p_app_state ->> 'selection_kind' not in ('cluster', 'exact', 'none')
     or exists (
       select 1 from jsonb_object_keys(
         case when jsonb_typeof(p_app_state) = 'object'
           then p_app_state else '{}'::jsonb end
       ) as app_key(value)
        where regexp_replace(lower(app_key.value), '[^a-z0-9]', '', 'g') = 'votecomment'
     ) then
    raise exception 'post-reveal app state is invalid or lacks selection provenance'
      using errcode = '22023';
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
    from public.weekly_quiz_post_reveal_vote_attempts
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
      raise exception 'post-reveal vote identity is already bound to different content'
        using errcode = '23505';
    end if;
    return v_attempt;
  end if;

  select * into v_session
    from public.weekly_quiz_post_reveal_sessions
   where session_id = p_session_id
   for share;
  if not found
     or v_session.user_id <> v_user_id
     or v_session.round_id <> p_round_id
     or v_session.completed_at is not null then
    raise exception 'post-reveal session is not accepting votes' using errcode = '23514';
  end if;
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found
     or v_round.status <> 'revealed'
     or v_round.reveal_manifest is null
     or v_round.revealed_at is null then
    raise exception 'weekly round is not accepting post-reveal votes'
      using errcode = '23514';
  end if;

  select item.value into v_item
    from jsonb_array_elements(v_round.blind_manifest -> 'items')
      with ordinality as item(value, ordinal_position)
   where item.value ->> 'id' = p_item_id
     and item.ordinal_position - 1 = p_question_index;
  if v_item is null then
    raise exception 'vote does not reference a published item'
      using errcode = '22023';
  end if;

  if p_picked_none then
    if p_app_state ->> 'selection_kind' <> 'none' then
      raise exception 'post-reveal none selection provenance is inconsistent'
        using errcode = '22023';
    end if;
    v_selection_kind := 'none';
    v_selection_id := null;
  else
    select choice.value into v_choice
      from jsonb_array_elements(v_item -> 'choices') as choice(value)
     where choice.value ->> 'id' = p_choice_id;
    if v_choice is null then
      raise exception 'vote does not reference a published choice'
        using errcode = '22023';
    end if;
    if p_app_state ->> 'selection_kind' = 'exact' then
      if p_app_state ->> 'selected_choice_id' is distinct from p_choice_id then
        raise exception 'post-reveal exact selection identity is inconsistent'
          using errcode = '22023';
      end if;
      v_selection_kind := 'exact';
      v_selection_id := p_choice_id;
    elsif p_app_state ->> 'selection_kind' = 'cluster'
          and nullif(v_choice ->> 'cluster_id', '') is not null then
      if jsonb_typeof(p_app_state -> 'selected_choice_ids') is distinct from 'array'
         or not (p_app_state -> 'selected_choice_ids' @> jsonb_build_array(p_choice_id)) then
        raise exception 'post-reveal cluster selection identity is inconsistent'
          using errcode = '22023';
      end if;
      v_selection_kind := 'cluster';
      v_selection_id := v_choice ->> 'cluster_id';
    else
      raise exception 'post-reveal selection provenance is inconsistent'
        using errcode = '22023';
    end if;
  end if;

  perform pg_advisory_xact_lock(hashtextextended(
    'weekly-post-reveal-vote:' || v_user_id::text, 0
  ));
  if (
    select count(*)
      from public.weekly_quiz_post_reveal_vote_attempts
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 minute'
  ) >= 60 or (
    select count(*)
      from public.weekly_quiz_post_reveal_vote_attempts
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 hour'
  ) >= 600 then
    raise exception 'too many post-reveal vote attempts; try again later'
      using errcode = '42900';
  end if;

  insert into public.weekly_quiz_post_reveal_vote_attempts (
    vote_attempt_id, session_id, round_id, user_id, item_id, question_index,
    choice_id, picked_none, selection_kind, selection_id,
    viewer_trace, app_state, active_pane_id, vote_comment
  ) values (
    p_vote_attempt_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, v_selection_kind, v_selection_id,
    p_viewer_trace, p_app_state, p_active_pane_id, p_vote_comment
  )
  returning * into v_attempt;
  return v_attempt;
end;
$$;

create or replace function public.get_my_weekly_post_reveal_votes(p_round_id text)
returns table (
  item_id text,
  choice_id text,
  picked_none boolean,
  selection_kind public.weekly_quiz_selection_kind,
  selection_id text,
  submission_phase text,
  submitted_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog
as $$
  select latest.item_id, latest.choice_id, latest.picked_none,
         latest.selection_kind, latest.selection_id, latest.submission_phase,
         latest.submitted_at
    from (
      select distinct on (attempt.item_id) attempt.*
        from public.weekly_quiz_post_reveal_vote_attempts attempt
       where attempt.round_id = p_round_id
         and attempt.user_id = auth.uid()
       order by attempt.item_id, attempt.submitted_at desc, attempt.vote_attempt_id desc
    ) latest
   order by latest.item_id
$$;

create or replace view public.replay_weekly_post_reveal_vote_attempts_safe
with (security_barrier = true, security_invoker = true)
as
select
  attempt.vote_attempt_id, attempt.session_id, attempt.round_id,
  session.participant_hash, session.display_name_hash, session.display_name,
  attempt.item_id, attempt.question_index, attempt.choice_id, attempt.picked_none,
  attempt.selection_kind, attempt.selection_id, attempt.submission_phase,
  attempt.viewer_trace, attempt.app_state, attempt.active_pane_id,
  attempt.vote_comment, attempt.submitted_at
from public.weekly_quiz_post_reveal_vote_attempts attempt
join public.weekly_quiz_post_reveal_sessions session
  using (session_id, round_id, user_id);

alter table public.weekly_quiz_post_reveal_sessions enable row level security;
alter table public.weekly_quiz_post_reveal_vote_attempts enable row level security;

revoke all on table public.weekly_quiz_post_reveal_sessions from public;
revoke all on table public.weekly_quiz_post_reveal_vote_attempts from public;
revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe from public;
revoke all on function public.start_named_weekly_post_reveal_session(
  uuid, text, text, jsonb
) from public;
revoke all on function public.resume_named_weekly_post_reveal_session(uuid, text) from public;
revoke all on function public.complete_named_weekly_post_reveal_session(uuid) from public;
revoke all on function public.submit_weekly_post_reveal_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
) from public;
revoke all on function public.get_my_weekly_post_reveal_votes(text) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_quiz_post_reveal_sessions from anon;
    revoke all on table public.weekly_quiz_post_reveal_vote_attempts from anon;
    revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_quiz_post_reveal_sessions from authenticated;
    revoke all on table public.weekly_quiz_post_reveal_vote_attempts from authenticated;
    revoke all on table public.replay_weekly_post_reveal_vote_attempts_safe from authenticated;
    grant execute on function public.start_named_weekly_post_reveal_session(
      uuid, text, text, jsonb
    ) to authenticated;
    grant execute on function public.resume_named_weekly_post_reveal_session(uuid, text)
      to authenticated;
    grant execute on function public.complete_named_weekly_post_reveal_session(uuid)
      to authenticated;
    grant execute on function public.submit_weekly_post_reveal_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, text, jsonb, jsonb, text
    ) to authenticated;
    grant execute on function public.get_my_weekly_post_reveal_votes(text)
      to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant select on table public.replay_weekly_post_reveal_vote_attempts_safe
      to service_role;
  end if;
end;
$$;

comment on table public.weekly_quiz_post_reveal_vote_attempts is
  'Append-only answer-informed votes, physically excluded from blind-week ballots and leaderboards.';
comment on column public.weekly_quiz_post_reveal_vote_attempts.submission_phase is
  'Server-enforced post_reveal annotation; never accepted from the client.';

notify pgrst, 'reload schema';

commit;
