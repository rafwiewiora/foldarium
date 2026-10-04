-- Separate, explicitly authorized research archive for expired Preview sources.
-- No round opens/closes, environment, ballots or production archive rows change.
begin;

create table private.weekly_historical_preview_scopes (
 round_id text primary key references public.weekly_quiz_rounds(round_id),
 scope_id uuid not null unique default gen_random_uuid(),
 campaign_id text not null, environment text not null check(environment='preview'),
 blind_manifest_sha256 text not null, private_index_sha256 text not null,
 opens_at timestamptz not null, closes_at timestamptz not null,
 authorized_at timestamptz not null default clock_timestamp()
);
create table private.weekly_historical_preview_evaluations (
 round_id text primary key references private.weekly_historical_preview_scopes(round_id),
 scope_id uuid not null references private.weekly_historical_preview_scopes(scope_id),
 descriptor jsonb not null check(jsonb_typeof(descriptor)='object'),
 registered_at timestamptz not null default clock_timestamp()
);
create table private.weekly_historical_preview_publications (
 round_id text primary key references private.weekly_historical_preview_scopes(round_id),
 scope_id uuid not null references private.weekly_historical_preview_scopes(scope_id),
 evaluation_id text not null, public_artifact_sha256 text not null,
 public_artifact_object_uri text not null, public_artifact_size_bytes bigint not null,
 item_count integer not null, choice_count integer not null,
 required_execution_ids uuid[] not null,
 published_at timestamptz not null default clock_timestamp()
);
create function private.prevent_historical_preview_mutation()
returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'historical Preview authority and artifacts are immutable' using errcode='23514'; end $$;
create trigger historical_preview_scope_immutable before update or delete on private.weekly_historical_preview_scopes for each row execute function private.prevent_historical_preview_mutation();
create trigger historical_preview_evaluation_immutable before update or delete on private.weekly_historical_preview_evaluations for each row execute function private.prevent_historical_preview_mutation();
create trigger historical_preview_publication_immutable before update or delete on private.weekly_historical_preview_publications for each row execute function private.prevent_historical_preview_mutation();

create function public.authorize_weekly_historical_preview_v1(p_round_id text,p_blind_manifest_sha256 text,p_private_index_sha256 text,p_opens_at timestamptz,p_closes_at timestamptz)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare r public.weekly_quiz_rounds%rowtype; s private.weekly_historical_preview_scopes%rowtype;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 select * into strict r from public.weekly_quiz_rounds where round_id=p_round_id for update;
 if r.environment is distinct from 'preview' or r.status is distinct from 'open' or r.revealed_at is not null or r.reveal_manifest is not null
  or r.closes_at>clock_timestamp() or r.opens_at is distinct from p_opens_at or r.closes_at is distinct from p_closes_at
  or r.blind_manifest_sha256 is distinct from p_blind_manifest_sha256 or r.metadata#>>'{private_index,sha256}' is distinct from p_private_index_sha256
  or p_blind_manifest_sha256 !~ '^[0-9a-f]{64}$' or p_private_index_sha256 is null or p_private_index_sha256 !~ '^[0-9a-f]{64}$' then
  raise exception 'historical authorization requires exact original closed Preview source'; end if;
 insert into private.weekly_historical_preview_scopes(round_id,campaign_id,environment,blind_manifest_sha256,private_index_sha256,opens_at,closes_at)
 values(r.round_id,r.campaign_id,r.environment,r.blind_manifest_sha256,p_private_index_sha256,r.opens_at,r.closes_at) on conflict do nothing;
 select * into strict s from private.weekly_historical_preview_scopes where round_id=p_round_id;
 if s.campaign_id is distinct from r.campaign_id or s.environment is distinct from r.environment or s.blind_manifest_sha256 is distinct from r.blind_manifest_sha256 or s.private_index_sha256 is distinct from p_private_index_sha256 or s.opens_at is distinct from r.opens_at or s.closes_at is distinct from r.closes_at then raise exception 'historical scope already bound differently'; end if;
 return to_jsonb(s);
end $$;

create function private.require_weekly_historical_source(p_round_id text)
returns private.weekly_historical_preview_scopes language plpgsql set search_path=pg_catalog,public,private as $$
declare r public.weekly_quiz_rounds%rowtype; s private.weekly_historical_preview_scopes%rowtype;
begin
 select * into strict r from public.weekly_quiz_rounds where round_id=p_round_id for update;
 select * into strict s from private.weekly_historical_preview_scopes where round_id=p_round_id;
 if r.environment is distinct from 'preview' or r.status is distinct from 'open' or r.revealed_at is not null or r.reveal_manifest is not null or r.closes_at>clock_timestamp()
  or s.campaign_id is distinct from r.campaign_id or s.environment is distinct from r.environment or s.blind_manifest_sha256 is distinct from r.blind_manifest_sha256
  or s.private_index_sha256 is distinct from r.metadata#>>'{private_index,sha256}' or s.opens_at is distinct from r.opens_at or s.closes_at is distinct from r.closes_at then
  raise exception 'historical source changed or is no longer closed'; end if;
 return s;
end $$;

create function public.get_weekly_historical_preview_v1(p_round_id text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 return jsonb_build_object('scope',(select to_jsonb(s) from private.weekly_historical_preview_scopes s where s.round_id=p_round_id),
  'evaluation',(select e.descriptor from private.weekly_historical_preview_evaluations e where e.round_id=p_round_id),
  'publication',(select to_jsonb(p) from private.weekly_historical_preview_publications p where p.round_id=p_round_id));
end $$;

create function public.register_weekly_historical_evaluation_v1(p_round_id text,p_descriptor jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare s private.weekly_historical_preview_scopes%rowtype; old_descriptor jsonb; bucket text; d jsonb:=p_descriptor; blind jsonb;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 s:=private.require_weekly_historical_source(p_round_id);
 select split_part(metadata#>>'{private_index,object_uri}','/',3),blind_manifest into strict bucket,blind from public.weekly_quiz_rounds where round_id=p_round_id;
 if jsonb_typeof(d) is distinct from 'object' or d->>'format_version' is distinct from 'foldarium.historical-preview-evaluation/v1'
  or d->>'round_id' is distinct from s.round_id or d->>'campaign_id' is distinct from s.campaign_id or d->>'environment' is distinct from s.environment
  or (d->>'round_opens_at')::timestamptz is distinct from s.opens_at or (d->>'round_closes_at')::timestamptz is distinct from s.closes_at
  or d->>'blind_manifest_sha256' is distinct from s.blind_manifest_sha256 or d->>'private_index_sha256' is distinct from s.private_index_sha256
  or coalesce(d->>'artifact_sha256','') !~ '^[0-9a-f]{64}$' or coalesce(d->>'reveal_manifest_sha256','') !~ '^[0-9a-f]{64}$'
  or coalesce(d->>'reference_set_sha256','') !~ '^[0-9a-f]{64}$' or coalesce(d->>'prediction_set_sha256','') !~ '^[0-9a-f]{64}$'
  or coalesce(d->>'evaluation_id','')='' or coalesce((d->>'item_count')::integer,0)<1 or coalesce((d->>'choice_count')::integer,0)<1
  or (d->>'item_count')::integer is distinct from jsonb_array_length(blind->'items')
  or (d->>'choice_count')::integer is distinct from (select count(*)::integer from jsonb_array_elements(blind->'items') i cross join lateral jsonb_array_elements(i->'choices') c)
  or coalesce((d->>'artifact_size_bytes')::bigint,0)<1 or coalesce(bucket,'')='' or bucket in ('weekly-public','foldarium-weekly-quiz') or d->>'artifact_media_type' is distinct from 'application/json'
  or d->>'artifact_object_uri' is distinct from 'supabase://'||bucket||'/sha256/'||left(d->>'artifact_sha256',2)||'/'||(d->>'artifact_sha256') then
  raise exception 'historical evaluation descriptor differs from authorized source'; end if;
 insert into private.weekly_historical_preview_evaluations(round_id,scope_id,descriptor) values(p_round_id,s.scope_id,d) on conflict do nothing;
 select descriptor into strict old_descriptor from private.weekly_historical_preview_evaluations where round_id=p_round_id;
 if old_descriptor<>d then raise exception 'historical evaluation already bound differently'; end if;
 return old_descriptor;
end $$;

create function public.publish_weekly_historical_preview_v1(p_round_id text,p_public_artifact_canonical text,p_object_uri text)
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
   if jsonb_typeof(disposition) is distinct from 'object' or disposition-array['policy','code','component_id','expected_heavy_atoms','observed_heavy_atoms','explicitly_unobserved_heavy_atoms','reference_coverage','minimum_reference_coverage','reference_sha256']<>'{}'::jsonb
    or disposition->>'policy' is distinct from 'foldarium.released-reference-disposition/v1' or disposition->>'code' is distinct from 'insufficient_reference_coverage'
    or coalesce(disposition->>'component_id','') !~ '^[A-Z0-9]{1,12}$' or coalesce(disposition->>'reference_sha256','') !~ '^[0-9a-f]{64}$'
    or coalesce(disposition->>'expected_heavy_atoms','') !~ '^[1-9][0-9]*$' or coalesce(disposition->>'observed_heavy_atoms','') !~ '^[1-9][0-9]*$'
    or (disposition->>'observed_heavy_atoms')::integer >= (disposition->>'expected_heavy_atoms')::integer
    or (disposition->>'explicitly_unobserved_heavy_atoms')::integer is distinct from (disposition->>'expected_heavy_atoms')::integer-(disposition->>'observed_heavy_atoms')::integer
    or (disposition->>'minimum_reference_coverage')::numeric is distinct from .8
    or (disposition->>'reference_coverage')::double precision is distinct from (disposition->>'observed_heavy_atoms')::double precision/(disposition->>'expected_heavy_atoms')::double precision
    or (disposition->>'reference_coverage')::numeric>=.8 then raise exception 'historical unscorable disposition invalid'; end if;
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

create view public.public_weekly_historical_preview_research as
select p.round_id,s.campaign_id,s.environment,s.opens_at,s.closes_at,s.blind_manifest_sha256,p.scope_id,p.evaluation_id,
 p.public_artifact_sha256,p.public_artifact_size_bytes,p.item_count,p.choice_count,p.required_execution_ids,p.published_at,
 'historical-preview-research'::text as publication_scope,false as human_votes_included
from private.weekly_historical_preview_publications p join private.weekly_historical_preview_scopes s using(round_id,scope_id)
join public.weekly_quiz_rounds r on r.round_id=s.round_id
where r.environment='preview' and r.status='open' and r.revealed_at is null and r.reveal_manifest is null and r.closes_at<=clock_timestamp()
 and r.campaign_id=s.campaign_id and r.opens_at=s.opens_at and r.closes_at=s.closes_at and r.blind_manifest_sha256=s.blind_manifest_sha256
 and r.metadata#>>'{private_index,sha256}'=s.private_index_sha256;
grant select on public.public_weekly_historical_preview_research to anon,authenticated,service_role;

alter table private.weekly_historical_preview_scopes enable row level security;
alter table private.weekly_historical_preview_evaluations enable row level security;
alter table private.weekly_historical_preview_publications enable row level security;
revoke all on private.weekly_historical_preview_scopes,private.weekly_historical_preview_evaluations,private.weekly_historical_preview_publications from public,anon,authenticated,service_role;
revoke all on function private.prevent_historical_preview_mutation(),private.require_weekly_historical_source(text) from public,anon,authenticated,service_role;
revoke all on function public.authorize_weekly_historical_preview_v1(text,text,text,timestamptz,timestamptz),public.get_weekly_historical_preview_v1(text),public.register_weekly_historical_evaluation_v1(text,jsonb),public.publish_weekly_historical_preview_v1(text,text,text) from public,anon,authenticated;
grant execute on function public.authorize_weekly_historical_preview_v1(text,text,text,timestamptz,timestamptz),public.get_weekly_historical_preview_v1(text),public.register_weekly_historical_evaluation_v1(text,jsonb),public.publish_weekly_historical_preview_v1(text,text,text) to service_role;

create or replace function public.weekly_automation_snapshot_v1()
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare result jsonb;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if (select count(*) from public.weekly_quiz_rounds)>1000 or (select count(*) from public.prerelease_snapshots)>1000 then
    raise exception 'weekly automation snapshot exceeds bound; archive/configuration review required';
  end if;
  select jsonb_build_object(
    'campaigns',coalesce((select jsonb_agg(jsonb_build_object('campaign_id',s.campaign_id,'release_date',s.release_date,
      'runs',coalesce((select jsonb_agg(jsonb_build_object('run_id',p.run_id,'status',p.status,'attempt_count',p.attempt_count))
        from public.prediction_runs p join public.targets t using(target_id) where t.campaign_id=s.campaign_id),'[]'::jsonb)))
      from (select distinct campaign_id,release_date from public.prerelease_snapshots) s),'[]'::jsonb),
    'rounds',coalesce((select jsonb_agg(jsonb_build_object(
      'round_id',r.round_id,'campaign_id',r.campaign_id,'environment',r.environment,'status',r.status,
      'opens_at',r.opens_at,'closes_at',r.closes_at,'opened_at',r.opened_at,'revealed_at',r.revealed_at,
      'blind_manifest_sha256',r.blind_manifest_sha256,
      'private_index_sha256',r.metadata#>>'{private_index,sha256}',
      'historical_scope',(select to_jsonb(h) from private.weekly_historical_preview_scopes h where h.round_id=r.round_id),
      'historical_evaluation_ready',exists(select 1 from private.weekly_historical_preview_evaluations h join private.weekly_historical_preview_scopes s using(round_id,scope_id) where h.round_id=r.round_id and s.campaign_id=r.campaign_id and s.environment=r.environment and s.blind_manifest_sha256=r.blind_manifest_sha256 and s.private_index_sha256=r.metadata#>>'{private_index,sha256}' and s.opens_at=r.opens_at and s.closes_at=r.closes_at),
      'historical_published',exists(select 1 from private.weekly_historical_preview_publications h where h.round_id=r.round_id),
      'metadata',jsonb_build_object('retrospective_release',r.metadata->'retrospective_release'),
      'kit',(select jsonb_build_object('kit_sha256',k.kit_sha256,'blind_manifest_sha256',k.blind_manifest_sha256,'storage_path',k.storage_path,'descriptor',k.descriptor) from private.weekly_selector_kit_catalog k where k.round_id=r.round_id),
      'featured_registered',exists(select 1 from private.weekly_featured_question_selections f where f.round_id=r.round_id and f.blind_manifest_sha256=r.blind_manifest_sha256),
      'evaluation_ready',exists(select 1 from public.weekly_quiz_evaluations e where e.round_id=r.round_id and e.round_closes_at=r.closes_at and e.blind_manifest_sha256=r.blind_manifest_sha256 and e.environment=r.environment and e.campaign_id=r.campaign_id and e.round_opens_at=r.opens_at and e.private_index_sha256=r.metadata#>>'{private_index,sha256}' and e.evaluation_id is not null and e.reveal_manifest_sha256 is not null),
      'retrospective_published',exists(select 1 from public.weekly_retrospective_publications p where p.round_id=r.round_id),
      'automation_policy',(select to_jsonb(p) from private.weekly_automation_round_policies p where p.round_id=r.round_id),
      'benchmark_jobs',coalesce((select jsonb_agg(to_jsonb(b)||jsonb_build_object('receipt',
        (select jsonb_build_object('execution_id',x.execution_id,'execution_sha256',x.execution_sha256,'payload_digest',x.payload_digest)
         from public.weekly_selector_post_close_benchmarks_v1 x where x.execution_id=b.execution_id and x.round_id=r.round_id and x.environment=r.environment and x.provider=b.driver and x.requested_model_id=b.model_id and x.config_sha256=b.config_sha256 and x.execution->>'blind_manifest_sha256'=r.blind_manifest_sha256 and x.execution_sha256=b.verified_execution_sha256 and x.payload_digest=b.verified_payload_digest)))
        from private.weekly_automation_benchmarks b where b.round_id=r.round_id),'[]'::jsonb)
    )) from public.weekly_quiz_rounds r),'[]'::jsonb),
    'action_history',coalesce((select jsonb_object_agg(a.action_key,jsonb_build_object('updated_at',a.updated_at,'dispatch_receipt',a.dispatch_receipt)) from private.weekly_automation_actions a),'{}'::jsonb),
    'failed_actions',coalesce((select jsonb_agg(jsonb_build_object('action_key',a.action_key,'kind',a.action->>'kind','identity',a.action->>'identity','last_error',a.last_error)) from private.weekly_automation_actions a where a.status='failed'),'[]'::jsonb)
  ) into result;
  return result;
end $$;
commit;
