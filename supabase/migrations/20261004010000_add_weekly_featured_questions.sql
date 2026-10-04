-- Freeze a small human-facing draw without changing the full scientific round.
begin;

create table private.weekly_featured_question_selections (
  round_id text primary key references public.weekly_quiz_rounds(round_id),
  blind_manifest_sha256 text not null check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  featured_questions jsonb not null check (jsonb_typeof(featured_questions) = 'object'),
  selection_artifact jsonb not null check (jsonb_typeof(selection_artifact) = 'object'),
  registered_at timestamptz not null default clock_timestamp()
);

create function private.prevent_weekly_featured_selection_mutation()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'weekly featured question selection is immutable' using errcode = '23514';
end;
$$;

create trigger weekly_featured_selection_immutable
before update or delete on private.weekly_featured_question_selections
for each row execute function private.prevent_weekly_featured_selection_mutation();

create function public.register_weekly_featured_questions(
  p_round_id text,
  p_featured_questions jsonb,
  p_selection_canonical text,
  p_selection_artifact jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
  v_existing private.weekly_featured_question_selections%rowtype;
  v_audit jsonb;
  v_digest text;
  v_expected jsonb;
  v_item_count integer;
  v_candidate_count integer;
  v_requested_count integer;
  v_selected_count integer;
  v_private_bucket text;
begin
  if nullif(p_round_id, '') is null
     or p_selection_canonical is null
     or octet_length(p_selection_canonical) > 10485760
     or jsonb_typeof(p_featured_questions) is distinct from 'object'
     or jsonb_typeof(p_selection_artifact) is distinct from 'object' then
    raise exception 'invalid featured question registration' using errcode = '22023';
  end if;
  v_audit := p_selection_canonical::jsonb;
  v_digest := encode(extensions.digest(convert_to(p_selection_canonical, 'UTF8'), 'sha256'), 'hex');
  select * into v_round from public.weekly_quiz_rounds
   where round_id = p_round_id for update;
  if not found then
    raise exception 'unknown weekly round' using errcode = 'P0002';
  end if;
  if jsonb_typeof(v_audit) is distinct from 'object'
     or v_audit -> 'schema_version' is distinct from '1'::jsonb
     or v_audit ->> 'policy' is distinct from 'foldarium-weekly-question-draw/v1'
     or coalesce(v_audit ->> 'mode', '') not in ('uniform', 'interestingness_weighted')
     or jsonb_typeof(v_audit -> 'seed') is distinct from 'string'
     or nullif(btrim(v_audit ->> 'seed'), '') is null
     or v_audit ->> 'source_round_id' is distinct from v_round.round_id
     or v_audit ->> 'source_blind_manifest_sha256' is distinct from v_round.blind_manifest_sha256
     or v_audit ->> 'source_private_index_sha256' is distinct from v_round.metadata #>> '{private_index,sha256}'
     or coalesce(v_audit ->> 'candidate_population_sha256', '') !~ '^[0-9a-f]{64}$'
     or coalesce(v_audit ->> 'candidate_evidence_sha256', '') !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(v_audit -> 'included_item_ids') is distinct from 'array'
     or jsonb_typeof(v_audit -> 'candidates') is distinct from 'array'
     or jsonb_typeof(v_audit -> 'source_item_count') is distinct from 'number'
     or jsonb_typeof(v_audit -> 'candidate_count') is distinct from 'number'
     or jsonb_typeof(v_audit -> 'requested_question_count') is distinct from 'number'
     or jsonb_typeof(v_audit -> 'selected_question_count') is distinct from 'number'
     or coalesce(v_audit ->> 'source_item_count', '') !~ '^[1-9][0-9]*$'
     or coalesce(v_audit ->> 'candidate_count', '') !~ '^[1-9][0-9]*$'
     or coalesce(v_audit ->> 'requested_question_count', '') !~ '^[1-5]$'
     or coalesce(v_audit ->> 'selected_question_count', '') !~ '^[1-5]$' then
    raise exception 'featured selection audit does not bind this full blind round' using errcode = '23514';
  end if;
  v_item_count := (v_audit ->> 'source_item_count')::integer;
  v_candidate_count := (v_audit ->> 'candidate_count')::integer;
  v_requested_count := (v_audit ->> 'requested_question_count')::integer;
  v_selected_count := (v_audit ->> 'selected_question_count')::integer;
  if v_item_count <> v_round.item_count
     or v_item_count <> jsonb_array_length(v_round.blind_manifest -> 'items')
     or v_candidate_count > v_item_count
     or v_candidate_count <> jsonb_array_length(v_audit -> 'candidates')
     or v_selected_count <> least(v_requested_count, v_candidate_count)
     or v_selected_count <> jsonb_array_length(v_audit -> 'included_item_ids') then
    raise exception 'featured selection counts do not match the full population' using errcode = '23514';
  end if;
  if exists (
      select 1 from jsonb_array_elements(v_audit -> 'included_item_ids') id(value)
       where jsonb_typeof(id.value) <> 'string' or nullif(id.value #>> '{}', '') is null
          or not exists (select 1 from jsonb_array_elements(v_round.blind_manifest -> 'items') item
                         where item ->> 'id' = id.value #>> '{}')
    ) or (select count(distinct value) from jsonb_array_elements(v_audit -> 'included_item_ids')) <> v_selected_count
    or exists (
      select 1 from jsonb_array_elements(v_audit -> 'candidates') candidate
       where jsonb_typeof(candidate) <> 'object'
          or jsonb_typeof(candidate -> 'item_id') is distinct from 'string'
          or not exists (select 1 from jsonb_array_elements(v_round.blind_manifest -> 'items') item
                         where item ->> 'id' = candidate ->> 'item_id')
    ) or (select count(distinct candidate ->> 'item_id')
            from jsonb_array_elements(v_audit -> 'candidates') candidate) <> v_candidate_count
    or exists (
      select 1 from jsonb_array_elements_text(v_audit -> 'included_item_ids') selected(item_id)
       where not exists (select 1 from jsonb_array_elements(v_audit -> 'candidates') candidate
                          where candidate ->> 'item_id' = selected.item_id)
    ) then
    raise exception 'featured selection item IDs are invalid or duplicated' using errcode = '23514';
  end if;
  v_expected := jsonb_build_object(
    'schema_version', 1,
    'policy', v_audit ->> 'policy',
    'mode', v_audit ->> 'mode',
    'seed', v_audit ->> 'seed',
    'blind_manifest_sha256', v_round.blind_manifest_sha256,
    'candidate_population_sha256', v_audit ->> 'candidate_population_sha256',
    'selection_sha256', v_digest,
    'source_item_count', v_item_count,
    'candidate_count', v_candidate_count,
    'requested_question_count', v_requested_count,
    'selected_question_count', v_selected_count,
    'item_ids', v_audit -> 'included_item_ids'
  );
  if p_featured_questions is distinct from v_expected then
    raise exception 'featured public marker differs from its private audit' using errcode = '23514';
  end if;
  v_private_bucket := split_part(v_round.metadata #>> '{private_index,object_uri}', '/', 3);
  if nullif(v_private_bucket, '') is null
     or p_selection_artifact ->> 'object_uri' is distinct from
        'supabase://' || v_private_bucket || '/sha256/' || left(v_digest, 2) || '/' || v_digest
     or p_selection_artifact ->> 'sha256' is distinct from v_digest
     or p_selection_artifact ->> 'media_type' is distinct from 'application/json'
     or p_selection_artifact -> 'size_bytes' is distinct from to_jsonb(octet_length(convert_to(p_selection_canonical, 'UTF8'))) then
    raise exception 'featured selection artifact must match its private content digest' using errcode = '23514';
  end if;
  select * into v_existing from private.weekly_featured_question_selections where round_id = p_round_id;
  if found then
    if v_existing.blind_manifest_sha256 is distinct from v_round.blind_manifest_sha256
       or v_existing.featured_questions is distinct from p_featured_questions
       or v_existing.selection_artifact is distinct from p_selection_artifact then
      raise exception 'weekly featured question selection is already frozen' using errcode = '23514';
    end if;
    return jsonb_build_object('status', 'already-registered', 'round_id', p_round_id,
                             'selection_sha256', v_digest);
  end if;
  if v_round.status <> 'open' or v_round.reveal_manifest is not null then
    raise exception 'featured questions require an unrevealed weekly round' using errcode = '23514';
  end if;
  insert into private.weekly_featured_question_selections (
    round_id, blind_manifest_sha256, featured_questions, selection_artifact
  ) values (p_round_id, v_round.blind_manifest_sha256, p_featured_questions, p_selection_artifact);
  return jsonb_build_object('status', 'registered', 'round_id', p_round_id,
                           'selection_sha256', v_digest);
end;
$$;

revoke all on table private.weekly_featured_question_selections from public;
revoke all on function private.prevent_weekly_featured_selection_mutation() from public;
revoke all on function public.register_weekly_featured_questions(text, jsonb, text, jsonb) from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table private.weekly_featured_question_selections from anon;
    revoke all on function public.register_weekly_featured_questions(text, jsonb, text, jsonb) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table private.weekly_featured_question_selections from authenticated;
    revoke all on function public.register_weekly_featured_questions(text, jsonb, text, jsonb) from authenticated;
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    revoke all on table private.weekly_featured_question_selections from service_role;
    grant execute on function public.register_weekly_featured_questions(text, jsonb, text, jsonb) to service_role;
  end if;
end;
$$;

-- Append columns to preserve the view row type consumed by existing round RPCs.
-- This is an explicit public projection; neither audit artifacts nor the round's
-- private metadata are exposed.
create or replace view public.public_weekly_quiz_rounds
with (security_barrier = true)
as
select
  round.round_id, round.campaign_id, round.opens_at, round.closes_at, round.item_count,
  round.blind_manifest,
  case when round.status = 'revealed' then round.reveal_manifest else null end as reveal_manifest,
  case
    when round.status = 'revealed' then 'revealed'
    when clock_timestamp() >= round.closes_at then 'closed'
    when clock_timestamp() >= round.opens_at then 'open'
    else 'scheduled'
  end as public_status,
  round.opened_at, round.revealed_at, round.environment,
  featured.featured_questions,
  round.blind_manifest_sha256
from public.weekly_quiz_rounds round
left join private.weekly_featured_question_selections featured using (round_id)
where round.status in ('open', 'revealed');

comment on table private.weekly_featured_question_selections is
  'Immutable featured human-question draws; full manifests, selector kits, votes, and benchmark populations are unchanged.';
notify pgrst, 'reload schema';
commit;
