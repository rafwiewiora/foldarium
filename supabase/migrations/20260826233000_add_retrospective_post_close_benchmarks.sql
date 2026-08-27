begin;

-- Extend retrospective source validation with reveal-gated post-close benchmarks.
-- Benchmark rows remain physically separate from ballots; only their unclustered
-- decisions enter the retrospective source snapshot as automated participants.

create or replace function private.foldarium_expected_weekly_retrospective_source(
  p_round_id text
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public, private
as $$
  with round_row as (
    select quiz_round.round_id, quiz_round.item_count
      from public.weekly_quiz_rounds quiz_round
     where quiz_round.round_id = p_round_id
  ),
  final_votes as (
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
            when p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'
              then 'exact'
            else null
          end
        )
      end as selection_kind
    from final_votes vote
    join participant_rows participant using (user_id)
  ),
  active_benchmarks as (
    select benchmark.*
      from public.weekly_selector_post_close_benchmarks_v1 benchmark
      join public.weekly_quiz_rounds quiz_round
        on quiz_round.environment = benchmark.environment
       and quiz_round.round_id = benchmark.round_id
     where benchmark.round_id = p_round_id
       and quiz_round.status = 'revealed'
       and quiz_round.reveal_manifest is not null
       and quiz_round.revealed_at is not null
       and not exists (
         select 1
           from public.weekly_selector_post_close_benchmarks_v1 successor
          where successor.supersedes_execution_id = benchmark.execution_id
            and successor.environment = benchmark.environment
            and successor.round_id = benchmark.round_id
       )
  ),
  benchmark_vote_rows as (
    select
      lower(benchmark.payload ->> 'submission_id') as participant_link,
      item.value ->> 'item_id' as item_id,
      case
        when item.value -> 'unclustered' ->> 'selection_kind' = 'none' then null::text
        when item.value -> 'unclustered' ->> 'selection_kind' = 'exact'
          then item.value -> 'unclustered' ->> 'choice_id'
        else null::text
      end as choice_id,
      (item.value -> 'unclustered' ->> 'selection_kind' = 'none') as picked_none,
      case
        when item.value -> 'unclustered' ->> 'selection_kind' = 'none' then 'none'
        when item.value -> 'unclustered' ->> 'selection_kind' = 'exact' then 'exact'
        else null::text
      end as selection_kind,
      benchmark.display_name
    from active_benchmarks benchmark
    cross join lateral jsonb_array_elements(benchmark.payload -> 'items') as item(value)
    where benchmark.run_class = 'post_close_benchmark'
      and (benchmark.payload ->> 'submission_id')::uuid = benchmark.execution_id
      and benchmark.payload ->> 'round_id' = p_round_id
  ),
  benchmark_participant_rows as (
    select distinct
      lower(benchmark.payload ->> 'submission_id') as participant_link,
      benchmark.display_name as automated_identity
    from active_benchmarks benchmark
    where benchmark.run_class = 'post_close_benchmark'
      and benchmark.display_name in ('Claude Opus', 'Codex GPT-5.6', 'GPT-5.6 Sol')
      and (benchmark.payload ->> 'submission_id')::uuid = benchmark.execution_id
      and benchmark.payload ->> 'round_id' = p_round_id
  ),
  combined_participants as (
    select
      participant.user_id::text as participant_link,
      case
        when participant.automated_identity is null then 'human'
        else 'automated'
      end as participant_kind,
      participant.automated_identity,
      case
        when participant.automated_identity is not null then null::text
        when participant.current_display_name is not null
          then participant.current_display_name
        when p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'
          then 'Anonymous'
        else null::text
      end as display_name,
      participant.current_session_count
    from participant_rows participant
    union all
    select
      benchmark.participant_link,
      'automated'::text as participant_kind,
      benchmark.automated_identity,
      null::text as display_name,
      0 as current_session_count
    from benchmark_participant_rows benchmark
  ),
  combined_votes as (
    select
      vote.user_id::text as participant_link,
      vote.item_id,
      vote.choice_id,
      vote.picked_none,
      vote.selection_kind
    from normalized_votes vote
    union all
    select
      benchmark.participant_link,
      benchmark.item_id,
      benchmark.choice_id,
      benchmark.picked_none,
      benchmark.selection_kind
    from benchmark_vote_rows benchmark
  ),
  benchmark_validation as (
    select
      count(*) as benchmark_count,
      count(*) filter (
        where benchmark.display_name not in ('Claude Opus', 'Codex GPT-5.6', 'GPT-5.6 Sol')
      ) as unknown_name_count,
      count(distinct benchmark.display_name) as distinct_name_count,
      count(*) filter (
        where benchmark.run_class <> 'post_close_benchmark'
           or (benchmark.payload ->> 'submission_id')::uuid <> benchmark.execution_id
           or benchmark.payload ->> 'round_id' <> p_round_id
      ) as malformed_count,
      count(*) filter (
        where jsonb_array_length(benchmark.payload -> 'items')
              is distinct from round_row.item_count
      ) as incomplete_item_count
    from active_benchmarks benchmark
    cross join round_row
  )
  select jsonb_build_object(
    'format_version', 'foldarium.weekly-retrospective-source/v1',
    'round_id', p_round_id,
    'participants', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'participant_link', participant.participant_link,
            'participant_kind', participant.participant_kind,
            'automated_identity', participant.automated_identity,
            'display_name', participant.display_name,
            'current_session_count', participant.current_session_count
          )
          order by participant.participant_link
        )
        from combined_participants participant
      ),
      '[]'::jsonb
    ),
    'votes', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'participant_link', vote.participant_link,
            'item_id', vote.item_id,
            'choice_id', vote.choice_id,
            'picked_none', vote.picked_none,
            'selection_kind', vote.selection_kind
          )
          order by
            vote.participant_link,
            vote.item_id,
            vote.picked_none,
            coalesce(vote.choice_id, '')
        )
        from combined_votes vote
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
  cross join benchmark_validation
  cross join round_row
  where validation.maximum_display_name_count <= 1
    and (
      validation.missing_human_name_count = 0
      or p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'
    )
    and not exists (
      select 1
      from normalized_votes vote
      where not vote.picked_none
        and vote.selection_kind is null
    )
    and benchmark_validation.unknown_name_count = 0
    and benchmark_validation.malformed_count = 0
    and benchmark_validation.incomplete_item_count = 0
    and benchmark_validation.distinct_name_count = benchmark_validation.benchmark_count
    and not exists (
      select 1
      from benchmark_vote_rows vote
      where vote.selection_kind is null
    )
    and not exists (
      select 1
      from benchmark_participant_rows benchmark
      join participant_rows ballot
        on ballot.user_id::text = benchmark.participant_link
    )
    and not exists (
      select 1
      from benchmark_participant_rows benchmark
      join public.weekly_retrospective_automated_identities identity
        on identity.participant_kind = 'llm'
       and identity.display_name = benchmark.automated_identity
      join participant_rows ballot
        on ballot.user_id = identity.user_id
    )
$$;

create or replace function public.get_weekly_selector_benchmarks_v1(
  p_environment text,
  p_round_id text
)
returns table (
  run_class text,
  payload jsonb,
  display_name text,
  method_name text,
  method_version text,
  provider text,
  requested_model_id text,
  observed_model_ids jsonb,
  requested_effort text,
  applied_effort text,
  effort_reporting text,
  prompt_profile_id text,
  prompt_sha256 text,
  input_manifest_sha256 text,
  tools_sha256 text,
  config_sha256 text,
  runtime_sha256 text,
  blindness_attestation jsonb,
  blindness_attestation_sha256 text,
  execution_sha256 text
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    benchmark.run_class,
    benchmark.payload,
    benchmark.display_name,
    benchmark.method_name,
    benchmark.method_version,
    benchmark.provider,
    benchmark.requested_model_id,
    benchmark.observed_model_ids,
    benchmark.requested_effort,
    benchmark.applied_effort,
    benchmark.effort_reporting,
    benchmark.prompt_profile_id,
    benchmark.prompt_sha256,
    benchmark.input_manifest_sha256,
    benchmark.tools_sha256,
    benchmark.config_sha256,
    benchmark.runtime_sha256,
    benchmark.blindness_attestation,
    benchmark.blindness_attestation_sha256,
    benchmark.execution_sha256
  from public.weekly_selector_post_close_benchmarks_v1 as benchmark
  join public.weekly_quiz_rounds as quiz_round
    on quiz_round.environment = benchmark.environment
   and quiz_round.round_id = benchmark.round_id
  where benchmark.environment = p_environment
    and benchmark.round_id = p_round_id
    and quiz_round.status = 'revealed'
    and quiz_round.reveal_manifest is not null
    and quiz_round.revealed_at is not null
    and not exists (
      select 1
        from public.weekly_selector_post_close_benchmarks_v1 as successor
       where successor.supersedes_execution_id = benchmark.execution_id
         and successor.environment = benchmark.environment
         and successor.round_id = benchmark.round_id
    )
  order by
    lower(benchmark.display_name),
    lower(benchmark.provider),
    benchmark.execution_id
$$;

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

  lock table public.weekly_quiz_votes in share mode;
  lock table public.weekly_quiz_vote_attempts in share mode;
  lock table public.weekly_quiz_sessions in share mode;
  lock table public.weekly_retrospective_automated_identities in share mode;
  lock table public.weekly_selector_post_close_benchmarks_v1 in share mode;

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
          || (p_publication ->> 'source_snapshot_sha256') || '"}',
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

revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from public;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from anon;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from authenticated;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from service_role;

revoke all on function public.get_weekly_selector_benchmarks_v1(text, text)
  from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.get_weekly_selector_benchmarks_v1(
      text, text
    ) to service_role;
  end if;
end
$$;

revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from public;
revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from anon;
revoke all on function public.register_weekly_retrospective_publication(jsonb, text)
  from authenticated;
grant execute on function public.register_weekly_retrospective_publication(jsonb, text)
  to service_role;

comment on function private.foldarium_expected_weekly_retrospective_source(text) is
  'Builds a pseudonymous retrospective source from final ballots, legacy automated identities, and active post-close benchmarks.';

commit;
