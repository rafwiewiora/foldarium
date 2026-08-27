begin;

-- The one historical replacement round copied a ballot without copying its
-- session row. Preserve that ballot under a neutral label without weakening
-- pseudonym requirements for any future round.
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
              when participant.automated_identity is not null then null
              when participant.current_display_name is not null
                then participant.current_display_name
              when p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'
                then 'Anonymous'
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
    and (
      validation.missing_human_name_count = 0
      or p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'
    )
$$;

revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from public;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from anon;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from authenticated;
revoke all on function private.foldarium_expected_weekly_retrospective_source(text)
  from service_role;

comment on function private.foldarium_expected_weekly_retrospective_source(text) is
  'Builds the immutable retrospective source and labels only the known legacy replacement ballot without a session pseudonym as Anonymous.';

commit;
