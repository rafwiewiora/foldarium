-- Service-only desired-state reconciliation. No existing rounds/votes are changed.
begin;

create table private.weekly_automation_actions (
  action_key text primary key check (action_key ~ '^[0-9a-f]{64}$'),
  action jsonb not null check (jsonb_typeof(action) = 'object'),
  status text not null default 'pending' check (status in ('pending','running','succeeded','failed')),
  attempt_count integer not null default 0 check (attempt_count between 0 and 5),
  lease_token uuid,
  lease_until timestamptz,
  next_attempt_at timestamptz not null default clock_timestamp(),
  last_error text,
  dispatch_receipt jsonb,
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  check ((status = 'running') = (lease_token is not null and lease_until is not null))
);
create index weekly_automation_actions_due_idx on private.weekly_automation_actions (status, next_attempt_at);

-- Empty expected sets must be explicitly frozen; NULL/missing never means zero.
create table private.weekly_automation_round_policies (
  round_id text primary key references public.weekly_quiz_rounds(round_id),
  environment text not null check (environment in ('production','preview','development')),
  blind_manifest_sha256 text not null check (blind_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  expected_execution_ids uuid[] not null,
  source_policy_sha256 text check (source_policy_sha256 ~ '^[0-9a-f]{64}$'),
  frozen_at timestamptz not null default clock_timestamp()
);
create table private.weekly_automation_benchmarks (
  execution_id uuid primary key,
  round_id text not null references private.weekly_automation_round_policies(round_id),
  driver text not null check (driver ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  model_id text not null check (length(model_id) between 1 and 200),
  config_sha256 text not null check (config_sha256 ~ '^[0-9a-f]{64}$'),
  max_cost_usd numeric check (max_cost_usd > 0 and max_cost_usd::text not in ('NaN','Infinity','-Infinity')),
  artifact_uri text,
  verified_execution_sha256 text check (verified_execution_sha256 ~ '^[0-9a-f]{64}$'),
  verified_payload_digest text check (verified_payload_digest ~ '^[0-9a-f]{64}$'),
  artifact_sha256 text check (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default clock_timestamp(),
  check ((artifact_uri is null) = (artifact_sha256 is null)),
  check (artifact_uri is null or artifact_uri ~ '^supabase://[a-z0-9._-]+/.+')
);

create function public.freeze_weekly_automation_policy_v1(p_round_id text, p_expected_executions jsonb,p_source_policy_sha256 text default null)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare
  r public.weekly_quiz_rounds%rowtype;
  existing private.weekly_automation_round_policies%rowtype;
  ids uuid[];
  job jsonb;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if jsonb_typeof(p_expected_executions) is distinct from 'array' or jsonb_array_length(p_expected_executions) > 20 then
    raise exception 'expected executions must be a bounded explicit array';
  end if;
  select * into strict r from public.weekly_quiz_rounds where round_id=p_round_id for update;
  if r.status <> 'open' or r.revealed_at is not null then raise exception 'policy requires an unrevealed open-status round'; end if;
  select coalesce(array_agg((e->>'execution_id')::uuid order by e->>'execution_id'), '{}'::uuid[]) into ids
    from jsonb_array_elements(p_expected_executions) e;
  if cardinality(ids) <> (select count(distinct x) from unnest(ids) x) then raise exception 'duplicate expected execution'; end if;
  select * into existing from private.weekly_automation_round_policies where round_id=p_round_id;
  if found then
    if existing.expected_execution_ids <> ids or existing.blind_manifest_sha256 <> r.blind_manifest_sha256 or existing.source_policy_sha256 is distinct from p_source_policy_sha256 then
      raise exception 'automation policy is immutable';
    end if;
    for job in select * from jsonb_array_elements(p_expected_executions) loop
      if not exists (select 1 from private.weekly_automation_benchmarks b where b.execution_id=(job->>'execution_id')::uuid
          and b.round_id=p_round_id and b.driver=job->>'driver' and b.model_id=job->>'model_id'
          and b.config_sha256=job->>'config_sha256' and b.max_cost_usd is not distinct from (job->>'max_cost_usd')::numeric) then raise exception 'expected execution identity is immutable'; end if;
    end loop;
    return jsonb_build_object('round_id',p_round_id,'idempotent',true);
  end if;
  insert into private.weekly_automation_round_policies(round_id,environment,blind_manifest_sha256,expected_execution_ids,source_policy_sha256)
    values(p_round_id,r.environment,r.blind_manifest_sha256,ids,p_source_policy_sha256);
  for job in select * from jsonb_array_elements(p_expected_executions) loop
    insert into private.weekly_automation_benchmarks(execution_id,round_id,driver,model_id,config_sha256,max_cost_usd)
      values((job->>'execution_id')::uuid,p_round_id,job->>'driver',job->>'model_id',job->>'config_sha256',(job->>'max_cost_usd')::numeric);
  end loop;
  return jsonb_build_object('round_id',p_round_id,'idempotent',false);
end $$;

create function public.register_weekly_automation_artifact_v1(p_execution_id uuid,p_artifact_uri text,p_artifact_sha256 text)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare b private.weekly_automation_benchmarks%rowtype;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if p_artifact_uri is null or p_artifact_sha256 is null then raise exception 'artifact reference required'; end if;
  select * into strict b from private.weekly_automation_benchmarks where execution_id=p_execution_id for update;
  if b.artifact_uri is not null then
    if b.artifact_uri <> p_artifact_uri or b.artifact_sha256 <> p_artifact_sha256 then raise exception 'artifact is immutable'; end if;
  else
    update private.weekly_automation_benchmarks set artifact_uri=p_artifact_uri,artifact_sha256=p_artifact_sha256 where execution_id=p_execution_id;
  end if;
  return jsonb_build_object('execution_id',p_execution_id,'registered',true);
end $$;

create function public.verify_weekly_automation_benchmark_v1(p_execution_id uuid,p_artifact_sha256 text,p_execution_sha256 text,p_payload_digest text)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare b private.weekly_automation_benchmarks%rowtype;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if p_execution_sha256 is null or p_payload_digest is null then raise exception 'verified digests required'; end if;
  select * into strict b from private.weekly_automation_benchmarks where execution_id=p_execution_id for update;
  if b.artifact_sha256 is null or b.artifact_sha256 is distinct from p_artifact_sha256 then raise exception 'artifact binding differs'; end if;
  if b.verified_execution_sha256 is not null and (b.verified_execution_sha256<>p_execution_sha256 or b.verified_payload_digest<>p_payload_digest) then raise exception 'verified artifact binding is immutable'; end if;
  update private.weekly_automation_benchmarks set verified_execution_sha256=p_execution_sha256,verified_payload_digest=p_payload_digest where execution_id=p_execution_id;
  return jsonb_build_object('execution_id',p_execution_id,'verified',true);
end $$;

create function public.record_weekly_automation_dispatch_v1(p_action_key text,p_lease_token uuid,p_receipt jsonb)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if jsonb_typeof(p_receipt) is distinct from 'object' or nullif(p_receipt->>'call_id','') is null or octet_length(p_receipt::text)>2048 then raise exception 'invalid dispatch receipt'; end if;
  update private.weekly_automation_actions set dispatch_receipt=p_receipt,updated_at=clock_timestamp()
    where action_key=p_action_key and status='running' and lease_token=p_lease_token and lease_until>clock_timestamp();
  if not found then raise exception 'stale automation lease' using errcode='40001'; end if;
  return jsonb_build_object('action_key',p_action_key,'recorded',true);
end $$;

-- Serialize final policy/artifact/window checks with the existing reveal write.
create function public.reveal_weekly_automation_round_v1(p_round_id text,p_evaluation_id text,p_reveal_manifest jsonb,p_reveal_manifest_sha256 text)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare r public.weekly_quiz_rounds%rowtype; e public.weekly_quiz_evaluations%rowtype; policy private.weekly_automation_round_policies%rowtype;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  select * into strict r from public.weekly_quiz_rounds where round_id=p_round_id for update;
  select * into strict policy from private.weekly_automation_round_policies where round_id=p_round_id;
  select * into strict e from public.weekly_quiz_evaluations where round_id=p_round_id and evaluation_id=p_evaluation_id;
  if r.environment<>'production' or r.status<>'open' or r.revealed_at is not null or r.closes_at>clock_timestamp()
    or e.campaign_id<>r.campaign_id or e.environment<>r.environment or e.round_opens_at<>r.opens_at or e.round_closes_at<>r.closes_at
    or e.blind_manifest_sha256<>r.blind_manifest_sha256 or e.private_index_sha256 is distinct from r.metadata#>>'{private_index,sha256}'
    or e.reveal_manifest_sha256 is distinct from p_reveal_manifest_sha256
    or policy.environment<>r.environment or policy.blind_manifest_sha256<>r.blind_manifest_sha256 then
    raise exception 'automation reveal source/window binding mismatch';
  end if;
  if r.metadata#>>'{retrospective_release,policy}'='next-weekly-activation' and r.metadata#>>'{retrospective_release,activated_by_round_id}' is null then
    raise exception 'automation reveal awaits successor activation';
  end if;
  if cardinality(policy.expected_execution_ids)<>(select count(*) from private.weekly_automation_benchmarks b where b.round_id=p_round_id)
    or exists(select 1 from unnest(policy.expected_execution_ids) id where not exists(
      select 1 from private.weekly_automation_benchmarks b join public.weekly_selector_post_close_benchmarks_v1 x on x.execution_id=b.execution_id
      where b.execution_id=id and b.round_id=p_round_id and b.artifact_sha256 is not null
        and x.round_id=p_round_id and x.environment=r.environment and x.provider=b.driver and x.requested_model_id=b.model_id and x.config_sha256=b.config_sha256
        and x.execution->>'blind_manifest_sha256'=r.blind_manifest_sha256 and x.execution_sha256=b.verified_execution_sha256 and x.payload_digest=b.verified_payload_digest
    )) then raise exception 'automation reveal requires every verified benchmark receipt'; end if;
  perform public.reveal_weekly_quiz_round(p_round_id,p_reveal_manifest,p_reveal_manifest_sha256);
  return jsonb_build_object('round_id',p_round_id,'status','revealed');
end $$;

create function public.weekly_automation_snapshot_v1()
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

create function public.enqueue_weekly_automation_action_v1(p_action jsonb)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare a private.weekly_automation_actions%rowtype; k text:=p_action->>'action_key';
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if jsonb_typeof(p_action) is distinct from 'object' or octet_length(p_action::text)>16384
     or k is null or k !~ '^[0-9a-f]{64}$' then raise exception 'invalid action'; end if;
  insert into private.weekly_automation_actions(action_key,action) values(k,p_action) on conflict do nothing;
  select * into strict a from private.weekly_automation_actions where action_key=k;
  if a.action <> p_action then raise exception 'action key is already bound differently'; end if;
  return jsonb_build_object('action_key',k,'status',a.status);
end $$;

create function public.claim_weekly_automation_action_v1(p_action_key text)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare a private.weekly_automation_actions%rowtype;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  select * into a from private.weekly_automation_actions where action_key=p_action_key for update skip locked;
  if not found or a.status in ('succeeded','failed') or a.next_attempt_at>clock_timestamp()
    or (a.status='running' and a.lease_until>clock_timestamp()) then return null; end if;
  if a.attempt_count>=5 then
    update private.weekly_automation_actions set status='failed',lease_token=null,lease_until=null,last_error='lease-expired-at-retry-limit',updated_at=clock_timestamp() where action_key=p_action_key;
    return null;
  end if;
  update private.weekly_automation_actions set status='running',attempt_count=attempt_count+1,
    lease_token=gen_random_uuid(),lease_until=clock_timestamp()+interval '3 hours',updated_at=clock_timestamp()
    where action_key=p_action_key returning * into a;
  return jsonb_build_object('action_key',a.action_key,'lease_token',a.lease_token,'attempt_count',a.attempt_count);
end $$;

create function public.finish_weekly_automation_action_v1(p_action_key text,p_lease_token uuid,p_outcome text,p_error text default null)
returns jsonb language plpgsql security definer
set search_path = pg_catalog, public, private, auth as $$
declare a private.weekly_automation_actions%rowtype;
begin
  if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
  if p_outcome not in ('succeeded','waiting','error') then raise exception 'invalid action outcome'; end if;
  select * into a from private.weekly_automation_actions where action_key=p_action_key and status='running'
    and lease_token=p_lease_token and lease_until>clock_timestamp() for update;
  if not found then raise exception 'stale automation lease' using errcode='40001'; end if;
  update private.weekly_automation_actions set
    status=case when p_outcome='succeeded' then 'succeeded' when p_outcome='error' and attempt_count>=5 then 'failed' else 'pending' end,
    attempt_count=case when p_outcome='waiting' then greatest(0,attempt_count-1) else attempt_count end,
    next_attempt_at=clock_timestamp()+make_interval(secs=>case when p_outcome='waiting' then 900 else least(3600,60*(2^attempt_count)::integer) end),
    lease_token=null,lease_until=null,last_error=left(p_error,512),updated_at=clock_timestamp()
    where action_key=p_action_key returning * into a;
  return jsonb_build_object('action_key',a.action_key,'status',a.status,'attempt_count',a.attempt_count);
end $$;

alter table private.weekly_automation_actions enable row level security;
alter table private.weekly_automation_round_policies enable row level security;
alter table private.weekly_automation_benchmarks enable row level security;
revoke all on table private.weekly_automation_actions,private.weekly_automation_round_policies,private.weekly_automation_benchmarks from public,anon,authenticated,service_role;
revoke all on function public.freeze_weekly_automation_policy_v1(text,jsonb,text),public.register_weekly_automation_artifact_v1(uuid,text,text),public.weekly_automation_snapshot_v1(),public.reveal_weekly_automation_round_v1(text,text,jsonb,text),public.verify_weekly_automation_benchmark_v1(uuid,text,text,text),public.record_weekly_automation_dispatch_v1(text,uuid,jsonb),public.enqueue_weekly_automation_action_v1(jsonb),public.claim_weekly_automation_action_v1(text),public.finish_weekly_automation_action_v1(text,uuid,text,text) from public,anon,authenticated;
grant execute on function public.freeze_weekly_automation_policy_v1(text,jsonb,text),public.register_weekly_automation_artifact_v1(uuid,text,text),public.weekly_automation_snapshot_v1(),public.reveal_weekly_automation_round_v1(text,text,jsonb,text),public.verify_weekly_automation_benchmark_v1(uuid,text,text,text),public.record_weekly_automation_dispatch_v1(text,uuid,jsonb),public.enqueue_weekly_automation_action_v1(jsonb),public.claim_weekly_automation_action_v1(text),public.finish_weekly_automation_action_v1(text,uuid,text,text) to service_role;

commit;
