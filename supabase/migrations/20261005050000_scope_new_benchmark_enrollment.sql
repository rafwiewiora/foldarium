-- New scoped enrollment only. Legacy v1 policy bytes, receipts and manual
-- archival authority are unchanged; no existing round or policy is rewritten.
begin;
create table private.weekly_automation_scoped_enrollments (
  campaign_id text primary key references public.campaigns(campaign_id),
  round_id text unique not null references private.weekly_automation_round_policies(round_id),
  blind_manifest_sha256 text not null check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  source_policy_sha256 text not null check (source_policy_sha256 ~ '^[0-9a-f]{64}$'),
  source_policy jsonb not null,
  enrolled_at timestamptz not null default clock_timestamp()
);
create function private.prevent_weekly_scoped_enrollment_mutation()
returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'scoped benchmark enrollment is immutable' using errcode='23514'; end $$;
create trigger weekly_scoped_enrollment_immutable before update or delete
on private.weekly_automation_scoped_enrollments for each row
execute function private.prevent_weekly_scoped_enrollment_mutation();

create function public.freeze_weekly_automation_policy_v2(
  p_round_id text, p_campaign_id text, p_blind_manifest_sha256 text,
  p_expected_executions jsonb, p_policy_json text, p_source_policy_sha256 text
) returns jsonb language plpgsql security definer
set search_path=pg_catalog,public,private,auth as $$
declare
  p jsonb; scope jsonb; methods jsonb; method jsonb; job jsonb;
  c public.campaigns%rowtype; r public.weekly_quiz_rounds%rowtype;
  existing private.weekly_automation_scoped_enrollments%rowtype;
  first_release date; cap numeric; total numeric := 0; receipt jsonb;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if p_policy_json is null or octet_length(p_policy_json)>65536
    or p_source_policy_sha256 is distinct from encode(extensions.digest(convert_to(p_policy_json,'UTF8'),'sha256'),'hex') then
    raise exception 'scoped policy digest mismatch';
  end if;
  p := p_policy_json::jsonb; scope := p->'enrollment_scope'; methods := p->'required_methods';
  if jsonb_typeof(p) is distinct from 'object' or p->>'schema' is distinct from 'foldarium.weekly-benchmark-policy/v2'
    or (select array_agg(key order by key) from jsonb_object_keys(p) key) is distinct from array['enrollment_scope','policy_id','required_methods','schema']
    or coalesce(p->>'policy_id','') !~ '^[a-z0-9][a-z0-9._-]{0,79}$'
    or jsonb_typeof(scope) is distinct from 'object'
    or (select array_agg(key order by key) from jsonb_object_keys(scope) key) is distinct from array['first_release_date','kind','max_weekly_cost_usd','production_round_suffix']
    or scope->>'kind' is distinct from 'canonical-production-weekly'
    or coalesce(scope->>'first_release_date','') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
    or jsonb_typeof(scope->'production_round_suffix') is distinct from 'string'
    or coalesce(scope->>'production_round_suffix','') !~ '^[a-z0-9][a-z0-9.-]{0,79}$'
    or jsonb_typeof(scope->'max_weekly_cost_usd') is distinct from 'number'
    or jsonb_typeof(methods) is distinct from 'array' or jsonb_array_length(methods) not between 1 and 20
    or jsonb_typeof(p_expected_executions) is distinct from 'array'
    or jsonb_array_length(p_expected_executions) <> jsonb_array_length(methods) then
    raise exception 'invalid scoped benchmark policy';
  end if;
  first_release := (scope->>'first_release_date')::date;
  cap := (scope->>'max_weekly_cost_usd')::numeric;
  if extract(isodow from first_release) <> 6 or cap<=0 then raise exception 'invalid scoped release or cap'; end if;
  for method in select * from jsonb_array_elements(methods) loop
    if jsonb_typeof(method) is distinct from 'object'
      or (select array_agg(key order by key) from jsonb_object_keys(method) key) is distinct from array['config_sha256','driver','max_cost_usd','model_id']
      or jsonb_typeof(method->'driver') is distinct from 'string'
      or jsonb_typeof(method->'model_id') is distinct from 'string'
      or coalesce(method->>'driver','') !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
      or coalesce(length(method->>'model_id'),0) not between 1 and 200
      or coalesce(method->>'config_sha256','') !~ '^[0-9a-f]{64}$'
      or jsonb_typeof(method->'max_cost_usd') is distinct from 'number'
      or (method->>'max_cost_usd')::numeric<=0
      or (select count(*) from jsonb_array_elements(methods) m where m->>'driver'=method->>'driver' and m->>'model_id'=method->>'model_id' and m->>'config_sha256'=method->>'config_sha256') <> 1
      or (select count(*) from jsonb_array_elements(p_expected_executions) e where e-'execution_id'=method) <> 1 then
      raise exception 'scoped benchmark method or execution mismatch';
    end if;
    total := total + (method->>'max_cost_usd')::numeric;
  end loop;
  if total>cap then raise exception 'scoped benchmark weekly round cap exceeded'; end if;
  for job in select * from jsonb_array_elements(p_expected_executions) loop
    if (select array_agg(key order by key) from jsonb_object_keys(job) key) is distinct from array['config_sha256','driver','execution_id','max_cost_usd','model_id'] then
      raise exception 'invalid scoped execution fields';
    end if;
  end loop;
  -- Campaign lock serializes scoped automatic callers; explicit legacy/manual
  -- v1 authority remains separate and is not an account-wide invoice limit.
  select * into strict c from public.campaigns where campaign_id=p_campaign_id for update;
  select * into strict r from public.weekly_quiz_rounds where round_id=p_round_id for update;
  if r.campaign_id is distinct from p_campaign_id or r.environment is distinct from 'production'
    or r.blind_manifest_sha256 is distinct from p_blind_manifest_sha256
    or c.release_date is null or extract(isodow from c.release_date)<>6
    or c.campaign_id is distinct from 'wwpdb-'||c.release_date::text
    or r.round_id is distinct from 'weekly-'||c.release_date::text||'-'||(scope->>'production_round_suffix')
    or c.release_date < first_release then raise exception 'outside canonical benchmark enrollment scope'; end if;
  select * into existing from private.weekly_automation_scoped_enrollments where campaign_id=c.campaign_id;
  if found then
    if existing.round_id is distinct from r.round_id or existing.blind_manifest_sha256 is distinct from p_blind_manifest_sha256
      or existing.source_policy_sha256 is distinct from p_source_policy_sha256 or existing.source_policy is distinct from p then
      raise exception 'scoped weekly enrollment is immutable';
    end if;
    -- Exact retry after lost acknowledgement remains idempotent after close.
    return public.freeze_weekly_automation_policy_v1(p_round_id,p_expected_executions,p_source_policy_sha256);
  end if;
  if r.status is distinct from 'open' or r.opened_at is null or r.revealed_at is not null
    or r.opens_at>clock_timestamp() or r.closes_at<=clock_timestamp() then
    raise exception 'new benchmark enrollment requires current open voting window';
  end if;
  if exists(select 1 from private.weekly_automation_round_policies prior
      join public.weekly_quiz_rounds sibling on sibling.round_id=prior.round_id
      where sibling.campaign_id=c.campaign_id) then
    raise exception 'weekly campaign already has frozen benchmark authority';
  end if;
  receipt := public.freeze_weekly_automation_policy_v1(p_round_id,p_expected_executions,p_source_policy_sha256);
  insert into private.weekly_automation_scoped_enrollments(campaign_id,round_id,blind_manifest_sha256,source_policy_sha256,source_policy)
    values(c.campaign_id,r.round_id,p_blind_manifest_sha256,p_source_policy_sha256,p);
  return receipt;
end $$;

alter table private.weekly_automation_scoped_enrollments enable row level security;
revoke all on table private.weekly_automation_scoped_enrollments from public,anon,authenticated,service_role;
revoke all on function private.prevent_weekly_scoped_enrollment_mutation() from public,anon,authenticated,service_role;
revoke all on function public.freeze_weekly_automation_policy_v2(text,text,text,jsonb,text,text) from public,anon,authenticated;
grant execute on function public.freeze_weekly_automation_policy_v2(text,text,text,jsonb,text,text) to service_role;
commit;
