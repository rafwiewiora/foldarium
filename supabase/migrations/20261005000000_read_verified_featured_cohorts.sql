-- Read the immutable assignment authority only inside the server's released archive API.
begin;
create function public.get_weekly_featured_cohort_source_v1(p_round_id text)
returns jsonb language sql stable security definer set search_path = pg_catalog as $$
  select jsonb_build_object(
    'round_id', selection.round_id,
    'environment', round.environment,
    'blind_manifest_sha256', selection.blind_manifest_sha256,
    'featured_questions', selection.featured_questions,
    'selection_artifact', selection.selection_artifact,
    'registered_at', selection.registered_at
  )
  from private.weekly_featured_question_selections selection
  join public.weekly_quiz_rounds round using (round_id)
  where round.round_id = p_round_id and round.environment = 'production'
    and round.status = 'revealed' and round.revealed_at is not null
    and round.reveal_manifest is not null
    and selection.blind_manifest_sha256 = round.blind_manifest_sha256;
$$;
revoke all on function public.get_weekly_featured_cohort_source_v1(text) from public;
do $$ begin
  if exists(select 1 from pg_roles where rolname='anon') then
    revoke all on function public.get_weekly_featured_cohort_source_v1(text) from anon;
  end if;
  if exists(select 1 from pg_roles where rolname='authenticated') then
    revoke all on function public.get_weekly_featured_cohort_source_v1(text) from authenticated;
  end if;
  if exists(select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.get_weekly_featured_cohort_source_v1(text) to service_role;
  end if;
end $$;
notify pgrst, 'reload schema';
commit;
