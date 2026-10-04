-- Private append-only evidence, separate from successful scientific artifacts.
begin;
create table private.prediction_failure_diagnostics (
  run_id text not null references public.prediction_runs(run_id),
  attempt_count integer not null check (attempt_count > 0),
  worker_id text not null,
  registered_task_sha256 text not null check (registered_task_sha256 ~ '^[0-9a-f]{64}$'),
  descriptor_sha256 text not null check (descriptor_sha256 ~ '^[0-9a-f]{64}$'),
  descriptor_uri text not null,
  descriptor_size_bytes integer not null check (descriptor_size_bytes between 1 and 262144),
  created_at timestamptz not null default clock_timestamp(),
  primary key (run_id, attempt_count)
);
revoke all on private.prediction_failure_diagnostics from public, anon, authenticated, service_role;
grant select on private.prediction_failure_diagnostics to service_role;

create function public.register_prediction_failure_diagnostics_v1(
  p_run_id text, p_attempt_count integer, p_worker_id text,
  p_registered_task_sha256 text, p_descriptor_sha256 text,
  p_descriptor_uri text, p_descriptor_size_bytes integer
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare
  v_run public.prediction_runs%rowtype;
  v_existing private.prediction_failure_diagnostics%rowtype;
  v_bucket text;
begin
  if auth.role() is distinct from 'service_role' then
    raise exception 'service role required' using errcode='42501';
  end if;
  if p_descriptor_sha256 is null or p_descriptor_sha256 !~ '^[0-9a-f]{64}$'
     or p_registered_task_sha256 is null or p_registered_task_sha256 !~ '^[0-9a-f]{64}$'
     or p_attempt_count is null or p_attempt_count < 1
     or p_descriptor_size_bytes is null or p_descriptor_size_bytes not between 1 and 262144
     or p_descriptor_uri is null then
    raise exception 'invalid failure diagnostic descriptor' using errcode='22023';
  end if;
  v_bucket := split_part(substr(p_descriptor_uri,12),'/',1);
  if p_descriptor_uri is distinct from ('supabase://' || v_bucket || '/sha256/' || substr(p_descriptor_sha256,1,2) || '/' || p_descriptor_sha256)
     or not exists (select 1 from storage.buckets b where b.id=v_bucket and b.public=false) then
    raise exception 'failure diagnostics require private immutable storage' using errcode='22023';
  end if;
  select * into v_run from public.prediction_runs where run_id=p_run_id for update;
  if not found then raise exception 'unknown prediction run' using errcode='P0002'; end if;
  select * into v_existing from private.prediction_failure_diagnostics where run_id=p_run_id and attempt_count=p_attempt_count;
  if found then
    if v_existing.worker_id is distinct from p_worker_id
       or v_existing.registered_task_sha256 is distinct from p_registered_task_sha256
       or v_existing.descriptor_sha256 is distinct from p_descriptor_sha256
       or v_existing.descriptor_uri is distinct from p_descriptor_uri
       or v_existing.descriptor_size_bytes is distinct from p_descriptor_size_bytes then
      raise exception 'failure diagnostic attempt is immutable' using errcode='23505';
    end if;
    return jsonb_build_object('status','already-registered');
  end if;
  if v_run.status <> 'running' or v_run.lease_owner is distinct from p_worker_id
     or v_run.attempt_count is distinct from p_attempt_count
     or v_run.task_sha256 is distinct from p_registered_task_sha256
     or v_run.lease_expires_at is null or v_run.lease_expires_at <= clock_timestamp() then
    raise exception 'failure diagnostic attempt is not leased by this worker' using errcode='55P03';
  end if;
  insert into private.prediction_failure_diagnostics(run_id,attempt_count,worker_id,registered_task_sha256,descriptor_sha256,descriptor_uri,descriptor_size_bytes)
  values(p_run_id,p_attempt_count,p_worker_id,p_registered_task_sha256,p_descriptor_sha256,p_descriptor_uri,p_descriptor_size_bytes);
  return jsonb_build_object('status','registered');
end;
$$;
revoke all on function public.register_prediction_failure_diagnostics_v1(text,integer,text,text,text,text,integer) from public, anon, authenticated;
grant execute on function public.register_prediction_failure_diagnostics_v1(text,integer,text,text,text,text,integer) to service_role;
commit;
