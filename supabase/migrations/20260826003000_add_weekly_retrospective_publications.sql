-- Immutable, service-only catalog for post-reveal retrospective publications.
--
-- This is intentionally separate from weekly_quiz_evaluations: the evaluation
-- catalog binds scientific scoring, while this catalog binds a final ballot
-- snapshot to two separately sanitized archive artifacts.  The source snapshot
-- and both artifacts remain in the configured private content-addressed bucket.

begin;

create schema if not exists private;
revoke all on schema private from public;

create table public.weekly_retrospective_automated_identities (
  user_id uuid primary key references auth.users(id),
  display_name text not null check (
    display_name in ('Claude Opus', 'Codex GPT-5.6')
  ),
  participant_kind text not null check (participant_kind = 'llm'),
  created_at timestamptz not null default clock_timestamp()
);

create or replace function private.foldarium_reject_weekly_retrospective_identity_mutation()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
begin
  raise exception 'weekly retrospective automated identity rows are immutable'
    using errcode = '55000';
end;
$$;

create trigger weekly_retrospective_automated_identities_immutable
before update or delete on public.weekly_retrospective_automated_identities
for each row execute function
  private.foldarium_reject_weekly_retrospective_identity_mutation();

do $$
begin
  if exists (
    select session.user_id
      from public.weekly_quiz_sessions session
     where session.round_id = 'weekly-2026-08-08-beta-v4'
       and session.display_name in ('Claude Opus', 'Codex GPT-5.6')
     group by session.user_id
    having count(distinct session.display_name) <> 1
  ) then
    raise exception 'legacy retrospective automated identity is ambiguous'
      using errcode = '23514';
  end if;

  insert into public.weekly_retrospective_automated_identities (
    user_id,
    display_name,
    participant_kind,
    created_at
  )
  select
    session.user_id,
    session.display_name,
    'llm',
    min(session.started_at)
  from public.weekly_quiz_sessions session
  where session.round_id = 'weekly-2026-08-08-beta-v4'
    and session.display_name in ('Claude Opus', 'Codex GPT-5.6')
  group by session.user_id, session.display_name
  order by session.user_id, session.display_name;
end;
$$;

create or replace function public.register_weekly_retrospective_automated_identity(
  p_user_id uuid,
  p_display_name text,
  p_participant_kind text default 'llm'
)
returns public.weekly_retrospective_automated_identities
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_identity public.weekly_retrospective_automated_identities%rowtype;
begin
  if p_user_id is null
     or p_display_name not in ('Claude Opus', 'Codex GPT-5.6')
     or p_participant_kind <> 'llm' then
    raise exception 'retrospective automated identity is not approved'
      using errcode = '22023';
  end if;
  if not exists (select 1 from auth.users where id = p_user_id) then
    raise exception 'retrospective automated identity user does not exist'
      using errcode = '23503';
  end if;
  if exists (
    select 1
      from public.weekly_quiz_sessions session
     where session.round_id = 'weekly-2026-08-08-beta-v4'
       and session.user_id = p_user_id
       and session.display_name in ('Claude Opus', 'Codex GPT-5.6')
       and session.display_name <> p_display_name
  ) then
    raise exception 'retrospective automated identity conflicts with legacy lineage'
      using errcode = '23514';
  end if;

  insert into public.weekly_retrospective_automated_identities (
    user_id,
    display_name,
    participant_kind
  )
  values (p_user_id, p_display_name, p_participant_kind)
  on conflict (user_id) do nothing
  returning * into v_identity;

  if v_identity.user_id is null then
    select * into v_identity
      from public.weekly_retrospective_automated_identities
     where user_id = p_user_id;
    if v_identity.display_name <> p_display_name
       or v_identity.participant_kind <> p_participant_kind then
      raise exception 'retrospective automated identity registration conflicts'
        using errcode = '23514';
    end if;
  end if;
  return v_identity;
end;
$$;

create table public.weekly_retrospective_publications (
  publication_id text primary key
    check (publication_id ~ '^weekly_archive_[0-9a-f]{32}$'),
  round_id text not null references public.weekly_quiz_rounds(round_id),
  campaign_id text not null,
  environment text not null check (environment = 'production'),
  format_version text not null check (
    format_version = 'foldarium.weekly-retrospective-publication/v1'
  ),
  evaluation_id text not null
    references public.weekly_quiz_evaluations(evaluation_id),
  evaluation_format_version text not null check (
    evaluation_format_version = 'foldarium.weekly-private-evaluation/v5'
  ),
  round_opens_at timestamptz not null,
  round_closes_at timestamptz not null,
  round_revealed_at timestamptz not null,
  blind_manifest_sha256 text not null
    check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  private_index_sha256 text not null
    check (private_index_sha256 ~ '^[0-9a-f]{64}$'),
  reveal_manifest_sha256 text not null
    check (reveal_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  reference_set_sha256 text not null
    check (reference_set_sha256 ~ '^[0-9a-f]{64}$'),
  prediction_set_sha256 text not null
    check (prediction_set_sha256 ~ '^[0-9a-f]{64}$'),
  evaluation_artifact_sha256 text not null
    check (evaluation_artifact_sha256 ~ '^[0-9a-f]{64}$'),
  item_count integer not null check (item_count > 0),
  choice_count integer not null check (choice_count > 0),
  source_snapshot_object_uri text not null,
  source_snapshot_sha256 text not null
    check (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  source_snapshot_size_bytes bigint not null
    check (source_snapshot_size_bytes > 0),
  source_snapshot_media_type text not null
    check (source_snapshot_media_type = 'application/json'),
  public_artifact_object_uri text not null,
  public_artifact_sha256 text not null
    check (public_artifact_sha256 ~ '^[0-9a-f]{64}$'),
  public_artifact_size_bytes bigint not null
    check (public_artifact_size_bytes > 0),
  public_artifact_media_type text not null
    check (public_artifact_media_type = 'application/json'),
  admin_artifact_object_uri text not null,
  admin_artifact_sha256 text not null
    check (admin_artifact_sha256 ~ '^[0-9a-f]{64}$'),
  admin_artifact_size_bytes bigint not null
    check (admin_artifact_size_bytes > 0),
  admin_artifact_media_type text not null
    check (admin_artifact_media_type = 'application/json'),
  created_at timestamptz not null default clock_timestamp(),
  unique (round_id),
  unique (evaluation_id),
  check (round_closes_at > round_opens_at),
  check (round_revealed_at >= round_closes_at),
  check (public_artifact_sha256 <> admin_artifact_sha256),
  check (
    source_snapshot_object_uri ~ (
      '^supabase://[A-Za-z0-9][A-Za-z0-9._-]{0,127}/sha256/'
      || substring(source_snapshot_sha256 from 1 for 2)
      || '/' || source_snapshot_sha256 || '$'
    )
  ),
  check (
    public_artifact_object_uri ~ (
      '^supabase://[A-Za-z0-9][A-Za-z0-9._-]{0,127}/sha256/'
      || substring(public_artifact_sha256 from 1 for 2)
      || '/' || public_artifact_sha256 || '$'
    )
  ),
  check (
    admin_artifact_object_uri ~ (
      '^supabase://[A-Za-z0-9][A-Za-z0-9._-]{0,127}/sha256/'
      || substring(admin_artifact_sha256 from 1 for 2)
      || '/' || admin_artifact_sha256 || '$'
    )
  )
);

create index weekly_retrospective_publications_created_idx
  on public.weekly_retrospective_publications (round_revealed_at, created_at);

create or replace function private.foldarium_expected_weekly_retrospective_source(
  p_round_id text
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, private
as $$
  with final_votes as (
    select
      vote.user_id,
      vote.item_id,
      vote.choice_id,
      vote.picked_none
    from public.weekly_quiz_votes vote
    where vote.round_id = p_round_id
  ),
  participant_rows as (
    select
      vote_user.user_id,
      (
        select count(*)::integer
        from public.weekly_quiz_sessions session
        where session.round_id = p_round_id
          and session.user_id = vote_user.user_id
      ) as current_session_count,
      (
        select min(regexp_replace(btrim(session.display_name), '[[:space:]]+', ' ', 'g'))
        from public.weekly_quiz_sessions session
        where session.round_id = p_round_id
          and session.user_id = vote_user.user_id
      ) as current_display_name,
      (
        select count(distinct regexp_replace(
          btrim(session.display_name), '[[:space:]]+', ' ', 'g'
        ))
        from public.weekly_quiz_sessions session
        where session.round_id = p_round_id
          and session.user_id = vote_user.user_id
      ) as current_display_name_count,
      identity.display_name as automated_identity
    from (select distinct user_id from final_votes) vote_user
    left join public.weekly_retrospective_automated_identities identity
      on identity.user_id = vote_user.user_id
     and identity.participant_kind = 'llm'
  ),
  normalized_votes as (
    select
      vote.user_id,
      vote.item_id,
      vote.choice_id,
      vote.picked_none,
      case
        when vote.picked_none then 'none'
        else coalesce(
          (
            select attempt.app_state ->> 'selection_kind'
            from public.weekly_quiz_vote_attempts attempt
            where attempt.round_id = p_round_id
              and attempt.user_id = vote.user_id
              and attempt.item_id = vote.item_id
              and attempt.picked_none = vote.picked_none
              and attempt.choice_id is not distinct from vote.choice_id
              and attempt.app_state ->> 'selection_kind' in ('exact', 'cluster')
            order by attempt.submitted_at desc, attempt.vote_attempt_id desc
            limit 1
          ),
          case
            when participant.automated_identity is not null then 'cluster'
            else 'unknown'
          end
        )
      end as selection_kind
    from final_votes vote
    join participant_rows participant using (user_id)
  )
  select jsonb_build_object(
    'format_version', 'foldarium.weekly-retrospective-source/v1',
    'round_id', p_round_id,
    'participants', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'participant_link', participant.user_id::text,
            'participant_kind', case
              when participant.automated_identity is null then 'human'
              else 'automated'
            end,
            'automated_identity', participant.automated_identity,
            'display_name', case
              when participant.automated_identity is null
                then participant.current_display_name
              else null
            end,
            'current_session_count', participant.current_session_count
          )
          order by participant.user_id::text
        )
        from participant_rows participant
      ),
      '[]'::jsonb
    ),
    'votes', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'participant_link', vote.user_id::text,
            'item_id', vote.item_id,
            'choice_id', vote.choice_id,
            'picked_none', vote.picked_none,
            'selection_kind', vote.selection_kind
          )
          order by
            vote.user_id::text,
            vote.item_id,
            vote.picked_none,
            coalesce(vote.choice_id, '')
        )
        from normalized_votes vote
      ),
      '[]'::jsonb
    )
  )
  from (
    select
      coalesce(max(current_display_name_count), 0) as maximum_display_name_count,
      count(*) filter (
        where automated_identity is null and current_display_name is null
      ) as missing_human_name_count
    from participant_rows
  ) validation
  where validation.maximum_display_name_count <= 1
    and validation.missing_human_name_count = 0
$$;

create or replace function private.foldarium_validate_weekly_retrospective_catalog()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, private
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_evaluation public.weekly_quiz_evaluations%rowtype;
  v_private_index_sha256 text;
  v_choice_count integer;
begin
  if tg_op <> 'INSERT' then
    raise exception 'weekly retrospective publication rows are immutable'
      using errcode = '55000';
  end if;

  select * into v_round
    from public.weekly_quiz_rounds
   where round_id = new.round_id
   for update;
  if not found then
    raise exception 'unknown weekly round: %', new.round_id using errcode = 'P0002';
  end if;

  select * into v_evaluation
    from public.weekly_quiz_evaluations
   where evaluation_id = new.evaluation_id
     and round_id = new.round_id
   for share;
  if not found then
    raise exception 'weekly retrospective has no exact private evaluation'
      using errcode = '23514';
  end if;

  v_private_index_sha256 := v_round.metadata #>> '{private_index,sha256}';
  select count(*)::integer into v_choice_count
    from jsonb_array_elements(v_round.reveal_manifest -> 'items') item
    cross join lateral jsonb_array_elements(item.value -> 'choices') choice;

  if v_round.environment <> 'production'
     or v_round.status <> 'revealed'
     or v_round.reveal_manifest is null
     or v_round.reveal_manifest_sha256 is null
     or v_round.revealed_at is null
     or new.environment <> v_round.environment
     or new.campaign_id <> v_round.campaign_id
     or new.round_opens_at <> v_round.opens_at
     or new.round_closes_at <> v_round.closes_at
     or new.round_revealed_at <> v_round.revealed_at
     or new.blind_manifest_sha256 <> v_round.blind_manifest_sha256
     or new.private_index_sha256 <> v_private_index_sha256
     or new.reveal_manifest_sha256 <> v_round.reveal_manifest_sha256
     or new.item_count <> v_round.item_count
     or new.choice_count <> v_choice_count then
    raise exception 'weekly retrospective is not bound to one revealed production round'
      using errcode = '23514';
  end if;

  if v_evaluation.format_version <> 'foldarium.weekly-private-evaluation/v5'
     or new.evaluation_format_version <> v_evaluation.format_version
     or new.campaign_id <> v_evaluation.campaign_id
     or new.round_opens_at <> v_evaluation.round_opens_at
     or new.round_closes_at <> v_evaluation.round_closes_at
     or new.blind_manifest_sha256 <> v_evaluation.blind_manifest_sha256
     or new.private_index_sha256 <> v_evaluation.private_index_sha256
     or new.reveal_manifest_sha256 <> v_evaluation.reveal_manifest_sha256
     or new.reference_set_sha256 <> v_evaluation.reference_set_sha256
     or new.prediction_set_sha256 <> v_evaluation.prediction_set_sha256
     or new.evaluation_artifact_sha256 <> v_evaluation.artifact_sha256
     or new.item_count <> v_evaluation.item_count
     or new.choice_count <> v_evaluation.choice_count
     or split_part(new.source_snapshot_object_uri, '/', 3)
          <> split_part(v_evaluation.artifact_object_uri, '/', 3)
     or split_part(new.public_artifact_object_uri, '/', 3)
          <> split_part(v_evaluation.artifact_object_uri, '/', 3)
     or split_part(new.admin_artifact_object_uri, '/', 3)
          <> split_part(v_evaluation.artifact_object_uri, '/', 3) then
    raise exception 'weekly retrospective is not bound to the exact v5 evaluation'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger weekly_retrospective_publications_validate_insert
before insert on public.weekly_retrospective_publications
for each row execute function private.foldarium_validate_weekly_retrospective_catalog();

create trigger weekly_retrospective_publications_immutable
before update or delete on public.weekly_retrospective_publications
for each row execute function private.foldarium_validate_weekly_retrospective_catalog();

create or replace function public.register_weekly_retrospective_publication(
  p_publication jsonb,
  p_source_snapshot_canonical text
)
returns public.weekly_retrospective_publications
language plpgsql
security definer
set search_path = pg_catalog, public, private, extensions
as $$
declare
  v_expected_source jsonb;
  v_source jsonb;
  v_expected_publication_id text;
  v_publication public.weekly_retrospective_publications%rowtype;
begin
  if jsonb_typeof(p_publication) is distinct from 'object'
     or nullif(p_source_snapshot_canonical, '') is null then
    raise exception 'retrospective publication input is invalid'
      using errcode = '22023';
  end if;

  begin
    v_source := p_source_snapshot_canonical::jsonb;
  exception when others then
    raise exception 'retrospective source snapshot is not valid JSON'
      using errcode = '22023';
  end;

  -- Final voting is already closed, but these locks also serialize against a
  -- privileged maintenance write so source validation and catalog insertion
  -- cannot observe different snapshots.
  lock table public.weekly_quiz_votes in share mode;
  lock table public.weekly_quiz_vote_attempts in share mode;
  lock table public.weekly_quiz_sessions in share mode;
  lock table public.weekly_retrospective_automated_identities in share mode;

  v_expected_source := private.foldarium_expected_weekly_retrospective_source(
    p_publication ->> 'round_id'
  );
  if v_expected_source is null or v_source is distinct from v_expected_source then
    raise exception 'retrospective source snapshot differs from final votes or sessions'
      using errcode = '23514';
  end if;
  if encode(
       extensions.digest(convert_to(p_source_snapshot_canonical, 'UTF8'), 'sha256'),
       'hex'
     ) is distinct from p_publication ->> 'source_snapshot_sha256' then
    raise exception 'retrospective source snapshot digest is inconsistent'
      using errcode = '23514';
  end if;
  if octet_length(convert_to(p_source_snapshot_canonical, 'UTF8'))
       is distinct from (p_publication ->> 'source_snapshot_size_bytes')::bigint then
    raise exception 'retrospective source snapshot size is inconsistent'
      using errcode = '23514';
  end if;

  v_expected_publication_id := 'weekly_archive_' || substring(
    encode(
      extensions.digest(
        convert_to(
          '{"admin_artifact_sha256":"' || (p_publication ->> 'admin_artifact_sha256')
          || '","evaluation_artifact_sha256":"'
          || (p_publication ->> 'evaluation_artifact_sha256')
          || '","evaluation_id":"' || (p_publication ->> 'evaluation_id')
          || '","format_version":"' || (p_publication ->> 'format_version')
          || '","public_artifact_sha256":"'
          || (p_publication ->> 'public_artifact_sha256')
          || '","round_id":"' || (p_publication ->> 'round_id')
          || '","source_snapshot_sha256":"'
          || (p_publication ->> 'source_snapshot_sha256') || '}',
          'UTF8'
        ),
        'sha256'
      ),
      'hex'
    )
    from 1 for 32
  );
  if p_publication ->> 'publication_id' is distinct from v_expected_publication_id then
    raise exception 'retrospective publication_id is not deterministic'
      using errcode = '23514';
  end if;

  insert into public.weekly_retrospective_publications (
    publication_id, round_id, campaign_id, environment, format_version,
    evaluation_id, evaluation_format_version,
    round_opens_at, round_closes_at, round_revealed_at,
    blind_manifest_sha256, private_index_sha256, reveal_manifest_sha256,
    reference_set_sha256, prediction_set_sha256,
    evaluation_artifact_sha256, item_count, choice_count,
    source_snapshot_object_uri, source_snapshot_sha256,
    source_snapshot_size_bytes, source_snapshot_media_type,
    public_artifact_object_uri, public_artifact_sha256,
    public_artifact_size_bytes, public_artifact_media_type,
    admin_artifact_object_uri, admin_artifact_sha256,
    admin_artifact_size_bytes, admin_artifact_media_type
  ) values (
    p_publication ->> 'publication_id',
    p_publication ->> 'round_id',
    p_publication ->> 'campaign_id',
    p_publication ->> 'environment',
    p_publication ->> 'format_version',
    p_publication ->> 'evaluation_id',
    p_publication ->> 'evaluation_format_version',
    (p_publication ->> 'round_opens_at')::timestamptz,
    (p_publication ->> 'round_closes_at')::timestamptz,
    (p_publication ->> 'round_revealed_at')::timestamptz,
    p_publication ->> 'blind_manifest_sha256',
    p_publication ->> 'private_index_sha256',
    p_publication ->> 'reveal_manifest_sha256',
    p_publication ->> 'reference_set_sha256',
    p_publication ->> 'prediction_set_sha256',
    p_publication ->> 'evaluation_artifact_sha256',
    (p_publication ->> 'item_count')::integer,
    (p_publication ->> 'choice_count')::integer,
    p_publication ->> 'source_snapshot_object_uri',
    p_publication ->> 'source_snapshot_sha256',
    (p_publication ->> 'source_snapshot_size_bytes')::bigint,
    p_publication ->> 'source_snapshot_media_type',
    p_publication ->> 'public_artifact_object_uri',
    p_publication ->> 'public_artifact_sha256',
    (p_publication ->> 'public_artifact_size_bytes')::bigint,
    p_publication ->> 'public_artifact_media_type',
    p_publication ->> 'admin_artifact_object_uri',
    p_publication ->> 'admin_artifact_sha256',
    (p_publication ->> 'admin_artifact_size_bytes')::bigint,
    p_publication ->> 'admin_artifact_media_type'
  )
  on conflict (publication_id) do nothing;

  select * into v_publication
    from public.weekly_retrospective_publications
   where publication_id = p_publication ->> 'publication_id';
  if not found then
    raise exception 'retrospective publication identity is bound to different content'
      using errcode = '23505';
  end if;
  return v_publication;
end;
$$;

create or replace function public.list_missing_weekly_retrospective_publications()
returns table (round_id text)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select round.round_id
    from public.weekly_quiz_rounds round
    join public.weekly_quiz_evaluations evaluation
      on evaluation.round_id = round.round_id
     and evaluation.format_version = 'foldarium.weekly-private-evaluation/v5'
    left join public.weekly_retrospective_publications publication
      on publication.round_id = round.round_id
   where round.environment = 'production'
     and round.status = 'revealed'
     and round.reveal_manifest is not null
     and round.reveal_manifest_sha256 is not null
     and round.revealed_at is not null
     and publication.round_id is null
   order by round.revealed_at, round.round_id
$$;

alter table public.weekly_retrospective_publications enable row level security;
alter table public.weekly_retrospective_automated_identities enable row level security;

revoke all on table public.weekly_retrospective_publications from public;
revoke all on table public.weekly_retrospective_automated_identities from public;
revoke all on function
  public.register_weekly_retrospective_automated_identity(uuid, text, text)
  from public;
revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from public;
revoke all on function public.list_missing_weekly_retrospective_publications()
  from public;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from public;
revoke all on function private.foldarium_validate_weekly_retrospective_catalog()
  from public;
revoke all on function
  private.foldarium_reject_weekly_retrospective_identity_mutation()
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.weekly_retrospective_publications from anon;
    revoke all on table public.weekly_retrospective_automated_identities from anon;
    revoke all on function
      public.register_weekly_retrospective_automated_identity(uuid, text, text)
      from anon;
    revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
      from anon;
    revoke all on function public.list_missing_weekly_retrospective_publications()
      from anon;
    revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
      from anon;
    revoke all on function private.foldarium_validate_weekly_retrospective_catalog()
      from anon;
    revoke all on function
      private.foldarium_reject_weekly_retrospective_identity_mutation()
      from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.weekly_retrospective_publications from authenticated;
    revoke all on table public.weekly_retrospective_automated_identities
      from authenticated;
    revoke all on function
      public.register_weekly_retrospective_automated_identity(uuid, text, text)
      from authenticated;
    revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
      from authenticated;
    revoke all on function public.list_missing_weekly_retrospective_publications()
      from authenticated;
    revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
      from authenticated;
    revoke all on function private.foldarium_validate_weekly_retrospective_catalog()
      from authenticated;
    revoke all on function
      private.foldarium_reject_weekly_retrospective_identity_mutation()
      from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke all on table public.weekly_retrospective_publications from service_role;
    revoke all on table public.weekly_retrospective_automated_identities
      from service_role;
    grant select on table public.weekly_retrospective_publications to service_role;
    grant select on table public.weekly_retrospective_automated_identities
      to service_role;
    grant execute on function
      public.register_weekly_retrospective_automated_identity(uuid, text, text)
      to service_role;
    grant execute on function public.register_weekly_retrospective_publication(jsonb, text)
      to service_role;
    grant execute on function public.list_missing_weekly_retrospective_publications()
      to service_role;
  end if;
end;
$$;

comment on table public.weekly_retrospective_publications is
  'Service-role-only immutable catalog binding one revealed production round to final source, sanitized public, and private admin artifacts.';
comment on table public.weekly_retrospective_automated_identities is
  'Service-role-only append-only registry of reviewed LLM participant credentials used by retrospective publication.';
comment on function
  public.register_weekly_retrospective_automated_identity(uuid, text, text) is
  'Validates and append-only registers a reviewed LLM credential rotation for retrospective publication.';
comment on function public.register_weekly_retrospective_publication(jsonb, text) is
  'Validates the canonical source snapshot against final votes, current pseudonyms, and the automated identity registry, then atomically inserts one immutable publication.';
comment on function public.list_missing_weekly_retrospective_publications() is
  'Service-role-only backfill scan over every revealed production round with a v5 evaluation and no publication.';

notify pgrst, 'reload schema';

commit;
