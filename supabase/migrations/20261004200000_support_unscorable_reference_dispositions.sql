-- Preserve incomplete released references as audited unscorable items. Keep
-- every item/choice/vote and all existing lifecycle, receipt and window guards.
begin;

alter table public.weekly_quiz_evaluations
  drop constraint weekly_quiz_evaluations_format_version_check;
alter table public.weekly_quiz_evaluations
  add constraint weekly_quiz_evaluations_format_version_check check (
    format_version in ('foldarium.weekly-private-evaluation/v5', 'foldarium.weekly-private-evaluation/v6')
  );

-- PostgreSQL truncates generated constraint identifiers. Identify only the
-- existing single-column v5 check, and fail closed if the schema differs.
do $$
declare v_name text; v_count integer;
begin
  select min(conname), count(*) into v_name, v_count
    from pg_constraint
   where conrelid = 'public.weekly_retrospective_publications'::regclass
     and contype = 'c'
     and pg_get_constraintdef(oid) like '%evaluation_format_version%'
     and pg_get_constraintdef(oid) like '%foldarium.weekly-private-evaluation/v5%';
  if v_count <> 1 then
    raise exception 'expected one retrospective evaluation format constraint';
  end if;
  execute format('alter table public.weekly_retrospective_publications drop constraint %I', v_name);
end;
$$;
alter table public.weekly_retrospective_publications
  add constraint weekly_retrospective_evaluation_format_check check (
    evaluation_format_version in ('foldarium.weekly-private-evaluation/v5', 'foldarium.weekly-private-evaluation/v6')
  );

create or replace function private.foldarium_validate_reveal_item(p_item jsonb)
returns void
language plpgsql
immutable
set search_path = pg_catalog
as $$
declare
  v_disposition jsonb;
  v_choice jsonb;
  v_status text;
  v_expected numeric;
  v_observed numeric;
  v_missing numeric;
  v_keys text[];
begin
  if jsonb_typeof(p_item) is distinct from 'object'
     or jsonb_typeof(p_item -> 'id') is distinct from 'string'
     or nullif(p_item ->> 'id', '') is null
     or jsonb_typeof(p_item -> 'choices') is distinct from 'array' then
    raise exception 'reveal item has an invalid shape' using errcode = '22023';
  end if;
  if jsonb_array_length(p_item -> 'choices') = 0 or exists (
    select 1 from jsonb_array_elements(p_item -> 'choices') choice
     group by choice.value ->> 'id' having count(*) > 1
  ) then
    raise exception 'reveal choices must be nonempty and unique' using errcode = '22023';
  end if;
  v_status := coalesce(p_item ->> 'evaluation_status', 'scored');
  if v_status = 'unscorable' then
    v_disposition := p_item -> 'reference_disposition';
    if jsonb_typeof(v_disposition) is distinct from 'object' then
      raise exception 'unscorable reference disposition is missing' using errcode = '22023';
    end if;
    select array_agg(key order by key) into v_keys from jsonb_object_keys(v_disposition) key;
    if v_keys is distinct from array[
         'code','component_id','expected_heavy_atoms','explicitly_unobserved_heavy_atoms',
         'minimum_reference_coverage','observed_heavy_atoms','policy','reference_coverage','reference_sha256'
       ]::text[]
       or v_disposition ->> 'policy' is distinct from 'foldarium.released-reference-disposition/v1'
       or v_disposition ->> 'code' is distinct from 'insufficient_reference_coverage'
       or jsonb_typeof(v_disposition -> 'component_id') is distinct from 'string'
       or (v_disposition ->> 'component_id') !~ '[^[:space:]]'
       or jsonb_typeof(v_disposition -> 'reference_sha256') is distinct from 'string'
       or (v_disposition ->> 'reference_sha256') !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(v_disposition -> 'expected_heavy_atoms') is distinct from 'number'
       or jsonb_typeof(v_disposition -> 'observed_heavy_atoms') is distinct from 'number'
       or jsonb_typeof(v_disposition -> 'explicitly_unobserved_heavy_atoms') is distinct from 'number'
       or (v_disposition ->> 'expected_heavy_atoms') !~ '^[0-9]+$'
       or (v_disposition ->> 'observed_heavy_atoms') !~ '^[0-9]+$'
       or (v_disposition ->> 'explicitly_unobserved_heavy_atoms') !~ '^[0-9]+$'
       or jsonb_typeof(v_disposition -> 'reference_coverage') is distinct from 'number'
       or v_disposition -> 'minimum_reference_coverage' is distinct from '0.8'::jsonb then
      raise exception 'invalid unscorable reference proof fields' using errcode = '22023';
    end if;
    v_expected := (v_disposition ->> 'expected_heavy_atoms')::numeric;
    v_observed := (v_disposition ->> 'observed_heavy_atoms')::numeric;
    v_missing := (v_disposition ->> 'explicitly_unobserved_heavy_atoms')::numeric;
    if v_observed <= 0 or v_expected <= v_observed
       or greatest(v_expected, v_observed, v_missing) > 9007199254740991
       or v_missing <> v_expected - v_observed then
      raise exception 'invalid unscorable reference atom counts' using errcode = '22023';
    end if;
    -- Match the scientific validator's binary64 observed/expected ratio exactly.
    if (v_disposition ->> 'reference_coverage')::double precision
         <> v_observed::double precision / v_expected::double precision
       or (v_disposition ->> 'reference_coverage')::numeric >= 0.8 then
      raise exception 'invalid unscorable reference coverage' using errcode = '22023';
    end if;
  elsif v_status <> 'scored' or p_item -> 'reference_disposition' not in ('null'::jsonb) then
    raise exception 'unknown evaluation status or scored reference disposition' using errcode = '22023';
  end if;
  for v_choice in select value from jsonb_array_elements(p_item -> 'choices') loop
    if jsonb_typeof(v_choice) is distinct from 'object'
       or jsonb_typeof(v_choice -> 'id') is distinct from 'string'
       or nullif(v_choice ->> 'id', '') is null then
      raise exception 'reveal choice has an invalid shape' using errcode = '22023';
    end if;
    if v_status = 'unscorable' then
      if v_choice -> 'rmsd' is distinct from 'null'::jsonb
         or v_choice -> 'correct' is distinct from 'null'::jsonb
         or v_choice -> 'accepted_correct' is distinct from 'null'::jsonb
         or jsonb_typeof(v_choice -> 'reference_sha256') is distinct from 'string'
         or v_choice ->> 'reference_sha256' is distinct from v_disposition ->> 'reference_sha256' then
        raise exception 'unscorable choices require explicit null metrics and exact reference digest'
          using errcode = '22023';
      end if;
    elsif jsonb_typeof(v_choice -> 'correct') is distinct from 'boolean'
       or jsonb_typeof(v_choice -> 'rmsd') is distinct from 'number' then
      raise exception 'scored choices require boolean correctness and numeric RMSD'
        using errcode = '22023';
    elsif (v_choice ->> 'rmsd')::numeric < 0 then
      raise exception 'scored RMSD must be nonnegative' using errcode = '22023';
    end if;
  end loop;
end;
$$;
revoke all on function private.foldarium_validate_reveal_item(jsonb) from public;

create or replace function public.reveal_weekly_quiz_round(
  p_round_id text,
  p_reveal_manifest jsonb,
  p_reveal_manifest_sha256 text
)
returns public.weekly_quiz_rounds
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_round public.weekly_quiz_rounds%rowtype;
begin
  select * into v_round from public.weekly_quiz_rounds
   where round_id = p_round_id for update;
  if not found then
    raise exception 'unknown weekly round: %', p_round_id using errcode = 'P0002';
  end if;
  if v_round.status = 'revealed' then
    if v_round.reveal_manifest_sha256 = p_reveal_manifest_sha256
       and v_round.reveal_manifest = p_reveal_manifest then
      return v_round;
    end if;
    raise exception 'weekly round is already revealed with different content'
      using errcode = '23505';
  end if;
  if v_round.status <> 'open'
     or clock_timestamp() < v_round.closes_at
     or jsonb_typeof(p_reveal_manifest) is distinct from 'object'
     or (p_reveal_manifest -> 'schema_version') is distinct from '1'::jsonb
     or p_reveal_manifest ->> 'round_id' is distinct from p_round_id
     or p_reveal_manifest ->> 'blind_manifest_sha256'
          is distinct from v_round.blind_manifest_sha256
     or jsonb_typeof(p_reveal_manifest -> 'items') is distinct from 'array'
     or p_reveal_manifest_sha256 is null
     or p_reveal_manifest_sha256 !~ '^[0-9a-f]{64}$' then
    raise exception 'weekly round cannot be revealed yet or reveal manifest is invalid'
      using errcode = '23514';
  end if;
  if exists (
    (select value ->> 'id' from jsonb_array_elements(v_round.blind_manifest -> 'items'))
    except
    (select value ->> 'id' from jsonb_array_elements(p_reveal_manifest -> 'items'))
  ) or exists (
    (select value ->> 'id' from jsonb_array_elements(p_reveal_manifest -> 'items'))
    except
    (select value ->> 'id' from jsonb_array_elements(v_round.blind_manifest -> 'items'))
  ) then
    raise exception 'reveal item IDs do not match the blind manifest'
      using errcode = '22023';
  end if;
  -- Validate each item before calling array operations; no null is interpreted
  -- as a failed pose or a correct None response.
  perform private.foldarium_validate_reveal_item(value)
    from jsonb_array_elements(p_reveal_manifest -> 'items');
  if jsonb_array_length(p_reveal_manifest -> 'items')
       <> jsonb_array_length(v_round.blind_manifest -> 'items')
     or exists (
       select 1 from jsonb_array_elements(p_reveal_manifest -> 'items') item
        group by item.value ->> 'id' having count(*) > 1
     ) then
    raise exception 'reveal item IDs must be unique and complete' using errcode = '22023';
  end if;
  if exists (
    select 1
      from jsonb_array_elements(v_round.blind_manifest -> 'items') as blind(value)
      join jsonb_array_elements(p_reveal_manifest -> 'items') as reveal(value)
        on reveal.value ->> 'id' = blind.value ->> 'id'
     where jsonb_array_length(reveal.value -> 'choices')
          <> jsonb_array_length(blind.value -> 'choices')
        or exists (
          (select value ->> 'id' from jsonb_array_elements(blind.value -> 'choices'))
          except
          (select value ->> 'id' from jsonb_array_elements(reveal.value -> 'choices'))
        )
        or exists (
          (select value ->> 'id' from jsonb_array_elements(reveal.value -> 'choices'))
          except
          (select value ->> 'id' from jsonb_array_elements(blind.value -> 'choices'))
        )
  ) then
    raise exception 'reveal choice IDs do not exactly match the blind manifest'
      using errcode = '22023';
  end if;

  update public.weekly_quiz_rounds
     set status = 'revealed',
         reveal_manifest = p_reveal_manifest,
         reveal_manifest_sha256 = p_reveal_manifest_sha256,
         revealed_at = clock_timestamp()
   where round_id = p_round_id
   returning * into v_round;
  return v_round;
end;
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

  if v_evaluation.format_version not in ('foldarium.weekly-private-evaluation/v5', 'foldarium.weekly-private-evaluation/v6')
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
    raise exception 'weekly retrospective is not bound to the exact versioned evaluation'
      using errcode = '23514';
  end if;
  return new;
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
     and evaluation.format_version in ('foldarium.weekly-private-evaluation/v5', 'foldarium.weekly-private-evaluation/v6')
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

comment on function public.list_missing_weekly_retrospective_publications() is
  'Service-role-only backfill scan over revealed production rounds with an exact v5/v6 evaluation and no publication.';
-- Weekly vote RPCs intentionally store choices, not computed correctness. Keep
-- their privileges and append-only provenance unchanged. Client and artifact
-- scorers exclude unscorable items from accuracy denominators, including None.
commit;
