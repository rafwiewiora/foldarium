-- Durable initial/retry handoffs. No new scientific attempt authority beyond two.
begin;
create table private.weekly_prediction_dispatch_epoch (
 singleton boolean primary key default true check(singleton),
 started_at timestamptz not null default clock_timestamp()
);
insert into private.weekly_prediction_dispatch_epoch default values;
create table private.weekly_prediction_dispatches (
 dispatch_id uuid primary key default gen_random_uuid(),
 run_id text not null references public.prediction_runs(run_id),
 attempt_number integer not null check(attempt_number in (1,2)),
 execution_task jsonb not null,
 retry_request jsonb,
 reviewed_diagnostic_sha256 text,
 spawn_started_at timestamptz,
 call_id text unique,
 worker_task_id text,
 acknowledged_at timestamptz,
 created_at timestamptz not null default clock_timestamp(),
 unique(run_id,attempt_number)
);
create table private.weekly_prediction_lost_attempts (
 dispatch_id uuid primary key references private.weekly_prediction_dispatches(dispatch_id),
 run_id text not null,
 attempt_number integer not null,
 call_id text not null,
 worker_task_id text,
 terminal_status text not null,
 prior_run jsonb not null,
 recorded_at timestamptz not null default clock_timestamp()
);
create function private.prevent_weekly_prediction_loss_mutation() returns trigger
language plpgsql set search_path=pg_catalog as $$
begin raise exception 'prediction attempt evidence is immutable'; end $$;
create trigger weekly_prediction_loss_immutable before update or delete on private.weekly_prediction_lost_attempts
for each row execute function private.prevent_weekly_prediction_loss_mutation();

create function public.prepare_weekly_prediction_dispatch_v1(p_run_id text,p_execution_task jsonb,p_retry_request jsonb default null,p_existing_call_id text default null,p_retry_descriptor_sha256 text default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare r public.prediction_runs%rowtype; d private.weekly_prediction_dispatches%rowtype;
 cid text; n integer; gpu text; seconds integer; expected jsonb;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 select t.campaign_id into strict cid from public.prediction_runs x join public.targets t using(target_id) where x.run_id=p_run_id;
 -- Serialize campaign retry accounting before locking the selected run.
 perform pg_advisory_xact_lock(hashtextextended('weekly-prediction-dispatch:'||cid,0));
 select * into strict r from public.prediction_runs where run_id=p_run_id for update;
 n := case when p_retry_request is null then 1 else 2 end;
 select * into d from private.weekly_prediction_dispatches where run_id=p_run_id and attempt_number=n;
 if found then
  if d.execution_task is distinct from p_execution_task or d.retry_request is distinct from p_retry_request or d.reviewed_diagnostic_sha256 is distinct from p_retry_descriptor_sha256 then raise exception 'dispatch identity already bound differently'; end if;
  return to_jsonb(d);
 end if;
 if p_existing_call_id is not null and p_existing_call_id !~ '^fc-[A-Za-z0-9]+$' then raise exception 'invalid existing call identity'; end if;
 if n=1 then
  if r.status not in ('pending','queued') or r.attempt_count<>0 or r.max_attempts<>1 or r.task_payload is distinct from p_execution_task then raise exception 'initial dispatch source changed'; end if;
  if p_existing_call_id is null and r.created_at<(select started_at from private.weekly_prediction_dispatch_epoch) then raise exception 'legacy initial dispatch requires exact prior call evidence'; end if;
 else
  if r.status<>'failed' or r.attempt_count<>1 or r.max_attempts<>1 then raise exception 'retry requires an unmodified failed first attempt'; end if;
  if exists(select 1 from public.prediction_artifacts where run_id=r.run_id)
   or coalesce(jsonb_array_length(r.result->'samples'),0)>0 then raise exception 'durable scientific artifacts require recovery review before retry'; end if;
  if (select descriptor_sha256 from private.prediction_failure_diagnostics where run_id=r.run_id and attempt_count=1) is distinct from p_retry_descriptor_sha256 then raise exception 'retry diagnostic inspection binding changed'; end if;
  if p_retry_request->>'run_id' is distinct from r.run_id or p_retry_request->>'target_id' is distinct from r.target_id
   or p_retry_request->>'method' is distinct from r.method or p_retry_request->>'source_error_code' is distinct from r.error_code
   or jsonb_typeof(p_retry_request->'reviewed_legacy') is distinct from 'boolean' then raise exception 'retry source identity mismatch'; end if;
  if r.task_payload#>>'{resources,gpu_class}' is distinct from 'l4' or r.task_payload#>>'{resources,timeout_seconds}' is distinct from '1800'
   or r.method not in ('boltz2','openfold3') then raise exception 'retry original resources are outside reviewed policy'; end if;
  gpu := case when p_retry_request->>'retry_kind'='gpu_out_of_memory' then 'a100-40gb' else 'l4' end;
  seconds := case when p_retry_request->>'retry_kind'='msa_generation_timeout' then 4500 else 1800 end;
  if p_retry_request->>'retry_kind' not in ('gpu_out_of_memory','msa_generation_timeout','msa_preprocessing_failed','repeat_once')
   or p_retry_request->>'retry_gpu_class' is distinct from gpu or p_retry_request->>'retry_timeout_seconds' is distinct from seconds::text then raise exception 'retry resource policy mismatch'; end if;
  expected := jsonb_set(r.task_payload,'{resources}',(r.task_payload->'resources') || jsonb_build_object('gpu_class',gpu,'timeout_seconds',seconds,
   'retry_policy',jsonb_build_object('retry_kind',p_retry_request->>'retry_kind','source_error_code',r.error_code,'reviewed_legacy',p_retry_request->'reviewed_legacy','original_gpu_class','l4','original_timeout_seconds',1800)));
  if expected is distinct from p_execution_task then raise exception 'retry execution changes immutable science'; end if;
  -- At most40 targets/80 original half-hour L4 calls plus one maximum bounded
  -- retry each. This conservative ceiling cannot exceed the existing40h +
  -- 80*4500s and $44.62848 +80*$1.39464 policy, including prior retries.
  if (select count(*) from public.prediction_runs x join public.targets t using(target_id) where t.campaign_id=cid)>80
   or (select count(*) from public.targets t where t.campaign_id=cid)>40
   or exists(select 1 from public.prediction_runs x join public.targets t using(target_id) where t.campaign_id=cid and
    (x.task_payload#>>'{resources,gpu_class}' is distinct from 'l4' or x.task_payload#>>'{resources,timeout_seconds}' is distinct from '1800'
      or x.max_attempts not in (1,2) or x.method not in ('boltz2','openfold3'))) then raise exception 'campaign is outside reviewed retry ceiling'; end if;
  update public.prediction_runs set max_attempts=2 where run_id=p_run_id;
 end if;
 insert into private.weekly_prediction_dispatches(run_id,attempt_number,execution_task,retry_request,reviewed_diagnostic_sha256,call_id,acknowledged_at,spawn_started_at)
 values(p_run_id,n,p_execution_task,p_retry_request,p_retry_descriptor_sha256,p_existing_call_id,case when p_existing_call_id is not null then clock_timestamp() end,case when p_existing_call_id is not null then clock_timestamp() end) returning * into d;
 return to_jsonb(d);
end $$;

create function public.get_prediction_failure_diagnostics_v1(p_run_id text,p_attempt_count integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 return jsonb_build_object('diagnostics',(select to_jsonb(d) from private.prediction_failure_diagnostics d where run_id=p_run_id and attempt_count=p_attempt_count),
  'scientific_artifacts_registered',exists(select 1 from public.prediction_artifacts where run_id=p_run_id));
end $$;

create function public.claim_weekly_prediction_dispatch_v1(p_dispatch_id uuid)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare d private.weekly_prediction_dispatches%rowtype; first_claim boolean:=false; r public.prediction_runs%rowtype;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 select * into strict d from private.weekly_prediction_dispatches where dispatch_id=p_dispatch_id for update;
 select * into strict r from public.prediction_runs where run_id=d.run_id;
 if d.spawn_started_at is null then
  if r.attempt_count<>d.attempt_number-1 or r.max_attempts<>d.attempt_number
   or r.status not in ('pending','queued','failed') then raise exception 'dispatch is no longer unclaimed'; end if;
  update private.weekly_prediction_dispatches set spawn_started_at=clock_timestamp() where dispatch_id=p_dispatch_id returning * into d;
  first_claim:=true;
 end if;
 return to_jsonb(d)||jsonb_build_object('first_claim',first_claim);
end $$;

create function public.acknowledge_weekly_prediction_dispatch_v1(p_dispatch_id uuid,p_call_id text,p_worker_task_id text default null,p_execution_task jsonb default null)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare d private.weekly_prediction_dispatches%rowtype;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 select * into strict d from private.weekly_prediction_dispatches where dispatch_id=p_dispatch_id for update;
 if d.spawn_started_at is null or p_call_id !~ '^fc-[A-Za-z0-9]+$' or p_call_id is null
  or (d.call_id is not null and d.call_id<>p_call_id)
  or (p_worker_task_id is not null and (p_worker_task_id !~ '^ta-[A-Za-z0-9]+$' or p_execution_task is distinct from d.execution_task))
  or (d.worker_task_id is not null and p_worker_task_id is not null and d.worker_task_id<>p_worker_task_id)
  or exists(select 1 from private.weekly_prediction_lost_attempts where dispatch_id=p_dispatch_id) then raise exception 'dispatch acknowledgement binding mismatch'; end if;
 update private.weekly_prediction_dispatches set call_id=p_call_id,worker_task_id=coalesce(worker_task_id,p_worker_task_id),acknowledged_at=coalesce(acknowledged_at,clock_timestamp()) where dispatch_id=p_dispatch_id returning * into d;
 return to_jsonb(d);
end $$;

create function public.get_weekly_prediction_dispatch_v1(p_run_id text,p_attempt_number integer)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 return (select to_jsonb(d) from private.weekly_prediction_dispatches d where run_id=p_run_id and attempt_number=p_attempt_number);
end $$;

create function public.record_weekly_prediction_worker_loss_v1(p_dispatch_id uuid,p_call_id text,p_task_id text,p_terminal_status text,p_expected_attempt_count integer,p_expected_lease_owner text,p_expected_lease_expires_at timestamptz)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare d private.weekly_prediction_dispatches%rowtype; r public.prediction_runs%rowtype; outcome jsonb;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 select * into strict d from private.weekly_prediction_dispatches where dispatch_id=p_dispatch_id;
 select * into strict r from public.prediction_runs where run_id=d.run_id for update;
 if exists(select 1 from private.weekly_prediction_lost_attempts where dispatch_id=p_dispatch_id) then return jsonb_build_object('status','already-recorded'); end if;
 if d.call_id is distinct from p_call_id or p_call_id is null or p_terminal_status not in ('FAILURE','INIT_FAILURE','TERMINATED','TIMEOUT') then raise exception 'terminal dispatch proof mismatch'; end if;
 if r.attempt_count is distinct from p_expected_attempt_count or r.lease_owner is distinct from p_expected_lease_owner or r.lease_expires_at is distinct from p_expected_lease_expires_at
  or r.max_attempts<>d.attempt_number then raise exception 'prediction claim changed while observing worker'; end if;
 if r.status='running' then
  if r.attempt_count<>d.attempt_number or r.lease_expires_at is null or r.lease_expires_at>=clock_timestamp()
   or r.lease_owner is distinct from 'modal:'||p_task_id or p_task_id is null
   or (d.worker_task_id is not null and d.worker_task_id<>p_task_id) then raise exception 'worker lease is live or not bound to terminal call'; end if;
 elsif r.status in ('pending','queued','failed') then
  if r.attempt_count<>d.attempt_number-1 or r.lease_owner is not null or r.lease_expires_at is not null then raise exception 'dispatch is not an unclaimed attempt'; end if;
 else raise exception 'prediction is already terminal'; end if;
 if exists(select 1 from public.prediction_artifacts where run_id=r.run_id)
  or exists(select 1 from private.prediction_failure_diagnostics where run_id=r.run_id and attempt_count=d.attempt_number)
  or r.result->>'status'='succeeded' or coalesce(jsonb_array_length(r.result->'samples'),0)>0 then raise exception 'durable scientific artifacts require recovery review'; end if;
 insert into private.weekly_prediction_lost_attempts(dispatch_id,run_id,attempt_number,call_id,worker_task_id,terminal_status,prior_run)
 values(d.dispatch_id,r.run_id,d.attempt_number,p_call_id,p_task_id,p_terminal_status,to_jsonb(r));
 outcome:=jsonb_build_object('schema_version',d.execution_task->>'schema_version','campaign_id',d.execution_task->>'campaign_id','output_uri_prefix',d.execution_task->>'output_uri_prefix',
  'status','failed','task_id',r.run_id,'target_id',r.target_id,'method',r.method,'method_version',r.method_version,'container_image',r.image_ref,
  'error_code','modal_worker_lost','error','Terminal Modal dispatch ended without durable prediction completion','dispatch_id',d.dispatch_id,'attempt_number',d.attempt_number);
 update public.prediction_runs set status='failed',attempt_count=d.attempt_number,result=outcome,error_code='modal_worker_lost',error_message=outcome->>'error',completed_at=clock_timestamp(),lease_owner=null,lease_expires_at=null where run_id=r.run_id;
 return jsonb_build_object('status','worker-loss-recorded','run_id',r.run_id,'attempt_number',d.attempt_number);
end $$;

alter function public.weekly_automation_snapshot_v1() set schema private;
alter function private.weekly_automation_snapshot_v1() rename to weekly_automation_snapshot_before_dispatch_v1;
revoke all on function private.weekly_automation_snapshot_before_dispatch_v1() from public,anon,authenticated,service_role;
create function public.weekly_automation_snapshot_v1()
returns jsonb language plpgsql security definer set search_path=pg_catalog,public,private,auth as $$
declare result jsonb;
begin
 if auth.role() is distinct from 'service_role' then raise exception 'service role required' using errcode='42501'; end if;
 result:=private.weekly_automation_snapshot_before_dispatch_v1();
 return jsonb_set(result,'{campaigns}',coalesce((select jsonb_agg(c||jsonb_build_object('runs',coalesce((select jsonb_agg(jsonb_build_object(
  'run_id',r.run_id,'status',r.status,'attempt_count',r.attempt_count,'max_attempts',r.max_attempts,'lease_owner',r.lease_owner,'lease_expires_at',r.lease_expires_at,
  'dispatch_tracking_eligible',r.created_at>=(select started_at from private.weekly_prediction_dispatch_epoch),
  'dispatch',(select to_jsonb(d)-'execution_task'-'retry_request' from private.weekly_prediction_dispatches d where d.run_id=r.run_id order by attempt_number desc limit 1)))
  from public.prediction_runs r join public.targets t using(target_id) where t.campaign_id=c->>'campaign_id'),'[]'::jsonb)))
  from jsonb_array_elements(result->'campaigns') c),'[]'::jsonb));
end $$;

alter table private.weekly_prediction_dispatches enable row level security;
alter table private.weekly_prediction_lost_attempts enable row level security;
alter table private.weekly_prediction_dispatch_epoch enable row level security;
revoke all on private.weekly_prediction_dispatches,private.weekly_prediction_lost_attempts,private.weekly_prediction_dispatch_epoch from public,anon,authenticated,service_role;
revoke all on function private.prevent_weekly_prediction_loss_mutation() from public,anon,authenticated,service_role;
revoke all on function public.prepare_weekly_prediction_dispatch_v1(text,jsonb,jsonb,text,text),public.claim_weekly_prediction_dispatch_v1(uuid),public.acknowledge_weekly_prediction_dispatch_v1(uuid,text,text,jsonb),public.get_weekly_prediction_dispatch_v1(text,integer),public.get_prediction_failure_diagnostics_v1(text,integer),public.record_weekly_prediction_worker_loss_v1(uuid,text,text,text,integer,text,timestamptz),public.weekly_automation_snapshot_v1() from public,anon,authenticated;
grant execute on function public.prepare_weekly_prediction_dispatch_v1(text,jsonb,jsonb,text,text),public.claim_weekly_prediction_dispatch_v1(uuid),public.acknowledge_weekly_prediction_dispatch_v1(uuid,text,text,jsonb),public.get_weekly_prediction_dispatch_v1(text,integer),public.get_prediction_failure_diagnostics_v1(text,integer),public.record_weekly_prediction_worker_loss_v1(uuid,text,text,text,integer,text,timestamptz),public.weekly_automation_snapshot_v1() to service_role;
commit;
