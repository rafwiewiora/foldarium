-- Accept a separately versioned, provenance-bound missing-bridge disposition.
-- Existing v1 coverage semantics and immutable evaluation catalog versions remain unchanged.
begin;

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
  v_bridge boolean;
  v_extra_keys text[];
  v_atoms text[];
  v_boundaries numeric[];
  v_components numeric;
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
    v_bridge := v_disposition ->> 'policy' = 'foldarium.released-reference-disposition/v2'
      and v_disposition ->> 'code' = 'missing_reference_bridge';
    v_extra_keys := case when coalesce(v_bridge,false) then array['ccd_heavy_atom_graph_sha256',
      'missing_component_boundary_counts','missing_heavy_atom_ids','observed_component_count']::text[] else array[]::text[] end;
    if v_keys is distinct from (select array_agg(k order by k) from unnest(array[
         'code','component_id','expected_heavy_atoms','explicitly_unobserved_heavy_atoms',
         'minimum_reference_coverage','observed_heavy_atoms','policy','reference_coverage','reference_sha256'
       ]::text[] || v_extra_keys) k)
       or (not coalesce(v_bridge,false) and (v_disposition ->> 'policy' is distinct from 'foldarium.released-reference-disposition/v1'
         or v_disposition ->> 'code' is distinct from 'insufficient_reference_coverage'))
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
       or (not coalesce(v_bridge,false) and (v_disposition ->> 'reference_coverage')::numeric >= 0.8)
       or (coalesce(v_bridge,false) and ((v_disposition ->> 'reference_coverage')::numeric < 0.8
         or (v_disposition ->> 'reference_coverage')::numeric >= 1)) then
      raise exception 'invalid unscorable reference coverage' using errcode = '22023';
    end if;
    if coalesce(v_bridge,false) then
      if jsonb_typeof(v_disposition -> 'missing_heavy_atom_ids') is distinct from 'array'
         or jsonb_typeof(v_disposition -> 'missing_component_boundary_counts') is distinct from 'array'
         or jsonb_typeof(v_disposition -> 'observed_component_count') is distinct from 'number'
         or (v_disposition ->> 'observed_component_count') !~ '^[0-9]+$'
         or jsonb_typeof(v_disposition -> 'ccd_heavy_atom_graph_sha256') is distinct from 'string'
         or (v_disposition ->> 'ccd_heavy_atom_graph_sha256') !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid missing reference bridge proof fields' using errcode = '22023';
      end if;
      v_components := (v_disposition ->> 'observed_component_count')::numeric;
      if v_components < 2 or v_components > v_observed
         or jsonb_array_length(v_disposition -> 'missing_heavy_atom_ids') <> v_missing
         or jsonb_array_length(v_disposition -> 'missing_component_boundary_counts') < 1
         or jsonb_array_length(v_disposition -> 'missing_component_boundary_counts') > v_missing
         or exists (select 1 from jsonb_array_elements(v_disposition -> 'missing_heavy_atom_ids') a
           where jsonb_typeof(a) is distinct from 'string' or a #>> '{}' = ''
             or a #>> '{}' <> btrim(a #>> '{}'))
         or exists (select 1 from jsonb_array_elements(v_disposition -> 'missing_component_boundary_counts') b
           where jsonb_typeof(b) is distinct from 'number' or b #>> '{}' !~ '^[0-9]+$') then
        raise exception 'invalid missing reference bridge counts' using errcode = '22023';
      end if;
      select array_agg(a order by ordinal) into v_atoms
        from jsonb_array_elements_text(v_disposition -> 'missing_heavy_atom_ids') with ordinality t(a,ordinal);
      select array_agg(b::numeric order by ordinal) into v_boundaries
        from jsonb_array_elements_text(v_disposition -> 'missing_component_boundary_counts') with ordinality t(b,ordinal);
      if cardinality(v_atoms) <> (select count(distinct a) from unnest(v_atoms) a)
         or v_atoms is distinct from (select array_agg(a order by a collate "C") from unnest(v_atoms) a)
         or v_boundaries is distinct from (select array_agg(b order by b) from unnest(v_boundaries) b)
         or exists (select 1 from unnest(v_boundaries) b where b < 1 or b > v_components)
         or not exists (select 1 from unnest(v_boundaries) b where b >= 2) then
        raise exception 'invalid missing reference bridge topology proof' using errcode = '22023';
      end if;
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

-- Historical research keeps its explicit provenance hashes and identical null semantics.
create or replace function public.publish_weekly_historical_preview_v1(p_round_id text,p_public_artifact_canonical text,p_object_uri text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare s private.weekly_historical_preview_scopes%rowtype; d jsonb; a jsonb; expected_source jsonb; expected_policy jsonb;
 policy private.weekly_automation_round_policies%rowtype; existing private.weekly_historical_preview_publications%rowtype;
 digest text; bucket text; item jsonb; choice jsonb; benchmark jsonb; decision jsonb; x public.weekly_selector_post_close_benchmarks_v1%rowtype;
 blind jsonb; disposition jsonb; unscorable boolean; excluded integer:=0; expected_counts jsonb; expected_items text[]; observed_items text[]; expected_choices text[]; observed_choices text[];
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 s:=private.require_weekly_historical_source(p_round_id);
 select descriptor into strict d from private.weekly_historical_preview_evaluations where round_id=p_round_id and scope_id=s.scope_id;
 select * into strict policy from private.weekly_automation_round_policies where round_id=p_round_id;
 if policy.environment is distinct from 'preview' or policy.blind_manifest_sha256 is distinct from s.blind_manifest_sha256
  or cardinality(policy.expected_execution_ids)<>(select count(*) from private.weekly_automation_benchmarks b where b.round_id=p_round_id)
  or exists(select 1 from unnest(policy.expected_execution_ids) id where not exists(
   select 1 from private.weekly_automation_benchmarks b join public.weekly_selector_post_close_benchmarks_v1 receipt on receipt.execution_id=b.execution_id
   where b.execution_id=id and b.round_id=p_round_id and b.artifact_sha256 is not null and receipt.round_id=p_round_id and receipt.environment=s.environment
    and receipt.provider=b.driver and receipt.requested_model_id=b.model_id and receipt.config_sha256=b.config_sha256 and receipt.execution->>'blind_manifest_sha256'=s.blind_manifest_sha256
    and receipt.execution_sha256=b.verified_execution_sha256 and receipt.payload_digest=b.verified_payload_digest)) then raise exception 'historical publication requires every frozen verified receipt'; end if;
 if p_public_artifact_canonical is null or octet_length(p_public_artifact_canonical)>16777216 then raise exception 'historical public artifact exceeds bound'; end if;
 a:=p_public_artifact_canonical::jsonb;
 digest:=encode(extensions.digest(convert_to(p_public_artifact_canonical,'UTF8'),'sha256'),'hex');
 select split_part(metadata#>>'{private_index,object_uri}','/',3),blind_manifest into strict bucket,blind from public.weekly_quiz_rounds where round_id=p_round_id;
 expected_source:=jsonb_build_object('round_id',s.round_id,'campaign_id',s.campaign_id,'environment',s.environment,'blind_manifest_sha256',s.blind_manifest_sha256,
  'private_index_sha256',s.private_index_sha256,'opens_at',s.opens_at,'closes_at',s.closes_at,'scope_id',s.scope_id,
  'evaluation_id',d->>'evaluation_id','evaluation_artifact_sha256',d->>'artifact_sha256','reveal_manifest_sha256',d->>'reveal_manifest_sha256',
  'reference_set_sha256',d->>'reference_set_sha256','prediction_set_sha256',d->>'prediction_set_sha256');
 expected_counts:=jsonb_build_object('item_count',d->'item_count','choice_count',d->'choice_count');
 if d ? 'excluded_item_count' then expected_counts:=expected_counts||jsonb_build_object('scorable_item_count',d->'scorable_item_count','excluded_item_count',d->'excluded_item_count'); end if;
 expected_policy:=jsonb_build_object('reveal_policy_version',d->>'reveal_policy_version','acceptance_policy_version',d->>'acceptance_policy_version',
  'correct_rmsd_threshold_angstrom',d->'correct_rmsd_threshold_angstrom','evaluator_versions',d->'evaluator_versions');
 if jsonb_typeof(a) is distinct from 'object' or a-array['format_version','scope','human_cohort','source','counts','policy','items','benchmarks']<>'{}'::jsonb
  or a->>'format_version' is distinct from 'foldarium.historical-preview-research/v1' or a->>'scope' is distinct from 'historical-preview-research'
  or a->'human_cohort' is distinct from '{"included":false,"denominator":null,"reason":"no-human-votes-in-historical-research"}'::jsonb
  or a->'source' is distinct from expected_source or a->'policy' is distinct from expected_policy
  or a->'counts' is distinct from expected_counts
  or jsonb_typeof(a->'items') is distinct from 'array' or jsonb_typeof(a->'benchmarks') is distinct from 'array'
  or jsonb_array_length(a->'items')<>(d->>'item_count')::integer or jsonb_array_length(a->'benchmarks')<>cardinality(policy.expected_execution_ids)
  or p_object_uri is distinct from 'supabase://'||bucket||'/sha256/'||left(digest,2)||'/'||digest then raise exception 'historical public artifact identity or cohort differs'; end if;
 select array_agg(i->>'id' order by i->>'id') into expected_items from jsonb_array_elements(blind->'items') i;
 select array_agg(i->>'id' order by i->>'id') into observed_items from jsonb_array_elements(a->'items') i;
 if observed_items is distinct from expected_items then raise exception 'historical public population differs'; end if;
 for item in select value from jsonb_array_elements(a->'items') loop
  if item-array['id','target_id','choices','evaluation_status','reference_disposition']<>'{}'::jsonb or coalesce(item->>'target_id','') !~ '^[A-Z0-9]{4}$' or jsonb_typeof(item->'choices') is distinct from 'array' then raise exception 'historical public item schema differs'; end if;
  unscorable:=item->>'evaluation_status'='unscorable';
  if item->>'evaluation_status' not in ('scored','unscorable') or item->>'evaluation_status' is null then raise exception 'historical evaluation status required'; end if;
  if unscorable then
   disposition:=item->'reference_disposition'; excluded:=excluded+1;
   perform private.foldarium_validate_reveal_item(item);
   if coalesce(disposition->>'component_id','') !~ '^[A-Z0-9]{1,12}$' then
    raise exception 'historical unscorable component invalid';
   end if;
  elsif item ? 'reference_disposition' then raise exception 'scored historical item has disposition'; end if;
  select array_agg(c->>'id' order by c->>'id') into expected_choices from jsonb_array_elements(blind->'items') i cross join lateral jsonb_array_elements(i->'choices') c where i->>'id'=item->>'id';
  select array_agg(c->>'id' order by c->>'id') into observed_choices from jsonb_array_elements(item->'choices') c;
  if observed_choices is distinct from expected_choices then raise exception 'historical public choice population differs'; end if;
  for choice in select value from jsonb_array_elements(item->'choices') loop
   if choice-array['id','method','method_version','rmsd','correct','accepted_correct','evaluator_version','cluster_id','smina_affinity_kcal_mol','reference_sha256']<>'{}'::jsonb
    or coalesce(choice->>'reference_sha256','') !~ '^[0-9a-f]{64}$'
    or coalesce(choice->>'method','') !~ '^[A-Za-z0-9_. -]{1,80}$' or coalesce(choice->>'method_version','') !~ '^[A-Za-z0-9_. /:+-]{1,160}$'
    or coalesce(choice->>'evaluator_version','') !~ '^[A-Za-z0-9_. /:+-]{1,160}$'
    or (unscorable and (choice->'rmsd' is distinct from 'null'::jsonb or choice->'correct' is distinct from 'null'::jsonb or choice->'accepted_correct' is distinct from 'null'::jsonb or choice->>'reference_sha256' is distinct from disposition->>'reference_sha256'))
    or (not unscorable and (jsonb_typeof(choice->'rmsd') is distinct from 'number' or jsonb_typeof(choice->'correct') is distinct from 'boolean' or jsonb_typeof(choice->'accepted_correct') is distinct from 'boolean'))
    then raise exception 'historical public choice schema differs'; end if;
  end loop;
 end loop;
 if excluded<>coalesce((d->>'excluded_item_count')::integer,0) or coalesce((d->>'scorable_item_count')::integer,(d->>'item_count')::integer)<>(d->>'item_count')::integer-excluded then raise exception 'historical scorable population differs'; end if;
 if (select count(distinct b->>'execution_id') from jsonb_array_elements(a->'benchmarks') b)<>cardinality(policy.expected_execution_ids) then raise exception 'historical benchmark executions duplicated'; end if;
 for benchmark in select value from jsonb_array_elements(a->'benchmarks') loop
  select * into strict x from public.weekly_selector_post_close_benchmarks_v1 where execution_id=(benchmark->>'execution_id')::uuid and round_id=p_round_id;
  if not(x.execution_id=any(policy.expected_execution_ids)) or benchmark-array['execution_id','driver','model_id','config_sha256','execution_sha256','payload_digest','decisions']<>'{}'::jsonb
   or benchmark->>'driver' is distinct from x.provider or benchmark->>'model_id' is distinct from x.requested_model_id or benchmark->>'config_sha256' is distinct from x.config_sha256
   or benchmark->>'execution_sha256' is distinct from x.execution_sha256 or benchmark->>'payload_digest' is distinct from x.payload_digest
   or jsonb_typeof(benchmark->'decisions') is distinct from 'array' or jsonb_array_length(benchmark->'decisions')<>cardinality(expected_items) then raise exception 'historical benchmark receipt differs'; end if;
  select array_agg(i->>'item_id' order by i->>'item_id') into observed_items from jsonb_array_elements(benchmark->'decisions') i;
  if observed_items is distinct from expected_items then raise exception 'historical benchmark decision population differs'; end if;
  for decision in select value from jsonb_array_elements(benchmark->'decisions') loop
   select i->>'evaluation_status'='unscorable' into strict unscorable from jsonb_array_elements(a->'items') i where i->>'id'=decision->>'item_id';
   if decision-array['item_id','clustered','unclustered','clustered_correct','unclustered_correct']<>'{}'::jsonb
    or (unscorable and (decision->'clustered_correct' is distinct from 'null'::jsonb or decision->'unclustered_correct' is distinct from 'null'::jsonb))
    or (not unscorable and (jsonb_typeof(decision->'clustered_correct') is distinct from 'boolean' or jsonb_typeof(decision->'unclustered_correct') is distinct from 'boolean'))
    or not exists(select 1 from jsonb_array_elements(x.execution#>'{payload,items}') i where i->>'item_id'=decision->>'item_id' and i->'clustered'=decision->'clustered' and i->'unclustered'=decision->'unclustered') then raise exception 'historical benchmark decision differs from receipt'; end if;
  end loop;
 end loop;
 insert into private.weekly_historical_preview_publications(round_id,scope_id,evaluation_id,public_artifact_sha256,public_artifact_object_uri,public_artifact_size_bytes,item_count,choice_count,required_execution_ids)
 values(p_round_id,s.scope_id,d->>'evaluation_id',digest,p_object_uri,octet_length(p_public_artifact_canonical),(d->>'item_count')::integer,(d->>'choice_count')::integer,policy.expected_execution_ids) on conflict do nothing;
 select * into strict existing from private.weekly_historical_preview_publications where round_id=p_round_id;
 if existing.scope_id<>s.scope_id or existing.evaluation_id<>d->>'evaluation_id' or existing.public_artifact_sha256<>digest or existing.public_artifact_object_uri<>p_object_uri or existing.required_execution_ids<>policy.expected_execution_ids then raise exception 'historical publication already bound differently'; end if;
 return to_jsonb(existing);
end $$;

commit;
