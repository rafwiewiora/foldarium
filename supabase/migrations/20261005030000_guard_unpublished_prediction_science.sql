-- Generic claimed-worker failure may follow completed native inference.
-- Keep its exact row/lease/attempt intact, even if archival was unavailable.
begin;
create or replace function public.record_weekly_prediction_worker_loss_v1(p_dispatch_id uuid,p_call_id text,p_task_id text,p_terminal_status text,p_expected_attempt_count integer,p_expected_lease_owner text,p_expected_lease_expires_at timestamptz)
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
  if p_terminal_status in ('FAILURE','INIT_FAILURE') then raise exception 'claimed worker failure requires native artifact recovery review'; end if;
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

commit;
