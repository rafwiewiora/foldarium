-- Authorize future API archive identities from exact frozen executions and
-- verified receipts. Legacy snapshots remain byte-for-byte source/v1.
begin;

create function private.foldarium_frozen_benchmark_authorization(p_round_id text, p_execution_id uuid)
returns jsonb language sql stable security definer
set search_path = pg_catalog, public, private as $$
  select jsonb_build_object(
    'policy','foldarium.frozen-benchmark-identity/v1',
    'round_id',r.round_id,'environment',r.environment,
    'execution_id',b.execution_id::text,'provider',b.driver,'model_id',b.model_id,
    'config_sha256',b.config_sha256,'blind_manifest_sha256',r.blind_manifest_sha256,
    'execution_sha256',x.execution_sha256,'payload_digest',x.payload_digest,
    'artifact_sha256',b.artifact_sha256
  )
  from public.weekly_quiz_rounds r
  join private.weekly_automation_round_policies p on p.round_id=r.round_id
    and p.environment=r.environment and p.blind_manifest_sha256=r.blind_manifest_sha256
  join private.weekly_automation_benchmarks b on b.round_id=r.round_id
    and b.execution_id=any(p.expected_execution_ids)
  join public.weekly_selector_post_close_benchmarks_v1 x on x.execution_id=b.execution_id
    and x.round_id=r.round_id and x.environment=r.environment
    and x.provider=b.driver and x.requested_model_id=b.model_id
    and x.config_sha256=b.config_sha256 and x.display_name=b.model_id
    and x.execution_sha256=b.verified_execution_sha256
    and x.payload_digest=b.verified_payload_digest
    and x.execution->>'blind_manifest_sha256'=r.blind_manifest_sha256
    and x.payload->>'blind_manifest_sha256'=r.blind_manifest_sha256
    and x.payload->>'round_id'=r.round_id
    and x.payload->>'environment'=r.environment
    and x.payload->>'submission_id'=b.execution_id::text
  where r.round_id=p_round_id and b.execution_id=p_execution_id
    and r.environment='production' and r.status='revealed'
    and r.reveal_manifest is not null and r.revealed_at is not null
    and b.driver='anthropic-api' and b.artifact_sha256 is not null
    and b.artifact_uri is not null and b.model_id=btrim(b.model_id)
    and length(b.model_id) between 1 and 200
    and x.run_class='post_close_benchmark'
    and not exists (select 1 from public.weekly_selector_post_close_benchmarks_v1 successor
      where successor.supersedes_execution_id=x.execution_id
        and successor.round_id=x.round_id and successor.environment=x.environment)
$$;
revoke all on function private.foldarium_frozen_benchmark_authorization(text,uuid) from public;

create function public.get_weekly_retrospective_benchmark_authorizations_v1(p_round_id text)
returns jsonb language plpgsql stable security definer
set search_path = pg_catalog, public, private, auth as $$
declare result jsonb;
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  select coalesce(jsonb_agg(proof order by proof->>'execution_id'), '[]'::jsonb) into result
    from (
      select private.foldarium_frozen_benchmark_authorization(p_round_id,x.execution_id) as proof
      from public.weekly_selector_post_close_benchmarks_v1 x where x.round_id=p_round_id
    ) authorized where proof is not null;
  return result;
end;
$$;
revoke all on function public.get_weekly_retrospective_benchmark_authorizations_v1(text) from public;
grant execute on function public.get_weekly_retrospective_benchmark_authorizations_v1(text) to service_role;

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
    select benchmark.*, private.foldarium_frozen_benchmark_authorization(p_round_id, benchmark.execution_id) as benchmark_authorization
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
      benchmark.display_name as automated_identity,
      benchmark.benchmark_authorization
    from active_benchmarks benchmark
    where benchmark.run_class = 'post_close_benchmark'
      and ((benchmark.provider <> 'anthropic-api' and benchmark.display_name in ('Claude Opus', 'Codex GPT-5.6', 'GPT-5.6 Sol'))
        or benchmark.benchmark_authorization is not null)
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
      participant.current_session_count,
      null::jsonb as benchmark_authorization
    from participant_rows participant
    union all
    select
      benchmark.participant_link,
      'automated'::text as participant_kind,
      benchmark.automated_identity,
      null::text as display_name,
      0 as current_session_count,
      benchmark.benchmark_authorization
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
        where (benchmark.provider = 'anthropic-api' or benchmark.display_name not in ('Claude Opus', 'Codex GPT-5.6', 'GPT-5.6 Sol'))
          and benchmark.benchmark_authorization is null
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
    'format_version', case when exists (select 1 from benchmark_participant_rows where benchmark_authorization is not null)
      then 'foldarium.weekly-retrospective-source/v2' else 'foldarium.weekly-retrospective-source/v1' end,
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
          ) || case when participant.benchmark_authorization is not null
            then jsonb_build_object('benchmark_authorization', participant.benchmark_authorization)
            else '{}'::jsonb end
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

-- Existing publication registration already compares the full private source
-- object and its digest with this SQL-derived snapshot before catalog insertion.
-- No browser role receives the proofs, execution IDs, or private digests.
commit;
