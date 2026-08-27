-- First-class provenance for manual Weekly selections.
--
-- This migration intentionally follows the named-session, vote-comment, auth.uid(),
-- and selector-API migrations. Historical attempts and app_state snapshots are not
-- rewritten: nullable provenance marks them unresolved until a reviewed resolution
-- is appended.

begin;

do $$
begin
  if to_regclass('public.weekly_quiz_vote_attempts') is null
     or to_regclass('public.weekly_quiz_votes') is null
     or to_regprocedure(
       'public.submit_weekly_quiz_vote_attempt(uuid,uuid,text,text,integer,text,boolean,text,jsonb,jsonb,text)'
     ) is null then
    raise exception
      'weekly selection provenance must run after the vote-comment and auth.uid() migrations'
      using errcode = '55000';
  end if;
end;
$$;

do $$
begin
  create type public.weekly_quiz_selection_kind as enum (
    'cluster',
    'exact',
    'none'
  );
exception
  when duplicate_object then null;
end;
$$;

alter table public.weekly_quiz_vote_attempts
  add column selection_kind public.weekly_quiz_selection_kind,
  add column selection_id text;

alter table public.weekly_quiz_vote_attempts
  add constraint weekly_quiz_vote_attempts_selection_shape
  check (
    (
      selection_kind is null
      and selection_id is null
    )
    or (
      selection_kind = 'none'
      and selection_id is null
      and picked_none
      and choice_id is null
    )
    or (
      selection_kind = 'exact'
      and selection_id = choice_id
      and not picked_none
      and nullif(choice_id, '') is not null
    )
    or (
      selection_kind = 'cluster'
      and nullif(selection_id, '') is not null
      and not picked_none
      and nullif(choice_id, '') is not null
    )
  ),
  add constraint weekly_quiz_vote_attempts_selection_id_shape
  check (
    selection_id is null
    or (
      char_length(selection_id) between 1 and 200
      and octet_length(selection_id) <= 800
      and selection_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
    )
  );

alter table public.weekly_quiz_votes
  add column selection_kind public.weekly_quiz_selection_kind,
  add column selection_id text,
  add column selection_source_attempt_id uuid,
  add column selection_revision bigint not null default 0,
  add column selection_source text,
  add column selection_source_metadata jsonb,
  add column selection_resolution_id uuid;

alter table public.weekly_quiz_votes
  add constraint weekly_quiz_votes_selection_id_shape
  check (
    selection_id is null
    or (
      char_length(selection_id) between 1 and 200
      and octet_length(selection_id) <= 800
      and selection_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
    )
  ),
  add constraint weekly_quiz_votes_selection_revision_nonnegative
  check (selection_revision >= 0),
  add constraint weekly_quiz_votes_selection_source_shape
  check (
    (
      selection_kind is null
      and selection_id is null
      and selection_source_attempt_id is null
      and selection_source is null
      and selection_source_metadata is null
      and selection_resolution_id is null
    )
    or (
      selection_kind is not null
      and selection_source_attempt_id is not null
      and selection_revision > 0
      and selection_source in ('submit_v2', 'resolution')
      and jsonb_typeof(selection_source_metadata) = 'object'
      and octet_length(selection_source_metadata::text) <= 16384
      and (
        (selection_source = 'submit_v2' and selection_resolution_id is null)
        or (selection_source = 'resolution' and selection_resolution_id is not null)
      )
    )
  ),
  add constraint weekly_quiz_votes_selection_value_shape
  check (
    selection_kind is null
    or (
      selection_kind = 'none'
      and selection_id is null
      and picked_none
      and choice_id is null
    )
    or (
      selection_kind = 'exact'
      and selection_id = choice_id
      and not picked_none
      and nullif(choice_id, '') is not null
    )
    or (
      selection_kind = 'cluster'
      and nullif(selection_id, '') is not null
      and not picked_none
      and nullif(choice_id, '') is not null
    )
  ),
  add constraint weekly_quiz_votes_selection_source_attempt_fk
  foreign key (selection_source_attempt_id)
    references public.weekly_quiz_vote_attempts(vote_attempt_id)
    on delete restrict;

create table public.weekly_quiz_vote_selection_resolutions (
  resolution_id uuid primary key,
  round_id text not null
    references public.weekly_quiz_rounds(round_id) on delete restrict,
  user_id uuid not null references auth.users(id) on delete restrict,
  item_id text not null,
  source_vote_attempt_id uuid not null
    references public.weekly_quiz_vote_attempts(vote_attempt_id) on delete restrict,
  previous_selection_revision bigint not null
    check (previous_selection_revision >= 0),
  resulting_selection_revision bigint not null
    check (resulting_selection_revision = previous_selection_revision + 1),
  selection_kind public.weekly_quiz_selection_kind not null,
  selection_id text,
  vote_fingerprint_sha256 text not null
    check (vote_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_sha256 text not null
    check (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  evidence_metadata jsonb not null,
  actor text not null,
  reviewer text not null,
  reason text not null,
  supersedes_resolution_id uuid
    references public.weekly_quiz_vote_selection_resolutions(resolution_id)
    on delete restrict,
  resolved_at timestamptz not null default clock_timestamp(),
  unique (supersedes_resolution_id),
  check (supersedes_resolution_id is null or supersedes_resolution_id <> resolution_id),
  check (
    (selection_kind = 'none' and selection_id is null)
    or (
      selection_kind in ('cluster', 'exact')
      and nullif(selection_id, '') is not null
      and char_length(selection_id) <= 200
      and octet_length(selection_id) <= 800
      and selection_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
    )
  ),
  check (
    jsonb_typeof(evidence_metadata) = 'object'
    and octet_length(evidence_metadata::text) <= 16384
  ),
  check (
    actor = btrim(actor)
    and char_length(actor) between 1 and 200
    and octet_length(actor) <= 800
    and actor !~ '[[:cntrl:]]'
  ),
  check (
    reviewer = btrim(reviewer)
    and char_length(reviewer) between 1 and 200
    and octet_length(reviewer) <= 800
    and reviewer !~ '[[:cntrl:]]'
  ),
  check (
    reason = btrim(reason)
    and char_length(reason) between 1 and 4000
    and octet_length(reason) <= 16000
    and reason !~ '[[:cntrl:]]'
  )
);

create index weekly_quiz_vote_selection_resolutions_target_idx
  on public.weekly_quiz_vote_selection_resolutions (
    round_id, user_id, item_id, resolved_at desc
  );
create index weekly_quiz_vote_selection_resolutions_attempt_idx
  on public.weekly_quiz_vote_selection_resolutions (source_vote_attempt_id);

alter table public.weekly_quiz_votes
  add constraint weekly_quiz_votes_selection_resolution_fk
  foreign key (selection_resolution_id)
    references public.weekly_quiz_vote_selection_resolutions(resolution_id)
    on delete restrict;

create or replace function private.weekly_quiz_selection_matches_manifest(
  p_blind_manifest jsonb,
  p_item_id text,
  p_choice_id text,
  p_picked_none boolean,
  p_selection_kind public.weekly_quiz_selection_kind,
  p_selection_id text
)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select
    p_selection_kind is not null
    and exists (
      select 1
        from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
       where item.value ->> 'id' = p_item_id
         and (
           (
             p_selection_kind = 'none'
             and p_picked_none
             and p_choice_id is null
             and p_selection_id is null
           )
           or (
             p_selection_kind = 'exact'
             and not p_picked_none
             and p_selection_id = p_choice_id
             and exists (
               select 1
                 from jsonb_array_elements(item.value -> 'choices') as choice(value)
                where choice.value ->> 'id' = p_choice_id
             )
           )
           or (
             p_selection_kind = 'cluster'
             and not p_picked_none
             and nullif(p_selection_id, '') is not null
             and exists (
               select 1
                 from jsonb_array_elements(item.value -> 'choices') as choice(value)
                where choice.value ->> 'id' = p_choice_id
                  and choice.value ->> 'cluster_id' = p_selection_id
             )
           )
         )
    )
$$;

create or replace function private.weekly_quiz_vote_attempt_fingerprint(
  p_vote_attempt_id uuid
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog
as $$
declare
  v_attempt public.weekly_quiz_vote_attempts%rowtype;
begin
  select *
    into v_attempt
    from public.weekly_quiz_vote_attempts
   where vote_attempt_id = p_vote_attempt_id;
  if not found then
    raise exception 'unknown weekly vote attempt' using errcode = 'P0002';
  end if;

  return encode(
    extensions.digest(
      convert_to(
        jsonb_build_object(
          'vote_attempt_id', v_attempt.vote_attempt_id,
          'session_id', v_attempt.session_id,
          'round_id', v_attempt.round_id,
          'user_id', v_attempt.user_id,
          'item_id', v_attempt.item_id,
          'question_index', v_attempt.question_index,
          'choice_id', v_attempt.choice_id,
          'picked_none', v_attempt.picked_none,
          'selection_kind', v_attempt.selection_kind,
          'selection_id', v_attempt.selection_id,
          'viewer_trace', v_attempt.viewer_trace,
          'app_state', v_attempt.app_state,
          'active_pane_id', v_attempt.active_pane_id,
          'vote_comment', v_attempt.vote_comment,
          'submitted_at', v_attempt.submitted_at,
          'created_at', v_attempt.created_at
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  );
end;
$$;

create or replace function private.reject_weekly_selection_resolution_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'weekly selection resolutions are append-only'
    using errcode = '55000';
end;
$$;

create trigger weekly_selection_resolutions_append_only
before update or delete on public.weekly_quiz_vote_selection_resolutions
for each row execute function private.reject_weekly_selection_resolution_mutation();

-- Once a round has been opened, the manifest against which v2 selections are
-- checked must not be replaced while preserving its public identity.
create or replace function private.protect_weekly_quiz_blind_manifest()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if old.opened_at is not null
     and (
       new.blind_manifest is distinct from old.blind_manifest
       or new.blind_manifest_sha256 is distinct from old.blind_manifest_sha256
       or new.item_count is distinct from old.item_count
     ) then
    raise exception 'an opened weekly blind manifest is immutable'
      using errcode = '55000';
  end if;
  return new;
end;
$$;

create trigger protect_weekly_quiz_blind_manifest
before update of blind_manifest, blind_manifest_sha256, item_count
on public.weekly_quiz_rounds
for each row execute function private.protect_weekly_quiz_blind_manifest();

create or replace function private.submit_weekly_quiz_vote_attempt_core(
  p_vote_attempt_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_choice_id text,
  p_picked_none boolean,
  p_selection_kind public.weekly_quiz_selection_kind,
  p_selection_id text,
  p_provenance_validated boolean,
  p_vote_comment text,
  p_viewer_trace jsonb,
  p_app_state jsonb,
  p_active_pane_id text
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
  v_selection_metadata jsonb;
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
  if not p_provenance_validated
     and (p_selection_kind is not null or p_selection_id is not null) then
    raise exception 'legacy weekly vote RPC cannot assert selection provenance'
      using errcode = '22023';
  end if;
  if p_provenance_validated and p_selection_kind is null then
    raise exception 'v2 weekly votes require cluster, exact, or none provenance'
      using errcode = '22023';
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
       or v_attempt.selection_kind is distinct from p_selection_kind
       or v_attempt.selection_id is distinct from p_selection_id
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
    raise exception 'vote does not reference a published item/choice'
      using errcode = '22023';
  end if;
  if p_provenance_validated
     and not private.weekly_quiz_selection_matches_manifest(
       v_round.blind_manifest,
       p_item_id,
       p_choice_id,
       p_picked_none,
       p_selection_kind,
       p_selection_id
     ) then
    raise exception 'selection provenance does not reference the immutable blind manifest'
      using errcode = '22023';
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
    choice_id, picked_none, selection_kind, selection_id,
    viewer_trace, app_state, active_pane_id, vote_comment
  ) values (
    p_vote_attempt_id, p_session_id, p_round_id, v_user_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, p_selection_kind, p_selection_id,
    p_viewer_trace, p_app_state, p_active_pane_id, p_vote_comment
  ) returning * into v_attempt;

  if p_provenance_validated then
    v_selection_metadata := jsonb_build_object(
      'api_version', 2,
      'blind_manifest_sha256', v_round.blind_manifest_sha256
    );
  end if;

  insert into public.weekly_quiz_votes (
    vote_id, round_id, user_id, item_id, choice_id, picked_none, submitted_at,
    selection_kind, selection_id, selection_source_attempt_id,
    selection_revision, selection_source, selection_source_metadata,
    selection_resolution_id
  ) values (
    p_vote_attempt_id, p_round_id, v_user_id, p_item_id,
    p_choice_id, p_picked_none, v_attempt.submitted_at,
    case when p_provenance_validated then p_selection_kind else null end,
    case when p_provenance_validated then p_selection_id else null end,
    case when p_provenance_validated then p_vote_attempt_id else null end,
    1,
    case when p_provenance_validated then 'submit_v2' else null end,
    v_selection_metadata,
    null
  ) on conflict (round_id, user_id, item_id) do update
       set choice_id = excluded.choice_id,
           picked_none = excluded.picked_none,
           submitted_at = excluded.submitted_at,
           selection_kind = excluded.selection_kind,
           selection_id = excluded.selection_id,
           selection_source_attempt_id = excluded.selection_source_attempt_id,
           selection_revision = public.weekly_quiz_votes.selection_revision + 1,
           selection_source = excluded.selection_source,
           selection_source_metadata = excluded.selection_source_metadata,
           selection_resolution_id = null;
  return v_attempt;
end;
$$;

-- Existing callers remain supported, but they cannot carry trusted provenance.
-- Every legacy projection write explicitly clears prior selection provenance.
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
begin
  return private.submit_weekly_quiz_vote_attempt_core(
    p_vote_attempt_id, p_session_id, p_round_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none,
    null::public.weekly_quiz_selection_kind, null, false,
    p_vote_comment, p_viewer_trace, p_app_state, p_active_pane_id
  );
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
begin
  return private.submit_weekly_quiz_vote_attempt_core(
    p_vote_attempt_id, p_session_id, p_round_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none,
    null::public.weekly_quiz_selection_kind, null, false,
    null, p_viewer_trace, p_app_state, p_active_pane_id
  );
end;
$$;

create or replace function public.submit_weekly_quiz_vote_attempt_v2(
  p_vote_attempt_id uuid,
  p_session_id uuid,
  p_round_id text,
  p_item_id text,
  p_question_index integer,
  p_choice_id text,
  p_picked_none boolean,
  p_selection_kind public.weekly_quiz_selection_kind,
  p_selection_id text,
  p_vote_comment text default null,
  p_viewer_trace jsonb default null,
  p_app_state jsonb default null,
  p_active_pane_id text default null
)
returns public.weekly_quiz_vote_attempts
language plpgsql
security definer
set search_path = pg_catalog
as $$
begin
  if p_selection_kind is null then
    raise exception 'v2 weekly votes require cluster, exact, or none provenance'
      using errcode = '22023';
  end if;
  return private.submit_weekly_quiz_vote_attempt_core(
    p_vote_attempt_id, p_session_id, p_round_id, p_item_id, p_question_index,
    p_choice_id, p_picked_none, p_selection_kind, p_selection_id, true,
    p_vote_comment, p_viewer_trace, p_app_state, p_active_pane_id
  );
end;
$$;

create or replace function public.submit_weekly_quiz_vote(
  p_vote_id uuid,
  p_round_id text,
  p_item_id text,
  p_choice_id text,
  p_picked_none boolean
)
returns public.weekly_quiz_votes
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_user_id uuid;
  v_round public.weekly_quiz_rounds%rowtype;
  v_vote public.weekly_quiz_votes%rowtype;
begin
  v_user_id := auth.uid();
  if v_user_id is null then
    raise exception 'authentication required' using errcode = '42501';
  end if;
  select * into v_round from public.weekly_quiz_rounds
   where round_id = p_round_id for share;
  if not found
     or v_round.status <> 'open'
     or clock_timestamp() < v_round.opens_at
     or clock_timestamp() >= v_round.closes_at then
    raise exception 'weekly round is not accepting votes' using errcode = '23514';
  end if;
  if nullif(p_item_id, '') is null
     or not exists (
       select 1 from jsonb_array_elements(v_round.blind_manifest -> 'items') as item(value)
        where item.value ->> 'id' = p_item_id
          and (
            (p_picked_none and p_choice_id is null)
            or (
              not p_picked_none
              and exists (
                select 1 from jsonb_array_elements(item.value -> 'choices') as choice(value)
                 where choice.value ->> 'id' = p_choice_id
              )
            )
          )
     ) then
    raise exception 'vote does not reference a published item/choice'
      using errcode = '22023';
  end if;

  insert into public.weekly_quiz_votes (
    vote_id, round_id, user_id, item_id, choice_id, picked_none, submitted_at,
    selection_kind, selection_id, selection_source_attempt_id,
    selection_revision, selection_source, selection_source_metadata,
    selection_resolution_id
  ) values (
    p_vote_id, p_round_id, v_user_id, p_item_id, p_choice_id, p_picked_none,
    clock_timestamp(), null, null, null, 1, null, null, null
  )
  on conflict (round_id, user_id, item_id) do update
     set choice_id = excluded.choice_id,
         picked_none = excluded.picked_none,
         submitted_at = excluded.submitted_at,
         selection_kind = null,
         selection_id = null,
         selection_source_attempt_id = null,
         selection_revision = public.weekly_quiz_votes.selection_revision + 1,
         selection_source = null,
         selection_source_metadata = null,
         selection_resolution_id = null
  returning * into v_vote;
  return v_vote;
end;
$$;

create or replace function public.resolve_weekly_quiz_vote_selection(
  p_resolution_id uuid,
  p_source_vote_attempt_id uuid,
  p_selection_kind public.weekly_quiz_selection_kind,
  p_selection_id text,
  p_evidence_sha256 text,
  p_evidence_metadata jsonb,
  p_actor text,
  p_reviewer text,
  p_reason text,
  p_expected_selection_revision bigint,
  p_expected_vote_fingerprint_sha256 text,
  p_supersedes_resolution_id uuid default null
)
returns public.weekly_quiz_vote_selection_resolutions
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_attempt public.weekly_quiz_vote_attempts%rowtype;
  v_round public.weekly_quiz_rounds%rowtype;
  v_vote public.weekly_quiz_votes%rowtype;
  v_existing public.weekly_quiz_vote_selection_resolutions%rowtype;
  v_resolution public.weekly_quiz_vote_selection_resolutions%rowtype;
  v_vote_fingerprint text;
begin
  if p_resolution_id is null or p_source_vote_attempt_id is null
     or p_selection_kind is null
     or p_evidence_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_vote_fingerprint_sha256 !~ '^[0-9a-f]{64}$'
     or p_expected_selection_revision is null
     or p_expected_selection_revision < 0
     or jsonb_typeof(p_evidence_metadata) is distinct from 'object'
     or octet_length(p_evidence_metadata::text) > 16384
     or nullif(btrim(p_actor), '') is null
     or nullif(btrim(p_reviewer), '') is null
     or nullif(btrim(p_reason), '') is null then
    raise exception 'invalid weekly selection resolution request'
      using errcode = '22023';
  end if;

  select * into v_existing
    from public.weekly_quiz_vote_selection_resolutions
   where resolution_id = p_resolution_id;
  if found then
    if v_existing.source_vote_attempt_id = p_source_vote_attempt_id
       and v_existing.selection_kind = p_selection_kind
       and v_existing.selection_id is not distinct from p_selection_id
       and v_existing.evidence_sha256 = p_evidence_sha256
       and v_existing.evidence_metadata = p_evidence_metadata
       and v_existing.actor = btrim(p_actor)
       and v_existing.reviewer = btrim(p_reviewer)
       and v_existing.reason = btrim(p_reason)
       and v_existing.previous_selection_revision = p_expected_selection_revision
       and v_existing.vote_fingerprint_sha256 = p_expected_vote_fingerprint_sha256
       and v_existing.supersedes_resolution_id is not distinct from p_supersedes_resolution_id then
      return v_existing;
    end if;
    raise exception 'resolution id is already bound to different evidence'
      using errcode = '23505';
  end if;

  select * into v_attempt
    from public.weekly_quiz_vote_attempts
   where vote_attempt_id = p_source_vote_attempt_id;
  if not found then
    raise exception 'unknown weekly vote attempt' using errcode = 'P0002';
  end if;
  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = v_attempt.round_id
   for share;
  if not found then
    raise exception 'unknown weekly round' using errcode = 'P0002';
  end if;
  if not private.weekly_quiz_selection_matches_manifest(
    v_round.blind_manifest,
    v_attempt.item_id,
    v_attempt.choice_id,
    v_attempt.picked_none,
    p_selection_kind,
    p_selection_id
  ) then
    raise exception 'resolved selection does not reference the immutable blind manifest'
      using errcode = '22023';
  end if;

  v_vote_fingerprint :=
    private.weekly_quiz_vote_attempt_fingerprint(p_source_vote_attempt_id);
  if v_vote_fingerprint is distinct from p_expected_vote_fingerprint_sha256 then
    raise exception 'weekly vote fingerprint changed or was not expected'
      using errcode = '40001';
  end if;

  select * into v_vote
    from public.weekly_quiz_votes
   where round_id = v_attempt.round_id
     and user_id = v_attempt.user_id
     and item_id = v_attempt.item_id
   for update;
  if not found then
    raise exception 'weekly vote projection is missing' using errcode = 'P0002';
  end if;
  if v_vote.selection_revision <> p_expected_selection_revision
     or v_vote.choice_id is distinct from v_attempt.choice_id
     or v_vote.picked_none <> v_attempt.picked_none
     or v_vote.submitted_at <> v_attempt.submitted_at
     or v_vote.selection_resolution_id is distinct from p_supersedes_resolution_id then
    raise exception 'weekly vote projection changed; refresh the resolution plan'
      using errcode = '40001';
  end if;
  if p_supersedes_resolution_id is not null
     and not exists (
       select 1
         from public.weekly_quiz_vote_selection_resolutions as prior
        where prior.resolution_id = p_supersedes_resolution_id
          and prior.round_id = v_attempt.round_id
          and prior.user_id = v_attempt.user_id
          and prior.item_id = v_attempt.item_id
     ) then
    raise exception 'superseded resolution does not belong to this vote'
      using errcode = '22023';
  end if;

  insert into public.weekly_quiz_vote_selection_resolutions (
    resolution_id, round_id, user_id, item_id, source_vote_attempt_id,
    previous_selection_revision, resulting_selection_revision,
    selection_kind, selection_id, vote_fingerprint_sha256,
    evidence_sha256, evidence_metadata, actor, reviewer, reason,
    supersedes_resolution_id
  ) values (
    p_resolution_id, v_attempt.round_id, v_attempt.user_id, v_attempt.item_id,
    p_source_vote_attempt_id,
    p_expected_selection_revision, p_expected_selection_revision + 1,
    p_selection_kind, p_selection_id, v_vote_fingerprint,
    p_evidence_sha256, p_evidence_metadata,
    btrim(p_actor), btrim(p_reviewer), btrim(p_reason),
    p_supersedes_resolution_id
  )
  returning * into v_resolution;

  update public.weekly_quiz_votes
     set selection_kind = p_selection_kind,
         selection_id = p_selection_id,
         selection_source_attempt_id = p_source_vote_attempt_id,
         selection_revision = p_expected_selection_revision + 1,
         selection_source = 'resolution',
         selection_source_metadata = jsonb_build_object(
           'blind_manifest_sha256', v_round.blind_manifest_sha256,
           'evidence_sha256', p_evidence_sha256,
           'resolution_id', p_resolution_id
         ),
         selection_resolution_id = p_resolution_id
   where round_id = v_attempt.round_id
     and user_id = v_attempt.user_id
     and item_id = v_attempt.item_id
     and selection_revision = p_expected_selection_revision;
  if not found then
    raise exception 'weekly vote projection changed during resolution'
      using errcode = '40001';
  end if;

  return v_resolution;
end;
$$;

-- Read-only dry run for a prospective retrospective switch. "ready" is true
-- only when the target round has zero unresolved and zero inconsistent rows.
create or replace function public.check_weekly_quiz_selection_provenance(
  p_round_id text
)
returns table (
  round_id text,
  total_votes bigint,
  resolved_votes bigint,
  unresolved_votes bigint,
  inconsistent_votes bigint,
  ready boolean
)
language plpgsql
stable
security definer
set search_path = pg_catalog
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
begin
  select * into v_round
    from public.weekly_quiz_rounds
   where weekly_quiz_rounds.round_id = p_round_id;
  if not found then
    raise exception 'unknown weekly round' using errcode = 'P0002';
  end if;

  return query
  with checked as (
    select
      vote.selection_kind is null as unresolved,
      (
        vote.selection_kind is not null
        and (
          attempt.vote_attempt_id is null
          or attempt.round_id <> vote.round_id
          or attempt.user_id <> vote.user_id
          or attempt.item_id <> vote.item_id
          or attempt.choice_id is distinct from vote.choice_id
          or attempt.picked_none <> vote.picked_none
          or attempt.submitted_at <> vote.submitted_at
          or vote.selection_source_metadata ->> 'blind_manifest_sha256'
               is distinct from v_round.blind_manifest_sha256
          or not private.weekly_quiz_selection_matches_manifest(
            v_round.blind_manifest,
            vote.item_id,
            vote.choice_id,
            vote.picked_none,
            vote.selection_kind,
            vote.selection_id
          )
          or (
            vote.selection_source = 'submit_v2'
            and (
              attempt.selection_kind is distinct from vote.selection_kind
              or attempt.selection_id is distinct from vote.selection_id
              or vote.selection_resolution_id is not null
            )
          )
          or (
            vote.selection_source = 'resolution'
            and (
              resolution.resolution_id is null
              or resolution.round_id <> vote.round_id
              or resolution.user_id <> vote.user_id
              or resolution.item_id <> vote.item_id
              or resolution.source_vote_attempt_id <> attempt.vote_attempt_id
              or resolution.selection_kind <> vote.selection_kind
              or resolution.selection_id is distinct from vote.selection_id
              or resolution.resulting_selection_revision <> vote.selection_revision
              or resolution.vote_fingerprint_sha256
                   <> case
                        when attempt.vote_attempt_id is null then null
                        else private.weekly_quiz_vote_attempt_fingerprint(
                          attempt.vote_attempt_id
                        )
                      end
              or vote.selection_source_metadata ->> 'evidence_sha256'
                   is distinct from resolution.evidence_sha256
            )
          )
        )
      ) as inconsistent
    from public.weekly_quiz_votes as vote
    left join public.weekly_quiz_vote_attempts as attempt
      on attempt.vote_attempt_id = vote.selection_source_attempt_id
    left join public.weekly_quiz_vote_selection_resolutions as resolution
      on resolution.resolution_id = vote.selection_resolution_id
    where vote.round_id = p_round_id
  ),
  counts as (
    select
      count(*)::bigint as total_votes,
      count(*) filter (where not unresolved and not inconsistent)::bigint
        as resolved_votes,
      count(*) filter (where unresolved)::bigint as unresolved_votes,
      count(*) filter (where inconsistent)::bigint as inconsistent_votes
    from checked
  )
  select
    p_round_id,
    counts.total_votes,
    counts.resolved_votes,
    counts.unresolved_votes,
    counts.inconsistent_votes,
    counts.unresolved_votes = 0 and counts.inconsistent_votes = 0
  from counts;
end;
$$;

create or replace view public.replay_weekly_vote_attempts_safe
with (security_barrier = true, security_invoker = true)
as
select
  vote.vote_attempt_id, vote.session_id, vote.round_id,
  session.participant_hash, session.display_name_hash,
  vote.item_id, vote.question_index, vote.choice_id, vote.picked_none,
  vote.viewer_trace, vote.app_state, vote.active_pane_id, vote.vote_comment,
  vote.submitted_at,
  vote.selection_kind, vote.selection_id
from public.weekly_quiz_vote_attempts as vote
join public.weekly_quiz_sessions as session using (session_id, round_id, user_id);

alter table public.weekly_quiz_vote_attempts enable row level security;
alter table public.weekly_quiz_votes enable row level security;
alter table public.weekly_quiz_vote_selection_resolutions enable row level security;

revoke all on table public.weekly_quiz_vote_selection_resolutions from public;
revoke all on function private.weekly_quiz_selection_matches_manifest(
  jsonb, text, text, boolean, public.weekly_quiz_selection_kind, text
) from public;
revoke all on function private.weekly_quiz_vote_attempt_fingerprint(uuid) from public;
revoke all on function private.reject_weekly_selection_resolution_mutation() from public;
revoke all on function private.protect_weekly_quiz_blind_manifest() from public;
revoke all on function private.submit_weekly_quiz_vote_attempt_core(
  uuid, uuid, text, text, integer, text, boolean,
  public.weekly_quiz_selection_kind, text, boolean, text, jsonb, jsonb, text
) from public;
revoke all on function public.submit_weekly_quiz_vote_attempt_v2(
  uuid, uuid, text, text, integer, text, boolean,
  public.weekly_quiz_selection_kind, text, text, jsonb, jsonb, text
) from public;
revoke all on function public.resolve_weekly_quiz_vote_selection(
  uuid, uuid, public.weekly_quiz_selection_kind, text, text, jsonb,
  text, text, text, bigint, text, uuid
) from public;
revoke all on function public.check_weekly_quiz_selection_provenance(text) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_quiz_vote_selection_resolutions from anon;
    revoke all on function public.submit_weekly_quiz_vote_attempt_v2(
      uuid, uuid, text, text, integer, text, boolean,
      public.weekly_quiz_selection_kind, text, text, jsonb, jsonb, text
    ) from anon;
    revoke all on function public.resolve_weekly_quiz_vote_selection(
      uuid, uuid, public.weekly_quiz_selection_kind, text, text, jsonb,
      text, text, text, bigint, text, uuid
    ) from anon;
    revoke all on function public.check_weekly_quiz_selection_provenance(text) from anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_quiz_vote_selection_resolutions from authenticated;
    revoke all on function public.resolve_weekly_quiz_vote_selection(
      uuid, uuid, public.weekly_quiz_selection_kind, text, text, jsonb,
      text, text, text, bigint, text, uuid
    ) from authenticated;
    revoke all on function public.check_weekly_quiz_selection_provenance(text)
      from authenticated;
    grant execute on function public.submit_weekly_quiz_vote_attempt_v2(
      uuid, uuid, text, text, integer, text, boolean,
      public.weekly_quiz_selection_kind, text, text, jsonb, jsonb, text
    ) to authenticated;
  end if;

  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke insert, update, delete, truncate
      on table public.weekly_quiz_vote_selection_resolutions from service_role;
    grant select on table public.weekly_quiz_vote_selection_resolutions to service_role;
    revoke update, delete, truncate
      on table public.weekly_quiz_vote_attempts from service_role;
    revoke insert, update, delete, truncate
      on table public.weekly_quiz_votes from service_role;
    grant execute on function public.resolve_weekly_quiz_vote_selection(
      uuid, uuid, public.weekly_quiz_selection_kind, text, text, jsonb,
      text, text, text, bigint, text, uuid
    ) to service_role;
    grant execute on function public.check_weekly_quiz_selection_provenance(text)
      to service_role;
  end if;
end;
$$;

comment on type public.weekly_quiz_selection_kind is
  'Validated manual Weekly decision mode: a displayed cluster, one exact choice, or none.';
comment on column public.weekly_quiz_vote_attempts.selection_kind is
  'Nullable for historical and legacy-RPC attempts; v2 writes validate this against the opened blind manifest.';
comment on column public.weekly_quiz_votes.selection_source_attempt_id is
  'Append-only attempt from which the latest projection and its immutable fingerprint are derived.';
comment on table public.weekly_quiz_vote_selection_resolutions is
  'Service-readable, RPC-appended manual provenance resolutions; supersession appends rather than mutates.';
comment on function public.submit_weekly_quiz_vote_attempt_v2(
  uuid, uuid, text, text, integer, text, boolean,
  public.weekly_quiz_selection_kind, text, text, jsonb, jsonb, text
) is 'Submits one append-only vote attempt with cluster/exact/none provenance validated against the immutable blind manifest.';
comment on function public.check_weekly_quiz_selection_provenance(text) is
  'Read-only completeness and consistency dry run for a retrospective provenance switch.';

notify pgrst, 'reload schema';

commit;
