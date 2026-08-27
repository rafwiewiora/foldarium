-- Partition weekly rounds by deployment environment and permit reviewed
-- pose-only provenance in blind manifests.  Existing callers remain pinned to
-- production; Preview callers must opt into the preview environment explicitly.

begin;

alter table public.weekly_quiz_rounds
  add column if not exists environment text;

update public.weekly_quiz_rounds
   set environment = 'production'
 where environment is null;

alter table public.weekly_quiz_rounds
  alter column environment set default 'production',
  alter column environment set not null;

alter table public.weekly_quiz_rounds
  drop constraint if exists weekly_quiz_rounds_environment_check;
alter table public.weekly_quiz_rounds
  add constraint weekly_quiz_rounds_environment_check
  check (environment in ('production', 'preview', 'development'));

create index if not exists weekly_quiz_rounds_environment_window_idx
  on public.weekly_quiz_rounds (environment, opens_at desc, closes_at desc);

create or replace function public.open_weekly_quiz_round(
  p_round_id text,
  p_campaign_id text,
  p_opens_at timestamptz,
  p_closes_at timestamptz,
  p_blind_manifest jsonb,
  p_blind_manifest_sha256 text,
  p_metadata jsonb,
  p_environment text
)
returns public.weekly_quiz_rounds
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_item_count integer;
begin
  if nullif(p_round_id, '') is null
     or nullif(p_campaign_id, '') is null
     or p_environment is null
     or p_environment not in ('production', 'preview', 'development')
     or p_closes_at <= p_opens_at
     or jsonb_typeof(p_blind_manifest) is distinct from 'object'
     or (p_blind_manifest -> 'schema_version') is distinct from '1'::jsonb
     or p_blind_manifest ->> 'round_id' is distinct from p_round_id
     or jsonb_typeof(p_blind_manifest -> 'items') is distinct from 'array'
     or jsonb_array_length(p_blind_manifest -> 'items') = 0
     or p_blind_manifest_sha256 !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(coalesce(p_metadata, '{}'::jsonb)) is distinct from 'object' then
    raise exception 'invalid weekly blind manifest or voting window'
      using errcode = '22023';
  end if;

  if exists (
    select 1
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     where jsonb_typeof(value) is distinct from 'object'
        or nullif(value ->> 'id', '') is null
        or value ?| array['correct', 'rmsd', 'answer', 'answer_metadata', 'score', 'reference']
        or jsonb_typeof(value -> 'choices') is distinct from 'array'
        or jsonb_array_length(value -> 'choices') = 0
        or exists (
          select 1
            from jsonb_array_elements(value -> 'choices') as choice(value)
           where jsonb_typeof(choice.value) is distinct from 'object'
              or nullif(choice.value ->> 'id', '') is null
              or choice.value ?| array[
                'correct', 'rmsd', 'answer', 'answer_metadata', 'score',
                'run_id', 'sample_id', 'reference'
              ]
              or (
                choice.value ? 'method'
                and (
                  jsonb_typeof(choice.value -> 'method') is distinct from 'string'
                  or char_length(btrim(choice.value ->> 'method')) not between 1 and 50
                )
              )
              or (
                choice.value ? 'method_version'
                and (
                  not choice.value ? 'method'
                  or jsonb_typeof(choice.value -> 'method_version') is distinct from 'string'
                  or char_length(btrim(choice.value ->> 'method_version')) not between 1 and 100
                )
              )
              or (
                choice.value ? 'confidence'
                and (
                  not choice.value ? 'method'
                  or jsonb_typeof(choice.value -> 'confidence') is distinct from 'object'
                  or choice.value #>> '{confidence,metric}' <> 'ligand_plddt'
                  or jsonb_typeof(choice.value #> '{confidence,value}') is distinct from 'number'
                  or jsonb_typeof(choice.value #> '{confidence,scale_min}') is distinct from 'number'
                  or jsonb_typeof(choice.value #> '{confidence,scale_max}') is distinct from 'number'
                  or (choice.value #>> '{confidence,scale_min}')::numeric <> 0
                  or (choice.value #>> '{confidence,scale_max}')::numeric <> 100
                  or (choice.value #>> '{confidence,value}')::numeric not between 0 and 100
                  or choice.value #>> '{confidence,aggregation}'
                     <> 'arithmetic-mean-selected-ligand-heavy-atoms'
                )
              )
              or (
                choice.value ? 'smina_score'
                and (
                  not choice.value ? 'method'
                  or jsonb_typeof(choice.value -> 'smina_score') is distinct from 'object'
                  or choice.value #>> '{smina_score,metric}' <> 'smina_affinity'
                  or jsonb_typeof(choice.value #> '{smina_score,value}') is distinct from 'number'
                  or choice.value #>> '{smina_score,units}' <> 'kcal/mol'
                  or choice.value #>> '{smina_score,protocol}' <> 'score_only'
                  or nullif(choice.value #>> '{smina_score,scoring_function}', '') is null
                )
              )
              or (
                choice.value ? 'interaction_count'
                and (
                  not choice.value ? 'method'
                  or jsonb_typeof(choice.value -> 'interaction_count') is distinct from 'object'
                  or choice.value #>> '{interaction_count,metric}'
                     <> 'prolif_unique_residue_interaction_type'
                  or jsonb_typeof(choice.value #> '{interaction_count,value}') is distinct from 'number'
                  or choice.value #>> '{interaction_count,value}' !~ '^[0-9]+$'
                  or nullif(choice.value #>> '{interaction_count,policy}', '') is null
                )
              )
        )
  ) then
    raise exception 'blind manifest contains invalid or reveal-only fields'
      using errcode = '22023';
  end if;

  if exists (
    select 1 from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
     group by value ->> 'id' having count(*) > 1
  ) or exists (
    select 1
      from jsonb_array_elements(p_blind_manifest -> 'items') as item(value)
      cross join lateral jsonb_array_elements(item.value -> 'choices') as choice(value)
     group by item.value ->> 'id', choice.value ->> 'id' having count(*) > 1
  ) then
    raise exception 'weekly round item and choice IDs must be unique'
      using errcode = '23505';
  end if;

  v_item_count := jsonb_array_length(p_blind_manifest -> 'items');
  insert into public.weekly_quiz_rounds (
    round_id, campaign_id, status, opens_at, closes_at,
    blind_manifest, blind_manifest_sha256, item_count, opened_at, metadata,
    environment
  ) values (
    p_round_id, p_campaign_id, 'open', p_opens_at, p_closes_at,
    p_blind_manifest, p_blind_manifest_sha256, v_item_count,
    clock_timestamp(), coalesce(p_metadata, '{}'::jsonb), p_environment
  )
  on conflict (round_id) do nothing;

  select * into v_round from public.weekly_quiz_rounds where round_id = p_round_id;
  if v_round.campaign_id <> p_campaign_id
     or v_round.opens_at <> p_opens_at
     or v_round.closes_at <> p_closes_at
     or v_round.environment <> p_environment
     or v_round.blind_manifest_sha256 <> p_blind_manifest_sha256
     or v_round.blind_manifest <> p_blind_manifest then
    raise exception 'weekly round identity is already bound to different content'
      using errcode = '23505';
  end if;
  return v_round;
end;
$$;

comment on function public.open_weekly_quiz_round(
  text, text, timestamptz, timestamptz, jsonb, text, jsonb, text
) is
  'Idempotently opens an immutable blind round in an explicit deployment environment. Allows method-labelled pose-only metrics but rejects released-coordinate answers and execution identities.';

-- Backward-compatible service path: every legacy publisher is production.
create or replace function public.open_weekly_quiz_round(
  p_round_id text,
  p_campaign_id text,
  p_opens_at timestamptz,
  p_closes_at timestamptz,
  p_blind_manifest jsonb,
  p_blind_manifest_sha256 text,
  p_metadata jsonb default '{}'::jsonb
)
returns public.weekly_quiz_rounds
language sql
security definer
set search_path = pg_catalog, public
as $$
  select public.open_weekly_quiz_round(
    p_round_id,
    p_campaign_id,
    p_opens_at,
    p_closes_at,
    p_blind_manifest,
    p_blind_manifest_sha256,
    p_metadata,
    'production'
  )
$$;

create or replace view public.public_weekly_quiz_rounds
with (security_barrier = true)
as
select
  round_id, campaign_id, opens_at, closes_at, item_count,
  blind_manifest,
  case when status = 'revealed' then reveal_manifest else null end as reveal_manifest,
  case
    when status = 'revealed' then 'revealed'
    when clock_timestamp() >= closes_at then 'closed'
    when clock_timestamp() >= opens_at then 'open'
    else 'scheduled'
  end as public_status,
  opened_at, revealed_at, environment
from public.weekly_quiz_rounds
where status in ('open', 'revealed');

create or replace function public.get_current_weekly_quiz_round(p_environment text)
returns setof public.public_weekly_quiz_rounds
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select *
    from public.public_weekly_quiz_rounds
   where environment = p_environment
     and p_environment in ('production', 'preview', 'development')
     and opens_at <= clock_timestamp()
   order by opens_at desc
   limit 1
$$;

-- Backward-compatible browser path: every legacy deployment reads production.
create or replace function public.get_current_weekly_quiz_round()
returns setof public.public_weekly_quiz_rounds
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  select * from public.get_current_weekly_quiz_round('production')
$$;

revoke all on function public.open_weekly_quiz_round(
  text, text, timestamptz, timestamptz, jsonb, text, jsonb, text
) from public;
revoke all on function public.open_weekly_quiz_round(
  text, text, timestamptz, timestamptz, jsonb, text, jsonb
) from public;
revoke all on function public.get_current_weekly_quiz_round(text) from public;
revoke all on function public.get_current_weekly_quiz_round() from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    grant execute on function public.get_current_weekly_quiz_round(text) to anon;
    grant execute on function public.get_current_weekly_quiz_round() to anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    grant execute on function public.get_current_weekly_quiz_round(text) to authenticated;
    grant execute on function public.get_current_weekly_quiz_round() to authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.open_weekly_quiz_round(
      text, text, timestamptz, timestamptz, jsonb, text, jsonb, text
    ) to service_role;
    grant execute on function public.open_weekly_quiz_round(
      text, text, timestamptz, timestamptz, jsonb, text, jsonb
    ) to service_role;
  end if;
end;
$$;

comment on column public.weekly_quiz_rounds.environment is
  'Deployment partition. Production callers never select preview/development rounds.';
comment on function public.get_current_weekly_quiz_round(text) is
  'Returns the current public weekly round for one explicit deployment environment.';
comment on function public.get_current_weekly_quiz_round() is
  'Backward-compatible current-round lookup pinned to production.';

commit;
