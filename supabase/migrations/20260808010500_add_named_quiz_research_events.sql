-- Named quiz participation, append-only weekly vote research events, and feedback.
--
-- The HMAC key is generated inside Postgres when this migration is applied. It is
-- never returned by a public function or stored in this repository.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

do $$
begin
  if not exists (
    select 1
      from pg_extension as extension
      join pg_namespace as namespace on namespace.oid = extension.extnamespace
     where extension.extname = 'pgcrypto'
       and namespace.nspname = 'extensions'
  ) then
    raise exception 'pgcrypto must be installed in the extensions schema'
      using errcode = '55000';
  end if;
end;
$$;

create schema if not exists private;
revoke all on schema private from public;

create table if not exists private.foldarium_secrets (
  secret_name text primary key,
  secret_value bytea not null check (octet_length(secret_value) = 32),
  created_at timestamptz not null default clock_timestamp()
);

revoke all on table private.foldarium_secrets from public;

insert into private.foldarium_secrets (secret_name, secret_value)
values ('participant-hmac-v1', extensions.gen_random_bytes(32))
on conflict (secret_name) do nothing;

create or replace function private.foldarium_identity_hmac(
  p_domain text,
  p_value text
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog
as $$
declare
  v_secret bytea;
begin
  if p_domain not in ('participant', 'display-name') or nullif(p_value, '') is null then
    raise exception 'invalid identity hash input' using errcode = '22023';
  end if;

  select secret_value
    into v_secret
    from private.foldarium_secrets
   where secret_name = 'participant-hmac-v1';

  if v_secret is null or octet_length(v_secret) <> 32 then
    raise exception 'participant hashing is not configured' using errcode = '55000';
  end if;

  return encode(
    extensions.hmac(
      convert_to(p_domain || ':' || p_value, 'UTF8'),
      v_secret,
      'sha256'
    ),
    'hex'
  );
end;
$$;

revoke all on function private.foldarium_identity_hmac(text, text) from public;

alter table public.quiz_sessions
  add column if not exists display_name text,
  add column if not exists participant_hash text,
  add column if not exists display_name_hash text,
  add column if not exists identity_version smallint not null default 0,
  add column if not exists name_recorded_at timestamptz;

alter table public.quiz_sessions
  add constraint quiz_sessions_identity_version_check
  check (identity_version in (0, 1)),
  add constraint quiz_sessions_named_identity_complete
  check (
    identity_version = 0
    or (
      display_name is not null
      and participant_hash ~ '^[0-9a-f]{64}$'
      and display_name_hash ~ '^[0-9a-f]{64}$'
      and name_recorded_at is not null
    )
  ),
  add constraint quiz_sessions_display_name_shape
  check (
    display_name is null
    or (
      display_name = regexp_replace(btrim(display_name), '[[:space:]]+', ' ', 'g')
      and char_length(display_name) between 1 and 80
      and octet_length(display_name) <= 320
      and display_name !~ '[[:cntrl:]]'
    )
  );

create index if not exists quiz_sessions_participant_started_idx
  on public.quiz_sessions (participant_hash, started_at desc)
  where identity_version = 1;

-- Existing clients may continue inserting the original session fields, but only
-- the server-side RPC can populate HMAC-backed identity columns.
revoke insert on table public.quiz_sessions from authenticated;
grant insert (id, user_id, source, difficulty, started_at, completed_at)
  on table public.quiz_sessions to authenticated;

alter table public.quiz_answers
  add column if not exists app_trace jsonb,
  add column if not exists app_state jsonb,
  add column if not exists active_pane_id text;

-- NOT VALID preserves any historical oversized trace. PostgreSQL still enforces
-- the constraint for every new or updated row after this migration.
alter table public.quiz_answers
  add constraint quiz_answers_viewer_trace_max_bytes
  check (viewer_trace is null or octet_length(viewer_trace::text) <= 524288)
  not valid,
  add constraint quiz_answers_app_trace_shape_and_size
  check (
    app_trace is null
    or (
      jsonb_typeof(app_trace) in ('object', 'array')
      and octet_length(app_trace::text) <= 262144
    ) is true
  ),
  add constraint quiz_answers_app_state_shape_and_size
  check (
    app_state is null
    or (
      jsonb_typeof(app_state) = 'object'
      and octet_length(app_state::text) <= 65536
    ) is true
  ),
  add constraint quiz_answers_active_pane_id_shape
  check (
    active_pane_id is null
    or (
      char_length(active_pane_id) between 1 and 100
      and active_pane_id ~ '^[A-Za-z0-9._:-]+$'
    )
  );

create table public.weekly_quiz_sessions (
  session_id uuid primary key,
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  display_name text not null,
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  identity_version smallint not null default 1 check (identity_version = 1),
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

create index weekly_quiz_sessions_user_started_idx
  on public.weekly_quiz_sessions (user_id, started_at desc);
create index weekly_quiz_sessions_round_started_idx
  on public.weekly_quiz_sessions (round_id, started_at desc);

create table public.weekly_quiz_vote_attempts (
  vote_attempt_id uuid primary key,
  session_id uuid not null,
  round_id text not null,
  user_id uuid not null,
  item_id text not null check (char_length(item_id) between 1 and 200),
  question_index integer not null check (question_index >= 0),
  choice_id text,
  picked_none boolean not null,
  viewer_trace jsonb,
  app_state jsonb,
  active_pane_id text,
  submitted_at timestamptz not null default clock_timestamp(),
  created_at timestamptz not null default clock_timestamp(),
  foreign key (session_id, round_id, user_id)
    references public.weekly_quiz_sessions(session_id, round_id, user_id)
    on delete cascade,
  check (
    (picked_none and choice_id is null)
    or (not picked_none and nullif(choice_id, '') is not null)
  ),
  check (
    viewer_trace is null
    or (
      jsonb_typeof(viewer_trace) = 'object'
      and octet_length(viewer_trace::text) <= 524288
    ) is true
  ),
  check (
    app_state is null
    or (
      jsonb_typeof(app_state) = 'object'
      and octet_length(app_state::text) <= 65536
    ) is true
  ),
  check (
    active_pane_id is null
    or (
      char_length(active_pane_id) between 1 and 100
      and active_pane_id ~ '^[A-Za-z0-9._:-]+$'
    )
  )
);

create index weekly_quiz_vote_attempts_session_question_idx
  on public.weekly_quiz_vote_attempts (session_id, question_index, submitted_at);
create index weekly_quiz_vote_attempts_user_rate_idx
  on public.weekly_quiz_vote_attempts (user_id, submitted_at desc);

create table public.user_suggestions (
  suggestion_id uuid primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  participant_hash text not null check (participant_hash ~ '^[0-9a-f]{64}$'),
  display_name_hash text not null check (display_name_hash ~ '^[0-9a-f]{64}$'),
  quiz_session_id uuid references public.quiz_sessions(id) on delete cascade,
  weekly_session_id uuid references public.weekly_quiz_sessions(session_id) on delete cascade,
  context text not null
    check (
      char_length(context) between 1 and 64
      and context ~ '^[a-z0-9][a-z0-9_-]*$'
    ),
  item_id text,
  page_path text,
  suggestion_text text not null,
  app_state jsonb,
  viewer_snapshot jsonb,
  viewer_trace_tail jsonb,
  submitted_at timestamptz not null default clock_timestamp(),
  created_at timestamptz not null default clock_timestamp(),
  check (num_nonnulls(quiz_session_id, weekly_session_id) = 1),
  check (
    item_id is null
    or (
      char_length(item_id) between 1 and 200
      and octet_length(item_id) <= 800
      and item_id !~ '[[:cntrl:]]'
    )
  ),
  check (
    suggestion_text = btrim(suggestion_text)
    and char_length(suggestion_text) between 1 and 4000
    and octet_length(suggestion_text) <= 16000
  ),
  check (
    page_path is null
    or (
      char_length(page_path) between 1 and 512
      and octet_length(page_path) <= 2048
      and left(page_path, 1) = '/'
      and page_path !~ '[[:cntrl:]]'
    )
  ),
  check (
    app_state is null
    or (
      jsonb_typeof(app_state) = 'object'
      and octet_length(app_state::text) <= 65536
    ) is true
  ),
  check (
    viewer_snapshot is null
    or (
      jsonb_typeof(viewer_snapshot) = 'object'
      and octet_length(viewer_snapshot::text) <= 131072
    ) is true
  ),
  check (
    viewer_trace_tail is null
    or (
      jsonb_typeof(viewer_trace_tail) in ('object', 'array')
      and octet_length(viewer_trace_tail::text) <= 131072
    ) is true
  )
);

create index user_suggestions_user_rate_idx
  on public.user_suggestions (user_id, submitted_at desc);
create index user_suggestions_context_submitted_idx
  on public.user_suggestions (context, submitted_at desc);

create or replace function public.start_named_quiz_session(
  p_session_id uuid,
  p_source text,
  p_difficulty text,
  p_display_name text
)
returns public.quiz_sessions
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_display_name text;
  v_participant_hash text;
  v_display_name_hash text;
  v_session public.quiz_sessions%rowtype;
begin
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_session_id is null
     or p_source not in ('cameo', 'rnp')
     or p_difficulty not in ('easy', 'hard') then
    raise exception 'invalid quiz session identity' using errcode = '22023';
  end if;

  v_display_name := regexp_replace(btrim(p_display_name), '[[:space:]]+', ' ', 'g');
  if nullif(v_display_name, '') is null
     or char_length(v_display_name) > 80
     or octet_length(v_display_name) > 320
     or v_display_name ~ '[[:cntrl:]]' then
    raise exception 'display name must be 1-80 characters without control characters'
      using errcode = '22023';
  end if;

  v_participant_hash := private.foldarium_identity_hmac(
    'participant', v_user_id::text
  );
  v_display_name_hash := private.foldarium_identity_hmac(
    'display-name', v_user_id::text || ':' || lower(v_display_name)
  );

  select * into v_session
    from public.quiz_sessions
   where id = p_session_id
   for update;
  if found then
    if v_session.user_id <> v_user_id
       or v_session.source <> p_source
       or v_session.difficulty <> p_difficulty then
      raise exception 'quiz session identity is already in use' using errcode = '23505';
    end if;
    if v_session.identity_version = 1 then
      if v_session.display_name <> v_display_name
         or v_session.participant_hash <> v_participant_hash
         or v_session.display_name_hash <> v_display_name_hash then
        raise exception 'quiz session identity is already bound to another name'
          using errcode = '23505';
      end if;
      return v_session;
    end if;
    if v_session.completed_at is not null then
      raise exception 'a completed legacy session cannot be named retroactively'
        using errcode = '23514';
    end if;
    update public.quiz_sessions
       set display_name = v_display_name,
           participant_hash = v_participant_hash,
           display_name_hash = v_display_name_hash,
           identity_version = 1,
           name_recorded_at = clock_timestamp()
     where id = p_session_id
     returning * into v_session;
    return v_session;
  end if;

  perform pg_advisory_xact_lock(hashtextextended('named-session:' || v_user_id::text, 0));
  if (
    select count(*)
      from public.quiz_sessions
     where user_id = v_user_id
       and identity_version = 1
       and created_at >= clock_timestamp() - interval '1 hour'
  ) >= 30 then
    raise exception 'too many quiz sessions; try again later' using errcode = '42900';
  end if;

  insert into public.quiz_sessions (
    id, user_id, source, difficulty, started_at,
    display_name, participant_hash, display_name_hash,
    identity_version, name_recorded_at
  ) values (
    p_session_id, v_user_id, p_source, p_difficulty, clock_timestamp(),
    v_display_name, v_participant_hash, v_display_name_hash,
    1, clock_timestamp()
  )
  returning * into v_session;
  return v_session;
end;
$$;

create or replace function public.start_named_weekly_quiz_session(
  p_session_id uuid,
  p_round_id text,
  p_display_name text,
  p_initial_app_state jsonb default null
)
returns public.weekly_quiz_sessions
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
  v_session public.weekly_quiz_sessions%rowtype;
begin
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  if p_session_id is null or nullif(p_round_id, '') is null then
    raise exception 'invalid weekly quiz session identity' using errcode = '22023';
  end if;
  if p_initial_app_state is not null
     and (
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
  v_participant_hash := private.foldarium_identity_hmac(
    'participant', v_user_id::text
  );
  v_display_name_hash := private.foldarium_identity_hmac(
    'display-name', v_user_id::text || ':' || lower(v_display_name)
  );

  select * into v_session
    from public.weekly_quiz_sessions
   where session_id = p_session_id
   for update;
  if found then
    if v_session.user_id <> v_user_id
       or v_session.round_id <> p_round_id
       or v_session.display_name <> v_display_name
       or v_session.participant_hash <> v_participant_hash
       or v_session.display_name_hash <> v_display_name_hash
       or v_session.initial_app_state is distinct from p_initial_app_state then
      raise exception 'weekly quiz session identity is already in use'
        using errcode = '23505';
    end if;
    return v_session;
  end if;

  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found
     or v_round.status <> 'open'
     or clock_timestamp() < v_round.opens_at
     or clock_timestamp() >= v_round.closes_at then
    raise exception 'weekly round is not accepting participants' using errcode = '23514';
  end if;

  perform pg_advisory_xact_lock(hashtextextended('weekly-session:' || v_user_id::text, 0));
  if (
    select count(*)
      from public.weekly_quiz_sessions
     where user_id = v_user_id
       and created_at >= clock_timestamp() - interval '1 hour'
  ) >= 30 then
    raise exception 'too many weekly quiz sessions; try again later' using errcode = '42900';
  end if;

  insert into public.weekly_quiz_sessions (
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

create or replace function public.complete_named_weekly_quiz_session(
  p_session_id uuid
)
returns public.weekly_quiz_sessions
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_session public.weekly_quiz_sessions%rowtype;
begin
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  update public.weekly_quiz_sessions
     set completed_at = coalesce(completed_at, clock_timestamp())
   where session_id = p_session_id
     and user_id = v_user_id
   returning * into v_session;
  if not found then
    raise exception 'unknown weekly quiz session' using errcode = 'P0002';
  end if;
  return v_session;
end;
$$;

create or replace function public.submit_weekly_quiz_vote_attempt(
  p_vote_attempt_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_choice_id text,
  p_picked_none boolean,
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
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
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
  if p_viewer_trace is not null
     and (
       jsonb_typeof(p_viewer_trace) is distinct from 'object'
       or octet_length(p_viewer_trace::text) > 524288
     ) then
    raise exception 'viewer trace is invalid or too large' using errcode = '22023';
  end if;
  if p_app_state is not null
     and (
       jsonb_typeof(p_app_state) is distinct from 'object'
       or octet_length(p_app_state::text) > 65536
     ) then
    raise exception 'app state is invalid or too large' using errcode = '22023';
  end if;
  if p_active_pane_id is not null
     and (
       char_length(p_active_pane_id) not between 1 and 100
       or p_active_pane_id !~ '^[A-Za-z0-9._:-]+$'
     ) then
    raise exception 'active pane identity is invalid' using errcode = '22023';
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
       or v_attempt.active_pane_id is distinct from p_active_pane_id then
      raise exception 'vote attempt identity is already bound to different content'
        using errcode = '23505';
    end if;
    return v_attempt;
  end if;

  select * into v_session
    from public.weekly_quiz_sessions
   where session_id = p_session_id
   for share;
  if not found
     or v_session.user_id <> v_user_id
     or v_session.round_id <> p_round_id
     or v_session.completed_at is not null then
    raise exception 'weekly quiz session is not accepting votes' using errcode = '23514';
  end if;
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = p_round_id
   for share;
  if not found
     or v_round.status <> 'open'
     or clock_timestamp() < v_round.opens_at
     or clock_timestamp() >= v_round.closes_at then
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
         or (
           not p_picked_none
           and exists (
             select 1
               from jsonb_array_elements(item.value -> 'choices') as choice(value)
              where choice.value ->> 'id' = p_choice_id
           )
         )
       )
  ) then
    raise exception 'vote does not reference a published item/choice'
      using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(hashtextextended('weekly-vote:' || v_user_id::text, 0));
  if (
    select count(*)
      from public.weekly_quiz_vote_attempts
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 minute'
  ) >= 60 or (
    select count(*)
      from public.weekly_quiz_vote_attempts
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 hour'
  ) >= 600 then
    raise exception 'too many weekly vote attempts; try again later'
      using errcode = '42900';
  end if;

  insert into public.weekly_quiz_vote_attempts (
    vote_attempt_id, session_id, round_id, user_id, item_id, question_index,
    choice_id, picked_none, viewer_trace, app_state, active_pane_id
  ) values (
    p_vote_attempt_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, p_viewer_trace, p_app_state, p_active_pane_id
  )
  returning * into v_attempt;

  insert into public.weekly_quiz_votes (
    vote_id, round_id, user_id, item_id, choice_id, picked_none, submitted_at
  ) values (
    p_vote_attempt_id, p_round_id, v_user_id, p_item_id,
    p_choice_id, p_picked_none, v_attempt.submitted_at
  )
  on conflict (round_id, user_id, item_id) do update
     set choice_id = excluded.choice_id,
         picked_none = excluded.picked_none,
         submitted_at = excluded.submitted_at;

  return v_attempt;
end;
$$;

create or replace function public.submit_user_suggestion(
  p_suggestion_id uuid,
  p_suggestion_text text,
  p_context text,
  p_quiz_session_id uuid default null,
  p_weekly_session_id uuid default null,
  p_item_id text default null,
  p_page_path text default null,
  p_app_state jsonb default null,
  p_viewer_snapshot jsonb default null,
  p_viewer_trace_tail jsonb default null
)
returns public.user_suggestions
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_suggestion_text text;
  v_participant_hash text;
  v_display_name_hash text;
  v_weekly_round_id text;
  v_suggestion public.user_suggestions%rowtype;
begin
  v_user_id := nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  v_suggestion_text := btrim(p_suggestion_text);
  if p_suggestion_id is null
     or num_nonnulls(p_quiz_session_id, p_weekly_session_id) <> 1
     or nullif(v_suggestion_text, '') is null
     or char_length(v_suggestion_text) > 4000
     or octet_length(v_suggestion_text) > 16000
     or nullif(p_context, '') is null
     or p_context !~ '^[a-z0-9][a-z0-9_-]{0,63}$' then
    raise exception 'invalid suggestion identity, context, or text' using errcode = '22023';
  end if;
  if p_item_id is not null
     and (
       char_length(p_item_id) not between 1 and 200
       or octet_length(p_item_id) > 800
       or p_item_id ~ '[[:cntrl:]]'
     ) then
    raise exception 'suggestion item identity is invalid' using errcode = '22023';
  end if;
  if p_page_path is not null
     and (
       char_length(p_page_path) not between 1 and 512
       or octet_length(p_page_path) > 2048
       or left(p_page_path, 1) <> '/'
       or p_page_path ~ '[[:cntrl:]]'
     ) then
    raise exception 'page path is invalid or too large' using errcode = '22023';
  end if;
  if p_app_state is not null
     and (
       jsonb_typeof(p_app_state) is distinct from 'object'
       or octet_length(p_app_state::text) > 65536
     ) then
    raise exception 'suggestion app state is invalid or too large' using errcode = '22023';
  end if;
  if p_viewer_snapshot is not null
     and (
       jsonb_typeof(p_viewer_snapshot) is distinct from 'object'
       or octet_length(p_viewer_snapshot::text) > 131072
     ) then
    raise exception 'viewer snapshot is invalid or too large' using errcode = '22023';
  end if;
  if p_viewer_trace_tail is not null
     and (
       jsonb_typeof(p_viewer_trace_tail) not in ('object', 'array')
       or octet_length(p_viewer_trace_tail::text) > 131072
     ) then
    raise exception 'viewer trace tail is invalid or too large' using errcode = '22023';
  end if;

  if p_quiz_session_id is not null then
    select participant_hash, display_name_hash
      into v_participant_hash, v_display_name_hash
      from public.quiz_sessions
     where id = p_quiz_session_id
       and user_id = v_user_id
       and identity_version = 1;
  else
    select participant_hash, display_name_hash, round_id
      into v_participant_hash, v_display_name_hash, v_weekly_round_id
      from public.weekly_quiz_sessions
     where session_id = p_weekly_session_id
       and user_id = v_user_id;
  end if;
  if v_participant_hash is null or v_display_name_hash is null then
    raise exception 'suggestions require an owned named quiz session'
      using errcode = '42501';
  end if;
  if p_item_id is not null
     and (
       p_app_state is null
       or p_app_state ->> 'item_id' is distinct from p_item_id
     ) then
    raise exception 'suggestion item identity must match the captured app state'
      using errcode = '22023';
  end if;
  if v_weekly_round_id is not null
     and p_item_id is not null
     and not exists (
       select 1
         from public.weekly_quiz_rounds as round
         cross join lateral jsonb_array_elements(
           round.blind_manifest -> 'items'
         ) as item(value)
        where round.round_id = v_weekly_round_id
          and item.value ->> 'id' = p_item_id
     ) then
    raise exception 'suggestion item is not part of the named weekly round'
      using errcode = '22023';
  end if;

  select * into v_suggestion
    from public.user_suggestions
   where suggestion_id = p_suggestion_id;
  if found then
    if v_suggestion.user_id <> v_user_id
       or v_suggestion.participant_hash <> v_participant_hash
       or v_suggestion.display_name_hash <> v_display_name_hash
       or v_suggestion.quiz_session_id is distinct from p_quiz_session_id
       or v_suggestion.weekly_session_id is distinct from p_weekly_session_id
       or v_suggestion.context <> p_context
       or v_suggestion.item_id is distinct from p_item_id
       or v_suggestion.page_path is distinct from p_page_path
       or v_suggestion.suggestion_text <> v_suggestion_text
       or v_suggestion.app_state is distinct from p_app_state
       or v_suggestion.viewer_snapshot is distinct from p_viewer_snapshot
       or v_suggestion.viewer_trace_tail is distinct from p_viewer_trace_tail then
      raise exception 'suggestion identity is already bound to different content'
        using errcode = '23505';
    end if;
    return v_suggestion;
  end if;

  perform pg_advisory_xact_lock(hashtextextended('suggestion:' || v_user_id::text, 0));
  if (
    select count(*)
      from public.user_suggestions
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 hour'
  ) >= 5 or (
    select count(*)
      from public.user_suggestions
     where user_id = v_user_id
       and submitted_at >= clock_timestamp() - interval '1 day'
  ) >= 20 then
    raise exception 'too many suggestions; try again later' using errcode = '42900';
  end if;

  insert into public.user_suggestions (
    suggestion_id, user_id, participant_hash, display_name_hash,
    quiz_session_id, weekly_session_id, context, item_id, page_path,
    suggestion_text, app_state, viewer_snapshot, viewer_trace_tail
  ) values (
    p_suggestion_id, v_user_id, v_participant_hash, v_display_name_hash,
    p_quiz_session_id, p_weekly_session_id, p_context, p_item_id, p_page_path,
    v_suggestion_text, p_app_state, p_viewer_snapshot, p_viewer_trace_tail
  )
  returning * into v_suggestion;
  return v_suggestion;
end;
$$;

-- Server-only replay surfaces deliberately omit auth user IDs and plaintext names.
create or replace view public.replay_quiz_sessions_safe
with (security_barrier = true, security_invoker = true)
as
select
  id as session_id,
  'classic'::text as session_kind,
  participant_hash,
  display_name_hash,
  source,
  difficulty,
  null::text as round_id,
  started_at,
  completed_at,
  identity_version = 1 as has_recorded_name
from public.quiz_sessions;

create or replace view public.replay_weekly_sessions_safe
with (security_barrier = true, security_invoker = true)
as
select
  session_id,
  'weekly'::text as session_kind,
  participant_hash,
  display_name_hash,
  null::text as source,
  null::text as difficulty,
  round_id,
  started_at,
  completed_at,
  true as has_recorded_name
from public.weekly_quiz_sessions;

create or replace view public.replay_quiz_answers_safe
with (security_barrier = true, security_invoker = true)
as
select
  answer.id,
  answer.session_id,
  answer.question_index,
  answer.item_id,
  answer.picked_none,
  answer.picked_sample,
  answer.picked_correct,
  answer.answered_at,
  answer.viewer_trace,
  answer.app_trace,
  answer.app_state,
  answer.active_pane_id
from public.quiz_answers as answer;

create or replace view public.replay_weekly_vote_attempts_safe
with (security_barrier = true, security_invoker = true)
as
select
  vote.vote_attempt_id,
  vote.session_id,
  vote.round_id,
  session.participant_hash,
  session.display_name_hash,
  vote.item_id,
  vote.question_index,
  vote.choice_id,
  vote.picked_none,
  vote.viewer_trace,
  vote.app_state,
  vote.active_pane_id,
  vote.submitted_at
from public.weekly_quiz_vote_attempts as vote
join public.weekly_quiz_sessions as session using (session_id, round_id, user_id);

create or replace view public.replay_user_suggestions_safe
with (security_barrier = true, security_invoker = true)
as
select
  suggestion_id,
  participant_hash,
  display_name_hash,
  quiz_session_id,
  weekly_session_id,
  context,
  item_id,
  page_path,
  suggestion_text,
  app_state,
  viewer_snapshot,
  viewer_trace_tail,
  submitted_at
from public.user_suggestions;

alter table public.weekly_quiz_sessions enable row level security;
alter table public.weekly_quiz_vote_attempts enable row level security;
alter table public.user_suggestions enable row level security;

revoke all on table public.weekly_quiz_sessions from public;
revoke all on table public.weekly_quiz_vote_attempts from public;
revoke all on table public.user_suggestions from public;
revoke all on table public.replay_quiz_sessions_safe from public;
revoke all on table public.replay_weekly_sessions_safe from public;
revoke all on table public.replay_quiz_answers_safe from public;
revoke all on table public.replay_weekly_vote_attempts_safe from public;
revoke all on table public.replay_user_suggestions_safe from public;

revoke all on function public.start_named_quiz_session(uuid, text, text, text)
  from public;
revoke all on function public.start_named_weekly_quiz_session(uuid, text, text, jsonb)
  from public;
revoke all on function public.complete_named_weekly_quiz_session(uuid)
  from public;
revoke all on function public.submit_weekly_quiz_vote_attempt(
  uuid, uuid, text, text, integer, text, boolean, jsonb, jsonb, text
) from public;
revoke all on function public.submit_user_suggestion(
  uuid, text, text, uuid, uuid, text, text, jsonb, jsonb, jsonb
) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on schema private from anon;
    revoke all on table private.foldarium_secrets from anon;
    revoke all on function private.foldarium_identity_hmac(text, text) from anon;
    revoke all on table public.weekly_quiz_sessions from anon;
    revoke all on table public.weekly_quiz_vote_attempts from anon;
    revoke all on table public.user_suggestions from anon;
    revoke all on table public.replay_quiz_sessions_safe,
      public.replay_weekly_sessions_safe, public.replay_quiz_answers_safe,
      public.replay_weekly_vote_attempts_safe,
      public.replay_user_suggestions_safe from anon;
    revoke all on function public.start_named_quiz_session(uuid, text, text, text)
      from anon;
    revoke all on function public.start_named_weekly_quiz_session(
      uuid, text, text, jsonb
    ) from anon;
    revoke all on function public.complete_named_weekly_quiz_session(uuid)
      from anon;
    revoke all on function public.submit_weekly_quiz_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, jsonb, jsonb, text
    ) from anon;
    revoke all on function public.submit_user_suggestion(
      uuid, text, text, uuid, uuid, text, text, jsonb, jsonb, jsonb
    ) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on schema private from authenticated;
    revoke all on table private.foldarium_secrets from authenticated;
    revoke all on function private.foldarium_identity_hmac(text, text)
      from authenticated;
    revoke all on table public.weekly_quiz_sessions from authenticated;
    revoke all on table public.weekly_quiz_vote_attempts from authenticated;
    revoke all on table public.user_suggestions from authenticated;
    revoke all on table public.replay_quiz_sessions_safe,
      public.replay_weekly_sessions_safe, public.replay_quiz_answers_safe,
      public.replay_weekly_vote_attempts_safe,
      public.replay_user_suggestions_safe from authenticated;
    grant execute on function public.start_named_quiz_session(uuid, text, text, text)
      to authenticated;
    grant execute on function public.start_named_weekly_quiz_session(uuid, text, text, jsonb)
      to authenticated;
    grant execute on function public.complete_named_weekly_quiz_session(uuid)
      to authenticated;
    grant execute on function public.submit_weekly_quiz_vote_attempt(
      uuid, uuid, text, text, integer, text, boolean, jsonb, jsonb, text
    ) to authenticated;
    grant execute on function public.submit_user_suggestion(
      uuid, text, text, uuid, uuid, text, text, jsonb, jsonb, jsonb
    ) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke all on schema private from service_role;
    revoke all on table private.foldarium_secrets from service_role;
    revoke all on function private.foldarium_identity_hmac(text, text)
      from service_role;
    revoke update on table public.weekly_quiz_vote_attempts,
      public.user_suggestions from service_role;
    grant select on table public.quiz_sessions, public.quiz_answers,
      public.weekly_quiz_sessions, public.weekly_quiz_vote_attempts,
      public.user_suggestions to service_role;
    grant insert, update, delete on table public.weekly_quiz_sessions to service_role;
    grant insert, delete on table public.weekly_quiz_vote_attempts,
      public.user_suggestions to service_role;
    grant select on table public.replay_quiz_sessions_safe,
      public.replay_weekly_sessions_safe, public.replay_quiz_answers_safe,
      public.replay_weekly_vote_attempts_safe,
      public.replay_user_suggestions_safe to service_role;
  end if;
end;
$$;

comment on table private.foldarium_secrets is
  'Database-generated private key material for non-reversible research identity HMACs.';
comment on column public.quiz_sessions.identity_version is
  '0 is a backward-compatible legacy session; 1 is a server-verified named session.';
comment on table public.weekly_quiz_sessions is
  'Named participant sessions for one blind weekly round; direct browser table access is denied.';
comment on table public.weekly_quiz_vote_attempts is
  'Append-only research events. The existing weekly_quiz_votes table remains the latest vote projection.';
comment on table public.user_suggestions is
  'User feedback plus bounded app/viewer context, linked to an owned named session.';
comment on view public.replay_quiz_sessions_safe is
  'Server-only replay index without auth user IDs or plaintext display names.';
comment on view public.replay_weekly_vote_attempts_safe is
  'Server-only weekly replay events without auth user IDs or plaintext display names.';

commit;
